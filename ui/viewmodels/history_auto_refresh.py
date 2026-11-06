"""
历史页运行中任务的自动刷新调度（无控件）

只管"什么时候刷新"：定时器、按数据量分档的间隔、"本次是否跳过"的判断；
"刷新什么、怎么查、结果是否过期"都在 HistoryViewModel.on_tick 里（与用户操作的
加载共用同一条查询路径与请求序号）。页面在显示/隐藏时调用 start()/stop()。
"""
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSlot

from core.history_query import HistorySnapshot

# 自动刷新间隔（毫秒）：按当前范围内数据点数自适应。数据越多每次刷新（统计聚合 + 分桶
# 在后台线程，填充在主线程）越重，间隔相应拉长，避免长时间监控后资源占用过高。
# 数据点数 < AUTO_REFRESH_TIER1_COUNT 用 5 秒；< AUTO_REFRESH_TIER2_COUNT 用 15 秒；更大用 30 秒。
AUTO_REFRESH_INTERVAL_MS = 5000
AUTO_REFRESH_INTERVAL_MID_MS = 15000
AUTO_REFRESH_INTERVAL_SLOW_MS = 30000
AUTO_REFRESH_TIER1_COUNT = 200_000
AUTO_REFRESH_TIER2_COUNT = 1_000_000


def auto_refresh_interval_for(count: int) -> int:
    """按范围内数据点数返回自动刷新间隔（毫秒）。"""
    if count < AUTO_REFRESH_TIER1_COUNT:
        return AUTO_REFRESH_INTERVAL_MS
    if count < AUTO_REFRESH_TIER2_COUNT:
        return AUTO_REFRESH_INTERVAL_MID_MS
    return AUTO_REFRESH_INTERVAL_SLOW_MS


class HistoryAutoRefresh(QObject):
    """
    历史页自动刷新定时器。

    Args:
        vm: HistoryViewModel；每次到点调用 vm.on_tick()
        should_skip: 页面提供的"本次是否跳过"判断（明细视图、鼠标悬停图表、窗口
            最小化等）；到点时与结果回来时各判断一次，跳过时定时器继续
    """

    def __init__(self, vm, should_skip: Optional[Callable[[], bool]] = None,
                 parent=None):
        super().__init__(parent)
        self._vm = vm
        self._timer = QTimer(self)
        self._timer.setInterval(AUTO_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.tick)
        vm.set_refresh_guard(should_skip if should_skip is not None else (lambda: False))
        # 用户操作的加载与定时刷新都按显示出来的数据量调整间隔
        vm.snapshot_ready.connect(self._on_snapshot)

    @property
    def interval(self) -> int:
        """当前定时间隔（毫秒）"""
        return self._timer.interval()

    @property
    def is_active(self) -> bool:
        """定时器是否在跑"""
        return self._timer.isActive()

    def start(self) -> None:
        """页面变为可见：开始计时（已在跑则重新计时）。"""
        self._timer.start()

    def stop(self) -> None:
        """页面隐藏：停止计时，并让在途查询的结果作废（不阻塞等待线程）。"""
        self._timer.stop()
        self._vm.cancel_pending()

    def update_interval(self, count: int) -> None:
        """按数据点数调整间隔；只在变化时设置，避免重置计时。"""
        interval = auto_refresh_interval_for(count)
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)

    @pyqtSlot()
    def tick(self) -> None:
        """到点：交给视图模型（跳过判断在其中进行，保证跳过时仍刷新时长文案）。"""
        self._vm.on_tick()

    @pyqtSlot(object)
    def _on_snapshot(self, snapshot: HistorySnapshot) -> None:
        if snapshot.stats:
            self.update_interval(snapshot.stats['count'])
