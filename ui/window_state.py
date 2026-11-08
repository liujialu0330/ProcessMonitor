"""
窗口与工作状态记忆

记住主窗口的几何、是否最大化、当前页，以及分析页上次选中的任务、时间范围、视图与时间
模式，下次启动时恢复。存在 app_config（Window / Analysis 两节），经 WindowMemoryStore 读写：
主窗口只认这个接口，测试注入内存替身，不碰全局 cfg。

本模块的纯函数（default_window_size / place_window）不依赖 Qt，屏幕列表由调用方给出
（可用区域，主屏在前），因此"记住的位置已不在任何屏幕上"这类情形用替身屏幕就能测。
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

import app_config
import config
from app_config import cfg

# (x, y, 宽, 高)
Rect = Tuple[int, int, int, int]

# 默认窗口大小；屏幕可用区太小时取可用区的 90%
DEFAULT_WINDOW_SIZE = (config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
SCREEN_FRACTION = 0.9
# 记住的窗口与某块屏幕的交叠至少这么大才算"还在屏幕上"（再小就没法抓住标题栏拖回来）
MIN_VISIBLE_SIZE = (100, 50)

# 页面键与顺序（与 app_config.WINDOW_PAGE_KEYS 一致）
PAGE_MONITOR, PAGE_ANALYSIS, PAGE_SETTING = app_config.WINDOW_PAGE_KEYS


@dataclass(frozen=True)
class AnalysisMemory:
    """分析页上次的状态；空串表示没有记录（用默认）。"""

    task_id: str = ""
    range_key: str = ""
    view_mode: str = ""
    time_mode: str = ""
    # 摘要表用户勾选的列（键见 ui.formatting.SUMMARY_COLUMNS，含始终可见的两列）；
    # 空元组 = 没有勾选过，列随窗口宽度的档位走
    summary_columns: Tuple[str, ...] = ()


@dataclass(frozen=True)
class WindowMemory:
    """主窗口要记住的全部状态。geometry 在最大化时是还原后的位置与大小；None 表示没有记录。"""

    geometry: Optional[Rect] = None
    maximized: bool = False
    page: str = PAGE_MONITOR
    analysis: AnalysisMemory = field(default_factory=AnalysisMemory)


@runtime_checkable
class WindowMemoryStore(Protocol):
    """窗口状态的存取与屏幕信息。"""

    def load(self) -> WindowMemory:
        """读出上次保存的状态；没有记录或内容不可用时给默认值。"""
        ...

    def save(self, memory: WindowMemory) -> bool:
        """保存；内容与已存的相同时不写盘，返回是否真的写了。"""
        ...

    def screens(self) -> List[Rect]:
        """各屏幕的可用区域（去掉任务栏），主屏在前。"""
        ...


def split_columns(raw) -> Tuple[str, ...]:
    """配置里逗号分隔的列键 → 元组（空 / 非字符串为空元组）；键是否认识由摘要表判断。"""
    if not isinstance(raw, str):
        return ()
    return tuple(part for part in (p.strip() for p in raw.split(",")) if part)


def join_columns(columns: Sequence[str]) -> str:
    return ",".join(columns)


def default_window_size(area: Rect, min_size: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
    """默认窗口大小：min(1280×820, 可用区 × 90%)，但不小于窗口的最小尺寸。"""
    width = min(DEFAULT_WINDOW_SIZE[0], int(area[2] * SCREEN_FRACTION))
    height = min(DEFAULT_WINDOW_SIZE[1], int(area[3] * SCREEN_FRACTION))
    return max(width, min_size[0]), max(height, min_size[1])


def _overlap(a: Rect, b: Rect) -> Tuple[int, int]:
    """两个矩形交叠部分的 (宽, 高)，不相交为 (0, 0)。"""
    width = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    height = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0, 0
    return width, height


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def place_window(saved: Optional[Rect], screens: Sequence[Rect],
                 min_size: Tuple[int, int] = (0, 0)) -> Rect:
    """决定窗口启动时的位置与大小。

    记住的几何与某块屏幕的可用区域有足够交叠时沿用（大小收进该屏幕，位置挪到完全落在
    其可用区内）；没有记录，或已完全不在任何屏幕上（拔掉了外接显示器、分辨率变小）时，
    用默认大小居中到主屏。没有屏幕信息时按 1920×1080 的主屏处理。
    """
    areas = list(screens) or [(0, 0, 1920, 1080)]
    if saved is not None:
        best, best_area = None, 0
        for area in areas:
            width, height = _overlap(saved, area)
            if width >= MIN_VISIBLE_SIZE[0] and height >= MIN_VISIBLE_SIZE[1] \
                    and width * height > best_area:
                best, best_area = area, width * height
        if best is not None:
            width = _clamp(saved[2], min_size[0], max(best[2], min_size[0]))
            height = _clamp(saved[3], min_size[1], max(best[3], min_size[1]))
            x = _clamp(saved[0], best[0], best[0] + max(best[2] - width, 0))
            y = _clamp(saved[1], best[1], best[1] + max(best[3] - height, 0))
            return x, y, width, height

    primary = areas[0]
    width, height = default_window_size(primary, min_size)
    return (primary[0] + (primary[2] - width) // 2,
            primary[1] + (primary[3] - height) // 2, width, height)


def available_screen_areas() -> List[Rect]:
    """当前各屏幕的可用区域，主屏在前（没有 QGuiApplication 时为空列表）。"""
    from PyQt5.QtGui import QGuiApplication
    if QGuiApplication.instance() is None:
        return []
    primary = QGuiApplication.primaryScreen()
    screens = sorted(QGuiApplication.screens(), key=lambda s: s is not primary)
    areas = []
    for screen in screens:
        rect = screen.availableGeometry()
        areas.append((rect.x(), rect.y(), rect.width(), rect.height()))
    return areas


class ConfigWindowMemory:
    """生产实现：读写 app_config 的全局 cfg 单例。"""

    def __init__(self, screens: Optional[Callable[[], List[Rect]]] = None):
        self._screens = screens or available_screen_areas

    def load(self) -> WindowMemory:
        raw = cfg.get(cfg.window_geometry)
        geometry = tuple(raw) if isinstance(raw, list) and len(raw) == 4 else None
        return WindowMemory(
            geometry=geometry,
            maximized=bool(cfg.get(cfg.window_maximized)),
            page=str(cfg.get(cfg.window_page)),
            analysis=AnalysisMemory(
                task_id=str(cfg.get(cfg.analysis_task) or ""),
                range_key=str(cfg.get(cfg.analysis_range) or ""),
                view_mode=str(cfg.get(cfg.analysis_view_mode) or ""),
                time_mode=str(cfg.get(cfg.analysis_time_mode) or ""),
                summary_columns=split_columns(cfg.get(cfg.analysis_summary_columns))))

    def save(self, memory: WindowMemory) -> bool:
        analysis = memory.analysis
        return app_config.update_values([
            (cfg.window_geometry, list(memory.geometry) if memory.geometry else []),
            (cfg.window_maximized, bool(memory.maximized)),
            (cfg.window_page, memory.page),
            (cfg.analysis_task, analysis.task_id),
            (cfg.analysis_range, analysis.range_key),
            (cfg.analysis_view_mode, analysis.view_mode),
            (cfg.analysis_time_mode, analysis.time_mode),
            (cfg.analysis_summary_columns, join_columns(analysis.summary_columns)),
        ])

    def screens(self) -> List[Rect]:
        return self._screens()
