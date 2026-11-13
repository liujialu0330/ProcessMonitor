"""
结论条：文案规则（ui.formatting.verdict_summary / refresh_status_text）与控件
（胶囊配色取自主题令牌并随主题切换、依据与自动刷新状态的显隐）。
"""
from datetime import datetime, timedelta

import pytest
from qfluentwidgets import Theme, qconfig

from core.analysis.trend import Judgement, LinearFit, RateText, Verdict
from core.history_query import HistorySnapshot, build_request
from ui import theme_tokens as tokens
from ui.formatting import (
    VerdictSummary, rate_brief, refresh_status_text, verdict_summary)
from ui.widgets.analysis.verdict_banner import VerdictBanner
from utils.metrics import MetricType

MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
GDI = MetricType.NUM_GDI_OBJECTS
USER = MetricType.NUM_USER_OBJECTS
THREADS = MetricType.NUM_THREADS
IO = MetricType.IO_READ_BYTES


def _snap(metric, verdict=None, rate=None, span=1200.0, count=120, interval=10.0):
    judgement = (Judgement(verdict, LinearFit(0.0, 0.0, 0.9, 0.0), 'x', 0.5)
                 if verdict is not None else None)
    stats = {'count': count, 'first': 1.0, 'last': 1.0, 'span_seconds': span}
    return HistorySnapshot(request=build_request("t", metric, 'all'), stats=stats,
                           judgement=judgement, rate=rate, interval_seconds=interval)


def _rate(value, suffix='/小时'):
    return RateText(value, value * 0.1, suffix, 3600.0)


def test_rising_lists_names_with_rates_without_ci():
    items = [(MEM, _snap(MEM, Verdict.RISING, _rate(35 * 1024.0))),
             (HANDLES, _snap(HANDLES, Verdict.RISING, _rate(120.0))),
             (GDI, _snap(GDI, Verdict.STABLE)), (USER, _snap(USER, Verdict.STABLE)),
             (THREADS, _snap(THREADS, Verdict.STABLE))]
    summary = verdict_summary(items)
    assert summary == VerdictSummary(
        "持续上涨", "2 / 5 项持续上涨：私有字节 +35 MB/小时、句柄数 +120 个/小时",
        summary.tooltip)
    assert "平稳：GDI对象数、USER对象数、线程数" in summary.tooltip
    assert rate_brief(MEM, _rate(35 * 1024.0)) == "+35 MB/小时"


def test_rising_with_fluctuating_and_long_list():
    items = [(m, _snap(m, Verdict.RISING, _rate(10.0))) for m in (HANDLES, GDI, USER, THREADS)]
    items.append((MEM, _snap(MEM, Verdict.FLUCTUATING)))
    sentence = verdict_summary(items).sentence
    assert sentence.startswith("4 / 5 项持续上涨：句柄数 +10 个/小时、")
    assert "等 1 项" in sentence and sentence.endswith("；另 1 项波动")


@pytest.mark.parametrize("items, expected", [
    ([(MEM, _snap(MEM, Verdict.FLUCTUATING)), (GDI, _snap(GDI, Verdict.STABLE))],
     ("波动", "1 / 2 项波动：私有字节")),
    ([(m, _snap(m, Verdict.STABLE)) for m in (MEM, HANDLES, GDI, USER, THREADS)],
     ("平稳", "5 项指标均平稳")),
    ([(MEM, _snap(MEM, Verdict.STABLE)), (GDI, _snap(GDI, Verdict.INSUFFICIENT)), (IO, _snap(IO))],
     ("平稳", "1 项平稳，1 项数据不足")),
    ([(MEM, _snap(MEM, Verdict.INSUFFICIENT, span=480)), (HANDLES, None)],
     ("数据不足", "数据不足：已采 8分钟 / 需 ≥ 10分钟")),
    ([(MEM, _snap(MEM, Verdict.INSUFFICIENT, span=900, count=12, interval=1.0))],
     ("数据不足", "数据不足：仅 12 点 / 需 ≥ 30 点")),
    ([(MEM, None)], ("数据不足", "数据不足：所选范围内没有数据")),
    ([(IO, _snap(IO))], ("", "所选指标均为累计量或峰值类，不做趋势判定")),
])
def test_summary_rules(items, expected):
    summary = verdict_summary(items)
    assert (summary.verdict, summary.sentence) == expected


@pytest.mark.parametrize("seconds, text", [
    (0.4, "自动刷新 · 刚刚"), (15, "自动刷新 · 15 秒前"), (125, "自动刷新 · 2 分钟前")])
def test_refresh_status_text(seconds, text):
    assert refresh_status_text(seconds) == text


@pytest.fixture
def banner(qapp):
    widget = VerdictBanner()
    yield widget
    widget.close()


def test_banner_shows_summary_basis_and_refresh(banner):
    banner.show()
    assert not banner.pill.isVisible() and banner.sentence() == ""
    banner.set_summary(VerdictSummary("持续上涨", "1 / 3 项持续上涨：私有字节 +2 MB/分", "提示"))
    banner.set_basis("基线 16:37:33 起 · 15分钟 · 94 点")
    assert banner.pill.isVisible() and banner.verdict() == "持续上涨"
    assert banner.basis() == "依据：基线 16:37:33 起 · 15分钟 · 94 点"
    assert "提示" in banner.sentence_label.toolTip()
    assert not banner.refresh_label.isVisible()
    banner.set_refresh_status("自动刷新 · 15 秒前")
    assert banner.refresh_label.isVisible() and banner.refresh_status() == "自动刷新 · 15 秒前"
    banner.clear()
    assert (banner.verdict(), banner.sentence(), banner.basis(), banner.refresh_status()) == \
        ("", "", "", "")
    assert not banner.pill.isVisible() and not banner.basis_label.isVisible()


def test_banner_pill_colors_follow_theme_tokens(banner):
    banner.set_summary(VerdictSummary("持续上涨", "x"))
    light = banner.pill.styleSheet()
    assert tokens.pill_background("critical") in light
    original = qconfig._theme
    try:
        qconfig._theme = Theme.DARK
        qconfig.themeChanged.emit(Theme.DARK)
        dark = banner.pill.styleSheet()
        assert dark != light and tokens.pill_background("critical", dark=True) in dark
    finally:
        qconfig._theme = original
        qconfig.themeChanged.emit(original)
    banner.set_summary(VerdictSummary("平稳", "x"))
    assert tokens.pill_background("success") in banner.pill.styleSheet()
