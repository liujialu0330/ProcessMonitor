"""
任务下拉选项模型（纯 Python，不依赖 Qt）

历史页与导出页的任务下拉同口径：选项文本 = format_task_option_text，
提示 = format_task_tooltip，"是否运行中"以调用方给出的运行中 ID 集合为准
（即任务状态服务的视角，不看数据库 status 列）。

模型只负责"选项长什么样"：任务列表从哪来（查库）、运行中集合怎么得到、
刷新时取哪些任务的最新记录，都由调用方决定并传入；时间同样由调用方传入，
同一批选项共用一个时刻。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Collection, Dict, List, Mapping, Optional, Sequence

from data.models import MonitorTask
from ui.formatting import format_task_option_text, format_task_tooltip


@dataclass(frozen=True)
class TaskOption:
    """一个下拉项：任务 ID、显示文本、悬停提示、载入/刷新时是否在运行。"""

    task_id: str
    text: str
    tooltip: str
    running: bool


@dataclass
class _Entry:
    task: MonitorTask
    option: TaskOption


class TaskOptionsModel:
    """任务下拉选项模型。

    - rebuild：整体重建（顺序与传入的任务一致，调用方负责按开始时间倒序）；
    - refresh_running：只刷新"上次仍在运行"的项：时长随 now 增长，任务结束后
      换成最终时长并不再刷新；返回文本或提示有变化的下标，供控件原地更新。
    """

    def __init__(self, *, full_date: bool = False):
        """
        Args:
            full_date: 开始时间是否带年份（导出页 True，历史页 False）
        """
        self._full_date = full_date
        self._entries: List[_Entry] = []
        self._index: Dict[str, int] = {}

    def _make_option(self, task: MonitorTask, running: bool,
                     now: datetime) -> TaskOption:
        return TaskOption(
            task_id=task.task_id,
            text=format_task_option_text(
                task, running=running, now=now, full_date=self._full_date),
            tooltip=format_task_tooltip(task, running, now),
            running=running,
        )

    def rebuild(self, tasks: Sequence[MonitorTask], running_ids: Collection[str],
                now: datetime) -> List[TaskOption]:
        """按传入顺序重建全部选项并返回。

        Args:
            tasks: 已按期望顺序（最新在前）排好的任务
            running_ids: 此刻仍在采集的任务 ID
            now: 本批选项共用的当前时刻
        """
        self._entries = [
            _Entry(task, self._make_option(task, task.task_id in running_ids, now))
            for task in tasks]
        self._index = {entry.task.task_id: i for i, entry in enumerate(self._entries)}
        return self.options()

    def tracked_running_ids(self) -> List[str]:
        """仍按"运行中"显示的任务 ID（按下拉顺序）：刷新时只需为它们取最新记录。"""
        return [entry.task.task_id for entry in self._entries if entry.option.running]

    def refresh_running(self, now: datetime, running_ids: Collection[str],
                        latest_tasks: Mapping[str, Optional[MonitorTask]]) -> List[int]:
        """原地刷新运行中项，返回文本或提示发生变化的下标（升序）。

        已结束的项不再参与刷新（最终时长固定）；运行中项若此刻已不在
        running_ids 中，即改为已结束并按其结束时间显示最终时长。

        Args:
            now: 当前时刻
            running_ids: 此刻仍在采集的任务 ID
            latest_tasks: 任务 ID -> 最新任务记录（结束时间等）；缺失或为 None
                （如已被删除）时沿用已有记录
        """
        changed = []
        for i, entry in enumerate(self._entries):
            if not entry.option.running:
                continue
            task_id = entry.task.task_id
            task = latest_tasks.get(task_id) or entry.task
            option = self._make_option(task, task_id in running_ids, now)
            entry.task = task
            if option != entry.option:
                entry.option = option
                changed.append(i)
        return changed

    def options(self) -> List[TaskOption]:
        """当前全部选项（按下拉顺序的新列表）。"""
        return [entry.option for entry in self._entries]

    def index_of(self, task_id: Optional[str]) -> int:
        """任务所在下标，不在列表中返回 -1。"""
        if task_id is None:
            return -1
        return self._index.get(task_id, -1)

    def option(self, task_id: Optional[str]) -> Optional[TaskOption]:
        """任务对应的选项，不在列表中返回 None。"""
        index = self.index_of(task_id)
        return self._entries[index].option if index >= 0 else None

    def task(self, task_id: Optional[str]) -> Optional[MonitorTask]:
        """生成该选项所用的任务记录（刷新后为最新记录），不在列表中返回 None。"""
        index = self.index_of(task_id)
        return self._entries[index].task if index >= 0 else None

    def __len__(self) -> int:
        return len(self._entries)
