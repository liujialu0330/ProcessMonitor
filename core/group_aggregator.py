"""
进程组读数的聚合：逐实例读数 → 合计序列 + 成员序列（纯逻辑，不碰 psutil / Qt / 数据库）

输入是 ProcessTreeSource 给出的一个周期的实例读数（core.process_tree.Observation），输出是
这个周期要落库的 {序列键: 值}：

- 合计序列用原指标键，成员序列用 "指标@成员键"（见 utils.metrics.member_series_key）。
  成员 = 同名实例的合并（成员键为进程名小写），因此 PID 换了、名字不变，成员曲线仍然连续。
- 聚合规则由 MetricKind 派生（utils.metrics.group_aggregation），这里不逐指标手写：
  · 水位类（字节、个数、百分比）：各实例求和；
  · 累计类（CPU 时间、页面错误、上下文切换、I/O 次数与字节）：虚拟累计 = 每个实例（按
    pid + 创建时间区分）自首次出现以来的正增量之和。首次出现只记起点、不计入；计数回绕
    （读数变小）只重设起点；实例消失已经计入的不扣回，所以合计与成员序列都不会出现负值。
    结果是"任务开始以来各实例消耗量"的单调累计，分析页照常画每秒速率；
  · 峰值类与基本优先级：对组不适用，记为不可用（NOT_SUPPORTED），不产生序列；
  · CPU 使用率：每个实例按自己的 CPU 时间与单调时钟差分，再求和；新出现的实例首个周期
    没有差分起点，不计入。
- 进程总数 process_count 是组指标：合计 = 当前实例数，每个成员另有 process_count@成员键
  （成员本周期没有实例为 0）。
- 成员最多 MAX_GROUP_MEMBERS 种名字（先到先得，已占位的名字终身保留）；之后新出现的名字
  不建成员序列，其进程仍计入合计，只记一次日志并置 members_truncated。
- 没有任何实例的周期（name 任务成员归零）：只产出进程总数 0（与各成员的 0），其他指标本周期
  不写，形成空洞，不能写成 0。
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from core.contracts import UnavailableReason
from core.process_tree import InstanceKey, InstanceReading, Observation
from utils.metrics import (MAX_GROUP_MEMBERS, PROCESS_COUNT, GroupAggregation, MetricType,
                           group_aggregation, member_series_key, normalize_member_key)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstanceInfo:
    """一个进程实例的身份（task_members 留痕用）。"""
    pid: int
    create_time: float
    name: str

    @property
    def key(self) -> InstanceKey:
        return (self.pid, self.create_time)


@dataclass(frozen=True)
class MemberSnapshot:
    """一个成员在某个周期的状态。values 的键是不带后缀的指标键；本周期没有实例的成员
    只有 process_count（为 0）。"""
    member_key: str
    name: str
    instance_count: int
    values: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class GroupSample:
    """聚合器一个周期的输出。

    values：{序列键: 值}（合计序列在前、成员序列在后），可直接作为这个周期的采集结果；
    unavailable：合计序列里不可用的指标及原因（含对组不适用的指标）；
    members：全部已建序列的成员（含本周期没有实例的），按建立顺序；
    appeared / disappeared / alive：相对上个周期新出现、消失、仍存活的实例；
    members_changed：已建成员列表或"本周期有实例的成员"集合与上个周期不同（首个周期也算）；
    members_truncated：是否出现过因成员名超过上限而没建序列的进程（一旦为真保持为真）。
    """
    values: Dict[str, float]
    unavailable: Dict[str, UnavailableReason]
    members: Tuple[MemberSnapshot, ...]
    instance_count: int
    appeared: Tuple[InstanceInfo, ...] = ()
    disappeared: Tuple[InstanceInfo, ...] = ()
    alive: Tuple[InstanceInfo, ...] = ()
    members_truncated: bool = False
    members_changed: bool = False


@dataclass
class _Plan:
    """本次聚合涉及的指标按聚合方式分好类（水位类 / 累计类）。"""
    aggregation: Dict[str, GroupAggregation]
    level: List[str]
    delta: List[str]


def _info(instance: InstanceReading) -> InstanceInfo:
    return InstanceInfo(instance.pid, instance.create_time, instance.name)


class GroupAggregator:
    """一个组任务一个实例，有状态（实例的累计起点、CPU 差分起点、已建成员），只由采集线程用。"""

    def __init__(self, max_members: int = MAX_GROUP_MEMBERS, label: str = ''):
        self._max_members = max_members
        self._label = label
        # 成员键 → 显示名（原名，取最先出现的实例）；先到先得，占位终身保留
        self._tracked: Dict[str, str] = {}
        self._truncated = False
        self._alive: Dict[InstanceKey, InstanceInfo] = {}
        # 实例 CPU 差分的上一个点：(CPU 秒, 观察时刻的单调时钟)
        self._cpu_prev: Dict[InstanceKey, Tuple[float, float]] = {}
        self._cpu_valid_once = False
        # 累计类的各实例上次读数 {实例: {指标: 读数}}，与虚拟累计的合计 / 各成员值
        self._cum_prev: Dict[InstanceKey, Dict[str, float]] = {}
        self._cum_total: Dict[str, float] = {}
        self._cum_member: Dict[Tuple[str, str], float] = {}
        self._signature: Optional[Tuple[tuple, frozenset]] = None

    # ---------- 状态查询 ----------

    @property
    def members_truncated(self) -> bool:
        return self._truncated

    def tracked_members(self) -> Dict[str, str]:
        """已建序列的成员 {成员键: 显示名}（按建立顺序）。"""
        return dict(self._tracked)

    def alive_instances(self) -> Tuple[InstanceInfo, ...]:
        return tuple(self._alive.values())

    # ---------- CPU 预热 ----------

    def prime(self, observation: Observation) -> None:
        """记下 CPU 使用率的差分起点（任务开始前调用一次）：之后首个周期里已经存在的实例
        就有起点，不必空一个周期。只管 CPU，不登记实例、不动累计起点。"""
        for instance in observation.instances:
            if instance.cpu_seconds is not None:
                self._cpu_prev[instance.key] = (instance.cpu_seconds,
                                                observation.taken_monotonic)

    # ---------- 每周期聚合 ----------

    def update(self, observation: Observation, metrics: Sequence[str]) -> GroupSample:
        instances = observation.instances
        by_member = self._group_by_member(instances)
        self._track(by_member)

        plan = self._plan(metrics)
        sums, delta_seen = self._accumulate(instances, plan)
        cpu_total, cpu_by_member = self._cpu_step(observation, plan)

        values: Dict[str, float] = {}
        unavailable: Dict[str, UnavailableReason] = {}
        present = bool(instances)
        for metric in metrics:
            agg = plan.aggregation[metric]
            if agg is GroupAggregation.NONE:
                unavailable[metric] = UnavailableReason.NOT_SUPPORTED
            elif metric == PROCESS_COUNT:
                values[metric] = float(len(instances))
            elif not present:
                continue
            elif metric == MetricType.CPU_PERCENT:
                self._emit_cpu_total(values, unavailable, instances, cpu_total)
            elif agg is GroupAggregation.SUM:
                if (None, metric) in sums:
                    values[metric] = sums[(None, metric)]
                else:
                    unavailable[metric] = self._reason(instances, metric)
            else:
                if (None, metric) in delta_seen:
                    values[metric] = self._cum_total.get(metric, 0.0)
                else:
                    unavailable[metric] = self._reason(instances, metric)

        members = self._member_series(values, by_member, plan, sums, delta_seen,
                                      cpu_by_member)

        appeared, disappeared = self._diff_alive(instances)
        signature = (tuple(self._tracked), frozenset(by_member))
        changed = signature != self._signature
        self._signature = signature
        return GroupSample(
            values=values, unavailable=unavailable, members=members,
            instance_count=len(instances), appeared=appeared, disappeared=disappeared,
            alive=tuple(self._alive.values()), members_truncated=self._truncated,
            members_changed=changed)

    # ---------- 内部：分组与成员建立 ----------

    @staticmethod
    def _group_by_member(instances: Sequence[InstanceReading]
                         ) -> Dict[str, List[InstanceReading]]:
        by_member: Dict[str, List[InstanceReading]] = {}
        for instance in instances:
            by_member.setdefault(normalize_member_key(instance.name), []).append(instance)
        return by_member

    def _track(self, by_member: Dict[str, List[InstanceReading]]) -> None:
        """给新出现的成员名占位；超过上限的不建序列（只记一次日志）。同一周期里同时冒出
        多个新名字时按最早创建时间排，结果与枚举顺序无关。"""
        fresh = [key for key in by_member if key not in self._tracked]
        if not fresh:
            return
        fresh.sort(key=lambda key: (min(i.create_time for i in by_member[key]), key))
        for key in fresh:
            if len(self._tracked) < self._max_members:
                self._tracked[key] = by_member[key][0].name
            elif not self._truncated:
                self._truncated = True
                logger.warning("进程组%s成员名已达 %d 种上限，新出现的 %s 等不再单独建序列"
                               "（仍计入合计）", f" {self._label}" if self._label else "",
                               self._max_members, by_member[key][0].name)

    # ---------- 内部：聚合 ----------

    @staticmethod
    def _plan(metrics: Sequence[str]) -> _Plan:
        aggregation = {m: (GroupAggregation.SUM if m == PROCESS_COUNT
                           else group_aggregation(m)) for m in metrics}
        level = [m for m in metrics if aggregation[m] is GroupAggregation.SUM
                 and m not in (PROCESS_COUNT, MetricType.CPU_PERCENT)]
        delta = [m for m in metrics if aggregation[m] is GroupAggregation.DELTA_SUM]
        return _Plan(aggregation, level, delta)

    def _accumulate(self, instances: Sequence[InstanceReading], plan: _Plan):
        """水位类求和与累计类的正增量累加。返回 ({(成员键或 None, 指标): 和}, 本周期有读数
        的 {(成员键或 None, 指标)} 集合)——合计用 None 作成员键。"""
        sums: Dict[Tuple[Optional[str], str], float] = {}
        delta_seen: Set[Tuple[Optional[str], str]] = set()
        for instance in instances:
            member = normalize_member_key(instance.name)
            tracked = member in self._tracked
            for metric in plan.level:
                value = instance.values.get(metric)
                if value is None:
                    continue
                sums[(None, metric)] = sums.get((None, metric), 0.0) + value
                if tracked:
                    sums[(member, metric)] = sums.get((member, metric), 0.0) + value
            for metric in plan.delta:
                value = instance.values.get(metric)
                if value is None:
                    continue
                last = self._cum_prev.setdefault(instance.key, {})
                previous = last.get(metric)
                last[metric] = value
                if previous is not None and value > previous:
                    grown = value - previous
                    self._cum_total[metric] = self._cum_total.get(metric, 0.0) + grown
                    if tracked:
                        key = (member, metric)
                        self._cum_member[key] = self._cum_member.get(key, 0.0) + grown
                delta_seen.add((None, metric))
                if tracked:
                    delta_seen.add((member, metric))
        return sums, delta_seen

    def _cpu_step(self, observation: Observation, plan: _Plan):
        """CPU 使用率：每个有差分起点的实例各算一个百分比再求和，同时更新全部实例的起点。
        返回 (合计或 None, {成员键: 和})；没有任何实例带起点（合计为 None）时调用方不写值。"""
        instances = observation.instances
        now = observation.taken_monotonic
        total: Optional[float] = None
        by_member: Dict[str, float] = {}
        if MetricType.CPU_PERCENT not in plan.aggregation:
            self._drop_dead_cpu(instances)
            return total, by_member
        for instance in instances:
            if instance.cpu_seconds is None:
                continue
            previous = self._cpu_prev.get(instance.key)
            self._cpu_prev[instance.key] = (instance.cpu_seconds, now)
            if previous is None:
                continue
            elapsed = now - previous[1]
            if elapsed <= 0:
                # 同一时刻的两份观察：保留原起点，下次仍按完整窗口算
                self._cpu_prev[instance.key] = previous
                continue
            percent = max(0.0, instance.cpu_seconds - previous[0]) / elapsed * 100
            total = (total or 0.0) + percent
            member = normalize_member_key(instance.name)
            if member in self._tracked:
                by_member[member] = by_member.get(member, 0.0) + percent
        self._drop_dead_cpu(instances)
        return total, by_member

    def _drop_dead_cpu(self, instances: Sequence[InstanceReading]) -> None:
        if len(self._cpu_prev) > len(instances):
            alive = {i.key for i in instances}
            for key in [k for k in self._cpu_prev if k not in alive]:
                del self._cpu_prev[key]

    def _emit_cpu_total(self, values, unavailable, instances, cpu_total) -> None:
        if cpu_total is not None:
            self._cpu_valid_once = True
            values[MetricType.CPU_PERCENT] = round(cpu_total, 1)
        elif self._cpu_valid_once:
            # 全部实例都是新出现的（如每轮整体重建）：没有差分起点的实例不计入，合计为 0
            values[MetricType.CPU_PERCENT] = 0.0
        elif all(i.cpu_seconds is None for i in instances):
            unavailable[MetricType.CPU_PERCENT] = UnavailableReason.ERROR
        # 否则：还没有任何实例带差分起点，本周期不写（不写 0.0 冒充数据）

    @staticmethod
    def _reason(instances: Sequence[InstanceReading], metric: str) -> UnavailableReason:
        for instance in instances:
            reason = instance.unavailable.get(metric)
            if reason is not None:
                return reason
        return UnavailableReason.ERROR

    # ---------- 内部：成员序列 ----------

    def _member_series(self, values: Dict[str, float],
                       by_member: Dict[str, List[InstanceReading]], plan: _Plan, sums,
                       delta_seen, cpu_by_member) -> Tuple[MemberSnapshot, ...]:
        """把各成员的值写进 values（键为 指标@成员键）并生成成员快照。"""
        cpu_known = MetricType.CPU_PERCENT in values
        snapshots: List[MemberSnapshot] = []
        for key, name in self._tracked.items():
            count = len(by_member.get(key, ()))
            member_values: Dict[str, float] = {}
            for metric, agg in plan.aggregation.items():
                if agg is GroupAggregation.NONE:
                    continue
                if metric == PROCESS_COUNT:
                    member_values[metric] = float(count)
                elif count == 0:
                    continue
                elif metric == MetricType.CPU_PERCENT:
                    if cpu_known:
                        member_values[metric] = round(cpu_by_member.get(key, 0.0), 1)
                elif agg is GroupAggregation.SUM:
                    if (key, metric) in sums:
                        member_values[metric] = sums[(key, metric)]
                elif (key, metric) in delta_seen:
                    member_values[metric] = self._cum_member.get((key, metric), 0.0)
            for metric, value in member_values.items():
                values[member_series_key(metric, key)] = value
            snapshots.append(MemberSnapshot(key, name, count, member_values))
        return tuple(snapshots)

    # ---------- 内部：实例出现与消失 ----------

    def _diff_alive(self, instances: Sequence[InstanceReading]):
        current = {i.key: _info(i) for i in instances}
        appeared = tuple(info for key, info in current.items() if key not in self._alive)
        disappeared = tuple(info for key, info in self._alive.items() if key not in current)
        for info in disappeared:
            # 实例不会回来（PID 复用时创建时间不同，是另一个实例）：释放它的累计起点
            self._cum_prev.pop(info.key, None)
        self._alive = current
        return appeared, disappeared
