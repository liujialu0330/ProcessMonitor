"""
响应式布局断点

按"内容区宽度"（页面区域的宽度，侧栏占的部分已扣除；不是窗口宽度）分三档：

| 档位 | 内容区宽度 | 典型场景 |
|------|-----------|----------|
| compact | < 900 | 窗口最小尺寸（800）、窄窗口 |
| regular | 900 – 1099 | 窗口约 1000 宽（侧栏展开） |
| wide | ≥ 1100 | 默认窗口（1280，侧栏展开，内容区约 1130）/ 最大化 / 宽屏 |

档位切换带回滞（±16 px）：拖动窗口边缘时宽度在断点附近来回越过，不会反复切换版式。
`tier_for_width` / `next_tier` 是不依赖 Qt 的纯函数；`LayoutBreakpoints` 是一个小的 QObject
广播器，页面把自己的宽度喂给它（`track` 一次即可，之后随控件 Resize 自动喂入），各控件订阅 `tier_changed`，不必各自在
resizeEvent 里写宽度判断。
"""
from typing import Optional

from PyQt5.QtCore import QEvent, QObject, pyqtSignal
from PyQt5.QtWidgets import QWidget

COMPACT = "compact"
REGULAR = "regular"
WIDE = "wide"
TIERS = (COMPACT, REGULAR, WIDE)

# 档位下界（内容区宽度，像素）：低于 COMPACT_BELOW 为 compact，达到 WIDE_FROM 为 wide
COMPACT_BELOW = 900
WIDE_FROM = 1100
# 回滞：向上越过断点要多走这么多，向下越过断点也要多走这么多才切换
HYSTERESIS = 16

# 各档位小倍数图的列数
SMALL_MULTIPLE_COLUMNS = {COMPACT: 1, REGULAR: 2, WIDE: 3}


def tier_for_width(width: int) -> str:
    """不带回滞的档位（没有当前档位可参考时用）。"""
    if width < COMPACT_BELOW:
        return COMPACT
    if width < WIDE_FROM:
        return REGULAR
    return WIDE


def next_tier(current: Optional[str], width: int) -> str:
    """带回滞的档位：已在 current 档时，宽度要越过断点 ± HYSTERESIS 才离开；current 为 None
    （还没有档位）时等同 tier_for_width。一次跳变可以跨两档（如最大化瞬间 compact → wide）。"""
    if current not in TIERS:
        return tier_for_width(width)
    tier = current
    if tier == COMPACT and width >= COMPACT_BELOW + HYSTERESIS:
        tier = REGULAR
    if tier == REGULAR and width >= WIDE_FROM + HYSTERESIS:
        tier = WIDE
    if tier == WIDE and width < WIDE_FROM - HYSTERESIS:
        tier = REGULAR
    if tier == REGULAR and width < COMPACT_BELOW - HYSTERESIS:
        tier = COMPACT
    return tier


def small_multiple_columns(tier: str) -> int:
    """小倍数图的列数：wide 3 列，regular 2 列，compact 1 列。"""
    return SMALL_MULTIPLE_COLUMNS.get(tier, SMALL_MULTIPLE_COLUMNS[REGULAR])


class LayoutBreakpoints(QObject):
    """档位广播器：`set_width(width)` 喂入内容区宽度，档位变化时发 `tier_changed(档位)`。

    第一次喂入按不带回滞的档位算；在此之前 `tier` 是 REGULAR（订阅者构造时据此取初值）。
    """

    tier_changed = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._tier = REGULAR
        self._seeded = False
        self._width = 0
        self._tracked: Optional[QWidget] = None

    @property
    def tier(self) -> str:
        return self._tier

    @property
    def width(self) -> int:
        """最近一次喂入的内容区宽度（还没喂过为 0）。"""
        return self._width

    def set_width(self, width: int) -> bool:
        """喂入内容区宽度；档位变了发 tier_changed 并返回 True。宽度无效（≤ 0）时忽略。"""
        if width <= 0:
            return False
        self._width = width
        tier = next_tier(self._tier if self._seeded else None, width)
        self._seeded = True
        if tier == self._tier:
            return False
        self._tier = tier
        self.tier_changed.emit(tier)
        return True

    def track(self, widget: QWidget) -> None:
        """跟随控件的宽度：之后它每次 Resize 都喂入 widget.width()。不在这里喂初值：
        此时控件多半还没按窗口布局，那个宽度会让第一次回滞判断偏离真实情况。"""
        self._tracked = widget
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Resize and obj is self._tracked:
            self.set_width(obj.width())
        return False
