"""
进程组采集器：与 ProcessCollector 同一套对外接口，供 MonitorWorker 无差别使用

把 ProcessTreeSource（成员解析与逐实例读数）与 GroupAggregator（合计 / 成员聚合）接起来：
collect_metrics 返回 {序列键: 值}，合计序列用原指标键、成员序列为 "指标@成员键"，另有
进程总数 process_count；进程组专属的数据（成员快照、实例出现与消失、成员是否被截断）放在
last_group（GroupSample）里，由采集线程取走写 task_members、发 members_changed。

生命周期口径：
- tree：根进程退出（含 PID 被复用）时 collect_metrics 返回 None，任务以 PROCESS_EXITED 结束；
  子进程退出不影响。启动时根不存在，is_process_running 为 False；
- name：永远"在运行"，没有任何同名进程的周期只写进程总数 0（见 core.group_aggregator）。
"""
import logging
from typing import Dict, List, Optional, Sequence

from core.contracts import UnavailableReason
from core.group_aggregator import GroupAggregator, GroupSample, InstanceInfo
from core.process_tree import GroupSpec, ProcessTreeSource, SnapshotTreeSource
from core.snapshot_hub import SnapshotHub
from data.models import TARGET_TREE
from utils.metrics import (MAX_GROUP_MEMBERS, PROCESS_COUNT, GroupAggregation, MetricType,
                           group_aggregation)

logger = logging.getLogger(__name__)


class GroupCollector:
    """进程组的采集器。每个任务一个实例，只由采集线程调用。"""

    def __init__(self, spec: GroupSpec, *, source: Optional[ProcessTreeSource] = None,
                 hub: Optional[SnapshotHub] = None, max_age_s: Optional[float] = None,
                 max_members: int = MAX_GROUP_MEMBERS, label: str = ''):
        """
        Args:
            spec: 组目标
            source: 成员来源；None 用 SnapshotTreeSource（hub 为 None 时取进程内共享的快照中心）
            hub / max_age_s: 只在 source 为 None 时用，传给 SnapshotTreeSource
            max_members: 成员名种数上限
            label: 日志里标识任务的文字
        """
        self.spec = spec
        if source is None:
            kwargs = {} if max_age_s is None else {'max_age_s': max_age_s}
            source = SnapshotTreeSource(hub, **kwargs)
        self._source = source
        self._aggregator = GroupAggregator(max_members, label)
        # 本周期可接受的最早快照时刻，由 MonitorWorker 每周期设置（同 ProcessCollector）
        self.snapshot_not_before: Optional[float] = None
        # 最近一次 collect_metrics 中无法采集的指标：{指标: 原因}
        self.last_unavailable: Dict[str, UnavailableReason] = {}
        # 最近一次 collect_metrics 的进程组数据；还没采过或根已退出时为 None
        self.last_group: Optional[GroupSample] = None
        self.last_source: Optional[str] = None
        self._root_create_time: Optional[float] = None

    # ---------- 与 ProcessCollector 同名的接口 ----------

    def is_process_running(self) -> bool:
        """任务启动时的存在性检查：tree 看根进程在不在；name 恒为真（允许先开始、成员稍后出现）。
        同时确认根进程的创建时间（之后的周期据此识别 PID 复用）。"""
        observation = self._source.observe(self.spec, self._read_metrics([]),
                                           root_create_time=None)
        if self.spec.kind == TARGET_TREE:
            self._root_create_time = observation.root_create_time
            return observation.root_alive
        return True

    def get_create_time(self) -> Optional[float]:
        """tree 的根进程创建时间（is_process_running 之后可用）；name 为 None。"""
        return self._root_create_time

    def prime_cpu(self) -> None:
        """记下各实例 CPU 差分的起点，让含 CPU 指标的任务不必空一个周期。读不到就算了。"""
        try:
            observation = self._source.observe(
                self.spec, [], root_create_time=self._root_create_time,
                not_before=self.snapshot_not_before)
        except Exception:
            logger.warning("进程组 CPU 预热失败，首个 CPU 点可能缺失", exc_info=True)
            return
        self._aggregator.prime(observation)

    def collect_metrics(self, metric_types: Sequence[str]) -> Optional[Dict[str, float]]:
        """
        采集一个周期。返回 {序列键: 值}；tree 的根进程已退出（含 PID 复用）时返回 None。
        不可用的指标不放入字典，原因记在 last_unavailable（含对组不适用的指标）。
        取不到观察结果（快照与 psutil 都失败）时抛出异常，由采集线程按"本周期采集失败"处理。
        """
        self.last_unavailable = {}
        observation = self._source.observe(
            self.spec, self._read_metrics(metric_types),
            root_create_time=self._root_create_time, not_before=self.snapshot_not_before)
        self.last_source = observation.source
        if self.spec.kind == TARGET_TREE:
            if self._root_create_time is None and observation.root_alive:
                self._root_create_time = observation.root_create_time
            if not observation.root_alive:
                self.last_group = None
                return None
        sample = self._aggregator.update(observation, metric_types)
        self.last_group = sample
        self.last_unavailable = dict(sample.unavailable)
        return dict(sample.values)

    # ---------- 进程组专属 ----------

    def alive_instances(self) -> List[InstanceInfo]:
        """当前仍存活的实例（收尾时据此补写 task_members 的 last_ms）。"""
        return list(self._aggregator.alive_instances())

    @property
    def members_truncated(self) -> bool:
        return self._aggregator.members_truncated

    @staticmethod
    def _read_metrics(metric_types: Sequence[str]) -> List[str]:
        """真正需要逐实例读取的指标：对组不适用的、进程总数与 CPU 使用率（由 CPU 时间差分）
        都不用读。"""
        return [m for m in metric_types
                if m not in (PROCESS_COUNT, MetricType.CPU_PERCENT)
                and group_aggregation(m) is not GroupAggregation.NONE]
