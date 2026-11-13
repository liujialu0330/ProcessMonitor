"""
WCAG 对比度纯函数（ui/color_contrast.py）与两套配色逐项对比度（ui/chart_theme.py、
ui/theme_tokens.py）：文字 ≥ 4.5:1，图形元素 ≥ 3:1。

底色一律对卡片常态与悬停两种校验（图表背景透明，底色来自卡片）；半透明的层（min/max 带、
空洞阴影、概览条选区、胶囊底）先合成到下面那层再算。
"""
import pytest
from PyQt5.QtGui import QColor
from qfluentwidgets import Theme, qconfig

from ui import theme_tokens as tokens
from ui.chart_theme import (BAND_ALPHA, GAP_SHADE_ALPHA, SELECTION_ALPHA, chart_palette,
                            series_colors)
from ui.color_contrast import (GRAPHIC_MIN_CONTRAST, TEXT_MIN_CONTRAST, composite,
                               contrast_ratio, relative_luminance, to_qcolor)

TEXT = TEXT_MIN_CONTRAST
GRAPHIC = GRAPHIC_MIN_CONTRAST
THEMES = [pytest.param(False, id="浅色"), pytest.param(True, id="深色")]


# ---------- 纯函数 ----------

def test_relative_luminance_extremes():
    assert relative_luminance("#000000") == pytest.approx(0.0)
    assert relative_luminance("#ffffff") == pytest.approx(1.0)
    assert relative_luminance(QColor(255, 255, 255)) == pytest.approx(1.0)
    # WCAG 例：中灰 #777777 的相对亮度约 0.184
    assert relative_luminance("#777777") == pytest.approx(0.1844, abs=1e-3)


def test_contrast_ratio_known_values_and_symmetry():
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0)
    assert contrast_ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    assert contrast_ratio("#ffffff", "#767676") == pytest.approx(
        contrast_ratio("#767676", "#ffffff"))


def test_rgba_parsing_and_composite():
    half = to_qcolor("rgba(0, 0, 0, 0.5)")
    assert (half.red(), half.alpha()) == (0, 128)
    assert to_qcolor("rgba(255, 255, 255, 100)").alpha() == 100
    assert to_qcolor("rgb(10, 20, 30)").name() == "#0a141e"
    # 0.6 × 255 = 153，正好可逆：白底上合成为 255 × 0.4 = 102
    assert composite("rgba(0, 0, 0, 0.6)", "#ffffff").name() == "#666666"
    assert composite("#123456", "#ffffff").name() == "#123456"
    # 半透明前景先合成再算：与手工合成的结果一致
    assert contrast_ratio("rgba(0, 0, 0, 0.6)", "#ffffff") == pytest.approx(
        contrast_ratio("#666666", "#ffffff"))
    with pytest.raises(ValueError):
        to_qcolor("不是颜色")
    with pytest.raises(ValueError):
        contrast_ratio("#000000", "rgba(255, 255, 255, 0.5)")


# ---------- 图表 palette ----------

def _layer(base: str, color: str, alpha: int) -> QColor:
    layer = QColor(color)
    layer.setAlpha(alpha)
    return composite(layer, base)


def _backgrounds(palette: dict, spec: str) -> list:
    """底色规格 → 实际底色列表。卡片相关的规格同时给出常态与悬停两种。"""
    cards = (palette['surface'], palette['surface_hover'])
    if spec == 'card':
        return list(cards)
    if spec == 'window':
        return [palette['window']]
    if spec == 'band':
        return [_layer(bg, palette['curve'], BAND_ALPHA) for bg in cards]
    if spec == 'gap':
        return [_layer(bg, palette['gap_shade'], GAP_SHADE_ALPHA) for bg in cards]
    if spec == 'selection':
        return [_layer(palette['window'], palette['selection'], SELECTION_ALPHA)]
    raise AssertionError(spec)


# (palette 键, 底色规格, 门槛, 说明)
CHART_REQUIREMENTS = [
    ('text', 'card', TEXT, "刻度文字、轴标题"),
    ('text', 'gap', TEXT, "空洞说明文字压在空洞阴影上"),
    ('text', 'window', TEXT, "概览条时间轴文字（概览条直接在窗口底上）"),
    ('axis', 'card', GRAPHIC, "坐标轴线与刻度线"),
    ('axis', 'window', GRAPHIC, "概览条时间轴线"),
    ('curve', 'card', TEXT, "曲线与迷你图同色，按文字门槛"),
    ('curve', 'band', GRAPHIC, "中心线压在 min/max 带上"),
    ('crosshair', 'card', GRAPHIC, "悬停十字线"),
    ('marker', 'card', TEXT, "打点编号徽标（文字）与竖线"),
    ('marker', 'band', GRAPHIC, "打点竖线穿过 min/max 带"),
    ('marker', 'gap', GRAPHIC, "打点竖线落在空洞阴影里"),
    ('system_marker', 'card', GRAPHIC, "暂停/恢复短竖线"),
    ('crash_marker', 'card', TEXT, "异常退出点划线与底部文字标签"),
    ('crash_marker', 'band', GRAPHIC, "异常退出线穿过 min/max 带"),
    ('crash_marker', 'gap', GRAPHIC, "异常退出线落在空洞阴影里（崩溃后常有空洞）"),
    ('restart_marker', 'card', TEXT, "重启续接点划线与底部文字标签"),
    ('restart_marker', 'band', GRAPHIC, "重启续接线穿过 min/max 带"),
    ('restart_marker', 'gap', GRAPHIC, "重启续接线落在空洞阴影里"),
    ('recover_marker', 'card', TEXT, "续采点划线与底部文字标签"),
    ('recover_marker', 'band', GRAPHIC, "续采线穿过 min/max 带"),
    ('recover_marker', 'gap', GRAPHIC, "续采线落在空洞阴影里（工具重启前后的空洞）"),
    ('selection', 'window', GRAPHIC, "概览条选区边线（选中色条）"),
    ('overview_curve', 'window', GRAPHIC, "概览条走势线"),
    ('overview_curve', 'selection', GRAPHIC, "概览条走势线在选区填充内"),
]


@pytest.mark.parametrize("dark", THEMES)
@pytest.mark.parametrize("key, spec, minimum, _why", CHART_REQUIREMENTS,
                         ids=[f"{k}@{s}" for k, s, _m, _w in CHART_REQUIREMENTS])
def test_chart_palette_contrast(dark, key, spec, minimum, _why):
    palette = chart_palette(dark)
    for background in _backgrounds(palette, spec):
        ratio = contrast_ratio(palette[key], background)
        assert ratio >= minimum, (
            f"{key} {palette[key]} 对 {QColor(background).name()} 只有 {ratio:.2f}:1"
            f"（要求 {minimum}:1，{_why}）")


@pytest.mark.parametrize("dark", THEMES)
def test_series_colors_contrast(dark):
    palette = chart_palette(dark)
    for color in series_colors(dark):
        for background in _backgrounds(palette, 'card'):
            assert contrast_ratio(color, background) >= GRAPHIC, (color, background)


def test_palettes_cover_same_keys_and_differ():
    light, dark = chart_palette(False), chart_palette(True)
    assert set(light) == set(dark)
    for key in ('text', 'axis', 'curve', 'crosshair', 'marker', 'system_marker',
                'overview_curve', 'selection', 'crash_marker', 'restart_marker',
                'recover_marker'):
        assert light[key] != dark[key], key
    # 三种系统事件互相区分，也与用户打点、曲线不同色
    for palette in (light, dark):
        colors = [palette[k] for k in ('crash_marker', 'restart_marker', 'recover_marker',
                                       'marker', 'curve')]
        assert len(set(colors)) == len(colors)
    # 导出图片的不透明底 = 卡片常态底色
    assert light['background'] == tokens.SURFACE[0]
    assert dark['background'] == tokens.SURFACE[1]


# ---------- 语义色、胶囊、迷你图、置灰 ----------

def _card_backgrounds(dark: bool) -> list:
    i = 1 if dark else 0
    return [tokens.SURFACE[i], tokens.SURFACE_HOVER[i]]


# (令牌名, 门槛, 说明) —— 均画在卡片底上
TOKEN_REQUIREMENTS = [
    ('SUCCESS', TEXT, "状态：运行中"),
    ('CAUTION', TEXT, "状态：已暂停"),
    ('CRITICAL', TEXT, "状态：异常结束"),
    ('TERTIARY', TEXT, "状态：已结束 / 正在停止"),
    ('DATA', TEXT, "迷你图折线（要求 ≥ 4.5:1）"),
    ('ATTENTION', TEXT, "迷你图打点刻度、徽标"),
    ('INFO', TEXT, "续采打点（图表标签是文字、迷你图刻度）"),
    ('DIMMED_TEXT', TEXT, "摘要表置灰行文字"),
]


@pytest.mark.parametrize("dark", THEMES)
@pytest.mark.parametrize("name, minimum, _why", TOKEN_REQUIREMENTS,
                         ids=[n for n, _m, _w in TOKEN_REQUIREMENTS])
def test_token_contrast_on_card(dark, name, minimum, _why):
    color = tokens.pick(getattr(tokens, name), dark)
    for background in _card_backgrounds(dark):
        ratio = contrast_ratio(color, background)
        assert ratio >= minimum, f"{name} {color} 对 {background} 只有 {ratio:.2f}:1（{_why}）"


@pytest.mark.parametrize("dark", THEMES)
@pytest.mark.parametrize("tone", sorted(tokens.PILL_BACKGROUNDS))
@pytest.mark.parametrize("text_token", ["PILL_TEXT", "PILL_TEXT_DIMMED"])
def test_pill_text_contrast(dark, tone, text_token):
    text = tokens.pick(getattr(tokens, text_token), dark)
    for card in _card_backgrounds(dark):
        background = composite(tokens.pill_background(tone, dark), card)
        ratio = contrast_ratio(text, background)
        assert ratio >= TEXT, f"{text_token} 在 {tone} 胶囊上只有 {ratio:.2f}:1"


def test_pill_dimmed_text_still_distinguishable():
    # 置灰仍要看得出来：置灰文字明显比正常文字淡
    for i in (0, 1):
        assert to_qcolor(tokens.PILL_TEXT_DIMMED[i]).alpha() < to_qcolor(
            tokens.PILL_TEXT[i]).alpha() - 40


def test_verdict_and_status_share_tones():
    """判定胶囊与任务卡片状态胶囊同源：同一语气取同一组颜色。"""
    from ui.components.task_card import STATUS_STYLES
    assert tokens.verdict_tone("持续上涨") == "critical"
    assert tokens.verdict_tone("平稳") == "success"
    assert tokens.verdict_tone("波动") == "caution"
    assert tokens.verdict_tone("数据不足") == "neutral"
    pairs = {'running': tokens.SUCCESS, 'paused': tokens.CAUTION,
             'abnormal': tokens.CRITICAL, 'finished': tokens.TERTIARY,
             'waiting_restart': tokens.CAUTION}
    for status, pair in pairs.items():
        _text, light, dark = STATUS_STYLES[status]
        assert (light.name(), dark.name()) == (QColor(pair[0]).name(), QColor(pair[1]).name())
    # 胶囊底是同一语义色的淡底
    for tone, pair in (("critical", tokens.CRITICAL), ("success", tokens.SUCCESS),
                       ("caution", tokens.CAUTION)):
        for i in (0, 1):
            fill = to_qcolor(tokens.PILL_BACKGROUNDS[tone][i])
            fill.setAlpha(255)
            assert fill.name() == QColor(pair[i]).name()


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_selection_bar_contrast(qapp, theme):
    """选中色条：强调色对指标格淡底（叠在卡片常态 / 悬停底上）≥ 3:1，不足时自动加深 / 提亮。"""
    original = qconfig._theme
    try:
        qconfig._theme = theme
        dark = theme == Theme.DARK
        bar = tokens.selection_bar_color()
        for background in tokens.selection_bar_backgrounds(dark):
            assert contrast_ratio(bar, background) >= GRAPHIC
    finally:
        qconfig._theme = original


def test_ensure_contrast_adjusts_only_when_needed():
    ok = tokens.ensure_contrast("#000000", ["#ffffff"], GRAPHIC, dark=False)
    assert ok.name() == "#000000"
    fixed = tokens.ensure_contrast("#cccccc", ["#ffffff"], GRAPHIC, dark=False)
    assert contrast_ratio(fixed, "#ffffff") >= GRAPHIC
    brightened = tokens.ensure_contrast("#333333", ["#202020"], GRAPHIC, dark=True)
    assert contrast_ratio(brightened, "#202020") >= GRAPHIC


# ---------- 报告图片版面（ui/widgets/analysis/report_sheet.report_palette） ----------

# (palette 键, 底色规格, 门槛, 说明)；底色 page 为页面底，header 为表头淡底叠在页面底上
REPORT_REQUIREMENTS = [
    ('text', 'page', TEXT, "正文、表格数值"),
    ('secondary', 'page', TEXT, "范围行、元信息标签、说明小字、页脚"),
    ('secondary', 'header', TEXT, "表头文字"),
    ('dimmed', 'page', TEXT, "结论表置灰行（样本不足）"),
]


@pytest.mark.parametrize("dark", THEMES)
@pytest.mark.parametrize("key, spec, minimum, _why", REPORT_REQUIREMENTS,
                         ids=[f"{k}@{s}" for k, s, _m, _w in REPORT_REQUIREMENTS])
def test_report_palette_contrast(dark, key, spec, minimum, _why):
    from ui.widgets.analysis.report_sheet import report_palette
    palette = report_palette(dark)
    background = (palette['page'] if spec == 'page'
                  else composite(palette['header'], palette['page']))
    ratio = contrast_ratio(palette[key], background)
    assert ratio >= minimum, f"{key} {palette[key]} 只有 {ratio:.2f}:1（{_why}）"


@pytest.mark.parametrize("dark", THEMES)
@pytest.mark.parametrize("verdict", ["持续上涨", "平稳", "波动", "数据不足"])
def test_report_verdict_pill_contrast(dark, verdict):
    """报告里的判定胶囊画在页面底上：胶囊文字对（胶囊底叠页面底）≥ 4.5:1。"""
    from ui.widgets.analysis.report_sheet import report_palette
    page = report_palette(dark)['page']
    background = composite(tokens.pill_background(tokens.verdict_tone(verdict), dark), page)
    assert contrast_ratio(tokens.pick(tokens.PILL_TEXT, dark), background) >= TEXT


def test_report_palette_is_light_by_default_and_from_tokens():
    from ui.widgets.analysis.report_sheet import report_palette
    light = report_palette()
    assert light['page'] == tokens.SURFACE[0] == chart_palette(False)['background']
    assert light['text'] == tokens.PILL_TEXT[0]
    assert light['secondary'] == chart_palette(False)['text']
    assert report_palette(True)['page'] == tokens.SURFACE[1]
