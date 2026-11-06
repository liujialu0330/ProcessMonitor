"""
任务卡片：身份行（进程名 ●状态 · PID · 每 N 秒 · 已运行 hh:mm:ss · 备注）、状态胶囊配色、
"较基线 / 比开始时（N 分钟前）/ 基线采样中"文案、底部"已运行 · 每 N 秒 · N 次采集"、
迷你图打点刻度、操作按钮文案（打点 / 分析 / 暂停 / 停止；已结束：分析 / 重新监控 / ✕）。
时间一律由调用方传入（卡片不读系统时间）。
"""
from datetime import datetime, timedelta

import pytest
from PyQt5.QtGui import QColor

from core.contracts import StopReason, UnavailableReason
from data.models import Marker
from tests.helpers import dispose_widget
from ui.components.task_card import (
    BASELINE_SAMPLING_TEXT, STATUS_STYLES, STOP_TOOLTIP, TaskCard, format_ago,
    format_clock, format_elapsed, format_interval,
)
from utils.metrics import MetricType

T0 = datetime(2026, 11, 6, 9, 0, 0)
PRIVATE, GDI = MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS
MB = 1024.0      # 内存类指标以 KB 为单位


@pytest.fixture
def card(qapp):
    widget = TaskCard("t1", "demo.exe", 1234, [PRIVATE, GDI], interval=1.0,
                      note="建模 200 次")
    widget.resize(900, 300)
    widget.show()
    yield widget
    dispose_widget(widget)


def _colors(label):
    return label.lightColor.name(), label.darkColor.name()


# ---------- 文案小工具 ----------

@pytest.mark.parametrize("seconds, clock, elapsed, ago", [
    (0, "00:00:00", "0 秒", "0 秒前"),
    (45, "00:00:45", "45 秒", "45 秒前"),
    (135, "00:02:15", "2 分 15 秒", "2 分钟前"),
    (720, "00:12:00", "12 分 0 秒", "12 分钟前"),
    (3 * 3600 + 5 * 60 + 9, "03:05:09", "3 小时 5 分", "3 小时 5 分钟前"),
    (26 * 3600, "26:00:00", "26 小时 0 分", "26 小时 0 分钟前"),
])
def test_time_texts(seconds, clock, elapsed, ago):
    assert format_clock(seconds) == clock
    assert format_elapsed(seconds) == elapsed
    assert format_ago(seconds) == ago


def test_interval_text():
    assert format_interval(1.0) == "每 1 秒"
    assert format_interval(0.5) == "每 0.5 秒"
    assert format_interval(None) == ""


# ---------- 身份行与状态胶囊 ----------

def test_identity_line_before_and_after_start(card):
    assert card.title_label.text() == "demo.exe"
    assert card.status_label.text() == "● 运行中"
    assert card.meta_label.text() == "· PID 1234 · 每 1 秒"
    assert card.note_label.text() == "· 建模 200 次" and not card.note_label.isHidden()

    card.set_started_at(T0)
    card.tick(T0 + timedelta(seconds=135))
    assert card.meta_label.text() == "· PID 1234 · 每 1 秒 · 已运行 00:02:15"


def test_no_note_hides_note_label(qapp):
    card = TaskCard("t2", "x.exe", 1, [PRIVATE])
    try:
        assert card.note_label.isHidden()
        assert card.meta_label.text() == "· PID 1"     # 未知间隔不显示"每 N 秒"
    finally:
        dispose_widget(card)


def test_status_pill_colors_follow_design_spec(card):
    assert _colors(card.status_label) == ("#0f7b0f", "#6ccb5f")

    card.set_paused(True)
    assert card.status_label.text() == "● 已暂停"
    assert _colors(card.status_label) == ("#9d5d00", "#fce100")

    card.set_paused(False)
    assert card.status_label.text() == "● 运行中"

    card.set_finished(StopReason.PROCESS_EXITED, T0)
    assert card.status_label.text() == "● 异常结束"
    assert _colors(card.status_label) == ("#c42b1c", "#ff99a4")


def test_user_stop_uses_tertiary_text_color(card):
    card.set_finished(StopReason.USER, T0)
    _, light, dark = STATUS_STYLES['finished']
    assert card.status_label.text() == "● 已结束"
    assert _colors(card.status_label) == (QColor(light).name(), QColor(dark).name())


def test_stopping_disables_actions_but_keeps_analysis(card):
    card.set_stopping()
    assert card.status_label.text() == "● 正在停止…"
    assert not card.marker_button.isEnabled() and not card.stop_button.isEnabled()
    assert card.analysis_button.isEnabled()


# ---------- 底部计数行 ----------

def test_footer_shows_running_time_interval_and_count(card):
    assert card.count_label.text() == "每 1 秒 · 0 次采集"
    card.set_started_at(T0)
    for i in range(3):
        card.update_values({PRIVATE: 1.0 * MB}, T0 + timedelta(seconds=i + 1))
        card.increment_count()
    card.tick(T0 + timedelta(seconds=135))
    assert card.count_label.text() == "已运行 2 分 15 秒 · 每 1 秒 · 3 次采集"


def test_running_time_freezes_at_end_time(card):
    card.set_started_at(T0)
    card.set_finished(StopReason.USER, T0 + timedelta(minutes=5))
    card.tick(T0 + timedelta(hours=1))      # 结束后再收到 tick 也不变
    assert "已运行 00:05:00" in card.meta_label.text()
    assert card.count_label.text().startswith("已运行 5 分 0 秒")


# ---------- 变化量文案 ----------

def test_since_start_text_with_relative_time(card):
    assert card.delta_labels[PRIVATE].text() == "比开始时 --"
    card.update_values({PRIVATE: 100 * MB, GDI: 100.0}, T0)
    card.update_values({PRIVATE: 185.23 * MB, GDI: 103.0}, T0 + timedelta(minutes=12))

    assert card.delta_labels[PRIVATE].text() == "比开始时（12 分钟前）+85.23 MB"
    assert card.delta_labels[GDI].text() == "比开始时（12 分钟前）+3 个"
    assert card.trend_delta_label.text() == card.delta_labels[PRIVATE].text()

    card.tick(T0 + timedelta(minutes=12, seconds=30))
    assert card.delta_labels[GDI].text() == "比开始时（12 分钟前）+3 个"
    card.tick(T0 + timedelta(seconds=40))
    assert card.delta_labels[GDI].text() == "比开始时（40 秒前）+3 个"


def test_without_time_falls_back_to_plain_since_start(qapp):
    card = TaskCard("t3", "x.exe", 1, [GDI])
    try:
        card.update_values({GDI: 10.0})
        card.update_values({GDI: 12.0})
        assert card.delta_labels[GDI].text() == "比开始时 +2 个"
    finally:
        dispose_widget(card)


def test_baseline_texts(card):
    card.update_values({PRIVATE: 100 * MB, GDI: 100.0}, T0)
    card.set_baseline(T0 + timedelta(minutes=1))
    assert card.delta_labels[PRIVATE].text() == BASELINE_SAMPLING_TEXT == "基线采样中"
    assert "09:01:00" in card.delta_labels[PRIVATE].toolTip()

    card.set_baseline_value(PRIVATE, 100 * MB)
    card.update_values({PRIVATE: 185.23 * MB, GDI: 110.0}, T0 + timedelta(minutes=5))
    assert card.delta_labels[PRIVATE].text() == "较基线 +85.23 MB"
    assert card.delta_labels[GDI].text() == "基线采样中"
    assert card.trend_delta_label.text() == "较基线 +85.23 MB"

    card.clear_baseline()
    assert card.baseline is None
    assert card.delta_labels[GDI].text() == "比开始时（5 分钟前）+10 个"


def test_set_baseline_with_values_and_unavailable_metric(card):
    card.update_values({PRIVATE: 3 * MB, GDI: 5.0}, T0)
    card.set_baseline(T0, {PRIVATE: 1 * MB, GDI: 5.0})
    assert card.delta_labels[PRIVATE].text() == "较基线 +2 MB"
    assert card.delta_labels[GDI].text() == "较基线 ±0"

    card.mark_unavailable({GDI: UnavailableReason.ACCESS_DENIED})
    assert card.delta_labels[GDI].text() == "较基线 --"
    card.update_values({GDI: 7.0}, T0 + timedelta(seconds=1))
    assert card.delta_labels[GDI].text() == "较基线 +2 个"


def test_set_baseline_value_without_baseline_is_ignored(card):
    card.set_baseline_value(PRIVATE, 1.0)
    assert card.baseline is None


# ---------- 迷你图刻度 ----------

def test_sparkline_ticks_for_baseline_and_user_markers(card):
    for i in range(5):
        card.update_values({PRIVATE: float(i)}, T0 + timedelta(seconds=i))
    card.set_markers([
        Marker("t1", T0 + timedelta(seconds=1), "基线", 1, "baseline"),
        Marker("t1", T0 + timedelta(seconds=3), "第 1 轮", 2, "user"),
        Marker("t1", T0 + timedelta(seconds=2), "暂停", 3, "pause"),   # 系统打点不画
    ])
    assert card.sparkline.tick_positions() == [(0.25, "baseline"), (0.75, "user")]
    card.sparkline.grab()     # 带刻度绘制不抛异常


# ---------- 按钮与文案 ----------

def test_action_buttons_text(card):
    assert [b.text() for b in (card.marker_button, card.analysis_button,
                               card.pause_button, card.stop_button)] == [
        "打点", "分析", "暂停", "停止"]
    assert card.stop_button.toolTip() == STOP_TOOLTIP
    assert STOP_TOOLTIP == "停止后不能继续记录到这个任务；要继续请“重新监控”"

    card.set_finished(StopReason.USER, T0)
    assert card.history_button.text() == "分析"
    assert card.restart_button.text() == "重新监控"
    assert card.close_button.toolTip() == "从列表移除"
    assert not card.close_button.isHidden()


def test_analysis_button_emits_history_requested_while_running(card):
    received = []
    card.history_requested.connect(received.append)
    card.analysis_button.click()
    assert received == ["t1"]
