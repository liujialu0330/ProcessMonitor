"""
界面配色令牌（浅色 / 深色两套）

图表、迷你图、判定胶囊、任务卡片状态胶囊、打点徽标、空洞阴影、选中色条的颜色只在这里定义，
各控件按当前主题取用，避免同一种语义（"在涨"、"注意"、"数据"）在不同控件里各写一份、
深色主题下漏改。图表专用的组合（轴线、十字线、min/max 带等）见 ui/chart_theme.py，
也由这里的令牌派生。

底色说明：主窗口启用 Mica 时底色由系统合成，非 Mica 时为 Fluent 窗口底色（WINDOW）；
卡片（CardWidget）在其上叠一层半透明白，合成后约为 SURFACE，鼠标悬停时为 SURFACE_HOVER。
图表背景透明、由卡片提供底色，对比度一律对 SURFACE 与 SURFACE_HOVER 两者校验
（tests/test_color_contrast.py）：文字 ≥ 4.5:1，图形元素 ≥ 3:1。

每个令牌是 (浅色, 深色) 二元组；值为 "#rrggbb" 或样式表可用的 "rgba(r, g, b, a)"（a 为 0–1）。
"""
from typing import Dict, Optional, Sequence, Tuple

from PyQt5.QtGui import QColor
from qfluentwidgets import isDarkTheme, themeColor

from ui.color_contrast import GRAPHIC_MIN_CONTRAST, composite, contrast_ratio, to_qcolor

ThemePair = Tuple[str, str]

# ---------- 底色（只作对比度参照与导出图片的不透明底，屏幕上不直接画） ----------

# Fluent 窗口底色（与 tests/visual/snapshot.py 的截图容器一致）
WINDOW: ThemePair = ("#f3f3f3", "#202020")
# 卡片常态：窗口底 + 白色 170/255（浅）或 13/255（深）合成
SURFACE: ThemePair = ("#fbfbfb", "#2b2b2b")
# 卡片悬停：白色 64/255（浅）或 21/255（深）合成
SURFACE_HOVER: ThemePair = ("#f6f6f6", "#323232")

# ---------- 语义色（文字与图形，对卡片底 ≥ 4.5:1） ----------

# 正常 / 平稳
SUCCESS: ThemePair = ("#0F7B0F", "#6CCB5F")
# 注意 / 波动 / 暂停
CAUTION: ThemePair = ("#9D5D00", "#FCE100")
# 异常 / 持续上涨
CRITICAL: ThemePair = ("#C42B1C", "#FF99A4")
# 次要文字（已结束、正在停止）；浅色原为 #8D8D8D，对卡片底只有 3.2:1，加深到 4.9:1
TERTIARY: ThemePair = ("#6E6E6E", "#9E9E9E")
# 数据主色：曲线、迷你图折线；浅色原为 #0A8F8F（3.8:1），加深到 5.3:1
DATA: ThemePair = ("#087575", "#44D7D1")
# 打点注意色：打点竖线、编号徽标、迷你图刻度
ATTENTION: ThemePair = ("#C2410C", "#FFB454")
# 信息色：工具重启后续采的系统打点（与分析页多曲线系列里的蓝色同值，对卡片底 ≥ 4.5:1）
INFO: ThemePair = ("#2563EB", "#7AB8FF")

# 系统事件打点（被测进程异常退出 / 重启续接 / 工具重启后续采）：颜色取语义色，线型用点划线，
# 与用户打点（注意色虚线）区分；文字说明另有（颜色不是唯一线索）
EVENT_MARKER_COLORS: Dict[str, ThemePair] = {
    "crash": CRITICAL,
    "restart": SUCCESS,
    "recover": INFO,
}

# ---------- 胶囊 ----------

# 胶囊语气 → 底色（语义色的淡底）
PILL_BACKGROUNDS: Dict[str, ThemePair] = {
    "critical": ("rgba(196, 43, 28, 0.14)", "rgba(255, 153, 164, 0.22)"),
    "success": ("rgba(15, 123, 15, 0.14)", "rgba(108, 203, 95, 0.22)"),
    "caution": ("rgba(157, 93, 0, 0.14)", "rgba(252, 225, 0, 0.20)"),
    "neutral": ("rgba(0, 0, 0, 0.06)", "rgba(255, 255, 255, 0.10)"),
}
# 胶囊文字色。显式写进样式而不用 setEnabled 置灰：禁用态 QLabel 走调色板的 disabled
# 文字色，在深色主题的半透明胶囊底上几乎看不清
PILL_TEXT: ThemePair = ("rgba(0, 0, 0, 0.85)", "rgba(255, 255, 255, 0.90)")
# 置灰胶囊文字：仍须 ≥ 4.5:1（原 0.45 / 0.55 只有 3.2 / 3.8:1）
PILL_TEXT_DIMMED: ThemePair = ("rgba(0, 0, 0, 0.60)", "rgba(255, 255, 255, 0.70)")
# 表格置灰行文字（样本不足）：原 100/255 只有 2.7 / 3.3:1
DIMMED_TEXT: ThemePair = ("rgba(0, 0, 0, 0.59)", "rgba(255, 255, 255, 0.59)")

# 判定 → 胶囊语气（判定摘要表）
# 整体判定的"被测进程异常退出"取最严重的语气；提前退出（测试没做完）是警示
VERDICT_TONES: Dict[str, str] = {"持续上涨": "critical", "平稳": "success", "波动": "caution",
                                 "异常退出": "critical", "提前退出": "caution"}

# ---------- 选中 / 悬停 ----------

# 指标格悬停与选中的淡底（任务卡片）
CELL_HIGHLIGHT: ThemePair = ("rgba(0, 0, 0, 0.03)", "rgba(255, 255, 255, 0.05)")


def theme_index(dark: Optional[bool] = None) -> int:
    """二元组下标：0 浅色、1 深色；dark 为 None 时按当前主题。"""
    if dark is None:
        dark = isDarkTheme()
    return 1 if dark else 0


def pick(pair: ThemePair, dark: Optional[bool] = None) -> str:
    """按主题从 (浅色, 深色) 中取一个。"""
    return pair[theme_index(dark)]


def qcolor(pair: ThemePair, dark: Optional[bool] = None) -> QColor:
    """按主题取色并转成 QColor（支持 rgba(...) 字符串）。"""
    return to_qcolor(pick(pair, dark))


def colors(pair: ThemePair) -> Tuple[QColor, QColor]:
    """(浅色 QColor, 深色 QColor)，供 setTextColor(light, dark) 这类接口。"""
    return to_qcolor(pair[0]), to_qcolor(pair[1])


def pill_background(tone: str, dark: Optional[bool] = None) -> str:
    """胶囊底色（未知语气按中性）。"""
    return pick(PILL_BACKGROUNDS.get(tone, PILL_BACKGROUNDS["neutral"]), dark)


def verdict_tone(verdict: str) -> str:
    """判定文字对应的胶囊语气；其余判定（数据不足等）为中性。"""
    return VERDICT_TONES.get(verdict, "neutral")


def ensure_contrast(color, backgrounds: Sequence, minimum: float, dark: bool) -> QColor:
    """把 color 逐步加深（浅色主题）或提亮（深色主题），直到对每个底色都达到 minimum；
    本来就达标时原样返回。用于强调色这类由用户/库决定、不能写死的颜色。"""
    result = QColor(to_qcolor(color))
    for _ in range(40):
        if all(contrast_ratio(result, bg) >= minimum for bg in backgrounds):
            return result
        result = result.lighter(106) if dark else result.darker(106)
    return result


def selection_bar_backgrounds(dark: Optional[bool] = None) -> Tuple[QColor, QColor]:
    """选中色条实际所在的底：指标格淡底叠在卡片常态 / 悬停底上。"""
    index = theme_index(dark)
    highlight = CELL_HIGHLIGHT[index]
    return composite(highlight, SURFACE[index]), composite(highlight, SURFACE_HOVER[index])


def selection_bar_color(dark: Optional[bool] = None) -> QColor:
    """选中色条（任务卡片指标格左缘）：强调色，对所在底不足 3:1 时自动加深 / 提亮。"""
    if dark is None:
        dark = isDarkTheme()
    return ensure_contrast(themeColor(), selection_bar_backgrounds(dark),
                           GRAPHIC_MIN_CONTRAST, dark)
