"""
报告图片的版式计算（纯函数，不依赖 Qt）

文字宽度由调用方传入的 measure(text) -> 宽度 给出（界面层用 QFontMetrics，测试可用 len），
这里只负责：按宽度折行、把 (标签, 值) 排成网格、表格列宽分配与行高、按设备像素比折算逻辑尺寸。
"""
import math
from typing import Callable, List, Sequence, Tuple, TypeVar

Measure = Callable[[str], float]
T = TypeVar('T')

# 不放在行首的标点（避头）：遇到时把上一行最后一个字带到下一行
_NO_LINE_START = set('，。、；：？！）》」』】〉,.;:?!)]}%')
# 折行时优先在这些字符之后断开（西文单词、路径不从中间劈开）
_BREAK_AFTER = set(' \\/-_·')


def _fits(text: str, width: float, measure: Measure) -> bool:
    return measure(text) <= width


def _break_index(line: str, next_char: str) -> int:
    """当前行放不下 next_char 时的断点（新行从该下标开始）。

    西文 / 数字连续串在中间被截断时，回退到串内最后一个空格或分隔符之后；
    找不到就硬断。下一字符是避头标点时把本行最后一个字一起带过去。
    """
    index = len(line)
    if next_char.isascii() and next_char.isalnum() and line and line[-1].isascii() \
            and line[-1].isalnum():
        for i in range(len(line) - 1, 0, -1):
            if line[i - 1] in _BREAK_AFTER:
                index = i
                break
            if not (line[i - 1].isascii()):
                index = i
                break
    if next_char in _NO_LINE_START and index == len(line) and len(line) > 1:
        index -= 1
    return index


def wrap_text(text: str, width: float, measure: Measure) -> List[str]:
    """
    按可用宽度折行；显式换行符照样换行。逐字累加，放不下时在 _break_index 给出的位置断开。
    一个字都放不下（宽度过窄）时每行至少放一个字，保证终止。空串返回 [""]。
    """
    if width <= 0:
        width = 1
    lines: List[str] = []
    for paragraph in (text or '').split('\n'):
        current = ''
        for char in paragraph:
            if not current or _fits(current + char, width, measure):
                current += char
                continue
            cut = _break_index(current, char)
            lines.append(current[:cut].rstrip(' '))
            current = (current[cut:] + char).lstrip(' ') if char != ' ' else current[cut:]
            # 极窄宽度下带过来的部分仍可能放不下：逐字硬断
            while len(current) > 1 and not _fits(current, width, measure):
                lines.append(current[:-1])
                current = current[-1]
        lines.append(current)
    return lines or ['']


def chunk(items: Sequence[T], columns: int) -> List[Tuple[T, ...]]:
    """按列数把序列切成行（行优先），最后一行可能不满。"""
    columns = max(1, columns)
    return [tuple(items[i:i + columns]) for i in range(0, len(items), columns)]


def column_widths(total: float, weights: Sequence[float]) -> List[int]:
    """按权重把总宽分成整数列宽，各列之和恰为 int(total)（余数从左到右各补 1）。"""
    if not weights:
        return []
    total_int = int(total)
    if sum(weights) <= 0:
        weights = [1.0] * len(weights)
    weight_sum = float(sum(weights))
    raw = [total_int * w / weight_sum for w in weights]
    widths = [int(math.floor(v)) for v in raw]
    remainder = total_int - sum(widths)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - widths[i], reverse=True)
    for i in order[:remainder]:
        widths[i] += 1
    return widths


def block_height(line_count: int, line_height: float) -> int:
    """若干行文字的高度（至少一行）。"""
    return int(math.ceil(max(1, line_count) * line_height))


def row_height(line_counts: Sequence[int], line_height: float, padding: float,
               minimum: float = 0.0) -> int:
    """表格 / 网格一行的高度：各格行数取最大，加上下内边距，不低于 minimum。"""
    lines = max([1, *line_counts])
    return int(math.ceil(max(minimum, lines * line_height + 2 * padding)))


def grid_shape(count: int, columns: int) -> Tuple[int, int]:
    """count 个格子排成网格的 (行数, 实际列数)；count 为 0 时为 (0, 0)。"""
    if count <= 0:
        return 0, 0
    used = max(1, min(columns, count))
    return (count + used - 1) // used, used


def logical_width(pixel_width: int, device_pixel_ratio: float) -> float:
    """输出图片像素宽对应的逻辑布局宽（像素宽 / 设备像素比）。"""
    return pixel_width / max(device_pixel_ratio, 1e-6)


def pixel_size(logical: float, device_pixel_ratio: float) -> int:
    """逻辑尺寸折算回像素（向上取整，至少 1）。"""
    return max(1, int(math.ceil(logical * device_pixel_ratio - 1e-6)))
