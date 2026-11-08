"""
响应式断点（ui.layout_breakpoints）：档位边界、回滞、广播器与跟随控件宽度。
各控件按档位的行为见 tests/test_responsive_layout.py。
"""
import re
from pathlib import Path

import pytest
from PyQt5.QtWidgets import QWidget

from ui.layout_breakpoints import (
    COMPACT, COMPACT_BELOW, HYSTERESIS, REGULAR, WIDE, WIDE_FROM, LayoutBreakpoints, next_tier,
    small_multiple_columns, tier_for_width)


@pytest.mark.parametrize("width, tier", [
    (1, COMPACT), (800, COMPACT), (COMPACT_BELOW - 1, COMPACT),
    (COMPACT_BELOW, REGULAR), (1000, REGULAR), (WIDE_FROM - 1, REGULAR),
    (WIDE_FROM, WIDE), (1130, WIDE), (1280, WIDE), (1920, WIDE)])
def test_tier_for_width_boundaries(width, tier):
    assert (COMPACT_BELOW, WIDE_FROM) == (900, 1100)
    assert tier_for_width(width) == tier


def test_columns_per_tier():
    assert [small_multiple_columns(t) for t in (COMPACT, REGULAR, WIDE)] == [1, 2, 3]


def test_without_a_current_tier_there_is_no_hysteresis():
    assert next_tier(None, COMPACT_BELOW) == REGULAR
    assert next_tier(None, COMPACT_BELOW - 1) == COMPACT
    assert next_tier(None, WIDE_FROM) == WIDE


@pytest.mark.parametrize("current, width, expected", [
    # 向上：要多走 16 px 才离开
    (COMPACT, COMPACT_BELOW, COMPACT),
    (COMPACT, COMPACT_BELOW + HYSTERESIS - 1, COMPACT),
    (COMPACT, COMPACT_BELOW + HYSTERESIS, REGULAR),
    (REGULAR, WIDE_FROM, REGULAR),
    (REGULAR, WIDE_FROM + HYSTERESIS - 1, REGULAR),
    (REGULAR, WIDE_FROM + HYSTERESIS, WIDE),
    # 向下：同样要多走 16 px
    (REGULAR, COMPACT_BELOW - 1, REGULAR),
    (REGULAR, COMPACT_BELOW - HYSTERESIS, REGULAR),
    (REGULAR, COMPACT_BELOW - HYSTERESIS - 1, COMPACT),
    (WIDE, WIDE_FROM - 1, WIDE),
    (WIDE, WIDE_FROM - HYSTERESIS, WIDE),
    (WIDE, WIDE_FROM - HYSTERESIS - 1, REGULAR),
    # 一次跳变可以跨两档（最大化、还原）
    (COMPACT, 1920, WIDE),
    (WIDE, 700, COMPACT),
])
def test_hysteresis(current, width, expected):
    assert next_tier(current, width) == expected


def test_dragging_around_a_breakpoint_does_not_flap():
    """宽度在断点两侧 ±8 px 来回抖动，档位一次都不变。"""
    for start, edge in ((REGULAR, COMPACT_BELOW), (COMPACT, COMPACT_BELOW),
                        (REGULAR, WIDE_FROM), (WIDE, WIDE_FROM)):
        tier = start
        for width in [edge - 8, edge + 8, edge - 4, edge + 4, edge - 8, edge + 8] * 3:
            tier = next_tier(tier, width)
            assert tier == start, (start, edge, width)


def test_broadcaster_emits_only_on_change_and_seeds_without_hysteresis(qapp):
    bp = LayoutBreakpoints()
    seen = []
    bp.tier_changed.connect(seen.append)
    assert bp.tier == REGULAR                       # 还没有宽度时的初值
    assert bp.set_width(0) is False                 # 无效宽度忽略
    assert bp.set_width(COMPACT_BELOW - 1) is True  # 第一次按纯函数
    assert seen == [COMPACT] and bp.width == COMPACT_BELOW - 1
    assert bp.set_width(COMPACT_BELOW + 5) is False   # 回滞内
    assert bp.set_width(COMPACT_BELOW + HYSTERESIS) is True
    assert bp.set_width(1000) is False
    assert bp.set_width(1900) is True
    assert seen == [COMPACT, REGULAR, WIDE]


def test_first_width_in_regular_range_emits_nothing(qapp):
    bp = LayoutBreakpoints()
    seen = []
    bp.tier_changed.connect(seen.append)
    assert bp.set_width(COMPACT_BELOW) is False     # 初值就是 regular，订阅者已按它构造
    assert seen == []


def test_track_follows_widget_resizes(qapp):
    widget = QWidget()
    bp = LayoutBreakpoints()
    bp.track(widget)
    seen = []
    bp.tier_changed.connect(seen.append)
    widget.show()
    widget.resize(800, 600)
    qapp.processEvents()
    widget.resize(1400, 600)
    qapp.processEvents()
    widget.resize(1000, 600)
    qapp.processEvents()
    widget.close()
    assert seen == [COMPACT, WIDE, REGULAR]


# 控件的设备像素比只有 devicePixelRatioF 是小数：QWidget.devicePixelRatio() 返回整数，
# 在 125% / 150% 缩放下会把比例取整成 1 或 2。QScreen / QImage 的 devicePixelRatio() 本身
# 就是小数（没有 F 版本），这几处是允许的。
_DPR_CALL = re.compile(r"\.devicePixelRatio\(\)")
_DPR_ALLOWED = {
    "ui/services/__init__.py",                  # QScreen.devicePixelRatio()
    "ui/widgets/analysis/report_sheet.py",      # QImage.devicePixelRatio()
}


def test_no_integer_widget_device_pixel_ratio_in_ui():
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "ui").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in _DPR_ALLOWED:
            continue
        if _DPR_CALL.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == [], f"请改用 devicePixelRatioF()：{offenders}"
