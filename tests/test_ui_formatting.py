"""
ui/formatting.py 纯函数：时长、任务下拉文案、状态词、判定摘要行与判定/增速文案、
打点区间与编号徽标。
"""
import math
from datetime import datetime, timedelta

import pytest

from core.analysis.intervals import RoundDelta
from core.analysis.trend import Judgement, LinearFit, RateText, Verdict
from core.contracts import StopReason, UnavailableReason
from core.history_query import (
    BaselineInfo, HistorySnapshot, PeakRises, RateStats, build_request)
from data.models import Marker, MonitorTask
from ui.formatting import (
    basis_text, build_summary_row, format_count, format_duration, format_per_second, format_rate_text,
    format_task_option_text, format_task_tooltip, insufficient_text,
    judgement_reason_text, marker_badge, marker_context, object_usage_text,
    is_abnormal_stop, stop_reason_text, task_duration_seconds, task_status_text,
    truncate_text, unavailable_reason_text, verdict_text
)
from utils.metrics import (
    VALUE_CLASS_PEAK, VALUE_CLASS_RATE, MetricType)

# 已结束任务的时长不看 now；随便给一个远离起止时间的时刻，证明确实没被用到
FAR_NOW = datetime(2030, 1, 1)


def _task(**overrides) -> MonitorTask:
    defaults = dict(
        task_id="t1", pid=1234, process_name="MPrintExp.exe",
        metric_types=[MetricType.MEMORY_PRIVATE], interval=1.0,
        start_time=datetime(2026, 7, 16, 20, 54, 51),
        end_time=datetime(2026, 7, 16, 23, 10, 0), status="stopped", note="")
    defaults.update(overrides)
    return MonitorTask(**defaults)


def test_format_count_thousands():
    assert format_count(0) == "0"
    assert format_count(1234567) == "1,234,567"


def test_format_duration_levels():
    assert format_duration(45) == "45秒"
    assert format_duration(12 * 60 + 30) == "12分钟"
    assert format_duration(2 * 3600 + 15 * 60) == "2小时15分"
    assert format_duration(27 * 3600) == "1天3小时"
    assert format_duration(-5) == "0秒"


def test_truncate_text():
    assert truncate_text("短备注", 10) == "短备注"
    assert truncate_text("a" * 20, 5) == "aaaaa…"
    assert truncate_text(None, 5) == ""


def test_task_option_text_contains_all_parts_and_truncates_note():
    note = "建模 200 次回归测试，连续运行过夜观察内存"
    text = format_task_option_text(
        _task(note=note), running=False, now=FAR_NOW, note_max=8)
    parts = text.split(" · ")
    assert parts[0] == "MPrintExp.exe"
    assert parts[1] == "PID 1234"
    assert parts[2] == "07-16 20:54"
    assert parts[3] == "2小时15分"
    assert parts[4] == note[:8] + "…"
    # 完整备注在 tooltip
    assert note in format_task_tooltip(_task(note=note), False, FAR_NOW)


def test_task_option_text_marks_running_and_uses_now_for_duration():
    now = datetime(2026, 7, 16, 21, 54, 51)
    text = format_task_option_text(
        _task(end_time=None, status="running"), running=True, now=now)
    assert "运行中 1小时0分" in text
    assert text.count(" · ") == 3  # 无备注时不带备注段


def test_task_option_text_full_date():
    text = format_task_option_text(_task(), False, FAR_NOW, full_date=True)
    assert "2026-07-16 20:54" in text


def test_task_duration_running_uses_given_now_only():
    """运行中时长只取调用方传入的 now，模块内不再自取系统时间"""
    task = _task(end_time=None, status="running")
    now = datetime(2026, 7, 16, 21, 4, 51)
    assert task_duration_seconds(task, True, now) == 600.0
    assert task_duration_seconds(_task(), False, FAR_NOW) == 2 * 3600 + 15 * 60 + 9


def test_task_tooltip_status_text():
    running = format_task_tooltip(
        _task(end_time=None, status="running"), True, datetime(2026, 7, 16, 21, 0))
    assert "状态：运行中" in running and "时长：5分钟" in running
    assert "状态：已结束" in format_task_tooltip(_task(), False, FAR_NOW)
    assert task_status_text(True) == "运行中" and task_status_text(False) == "已结束"
    assert task_status_text(True, paused=True) == "已暂停"
    assert task_status_text(False, abnormal=True) == "异常结束"
    # paused 只在运行中有意义，abnormal 只在已结束时有意义
    assert task_status_text(False, paused=True) == "已结束"
    assert task_status_text(True, abnormal=True) == "运行中"


@pytest.mark.parametrize("reason, text", [
    (StopReason.USER, "用户停止"),
    (StopReason.PROCESS_EXITED, "进程已终止"),
    (StopReason.PROCESS_NOT_FOUND, "进程不存在"),
    (StopReason.PID_REUSED, "进程已终止"),
    (StopReason.INTERNAL_ERROR, "内部错误"),
])
def test_stop_reason_text(reason, text):
    assert stop_reason_text(reason) == text


def test_stop_reason_text_covers_all_members():
    """新增枚举成员必须同时补文案，否则界面会 KeyError"""
    for reason in StopReason:
        assert stop_reason_text(reason)


@pytest.mark.parametrize("reason, text", [
    (UnavailableReason.ACCESS_DENIED, "权限不足"),
    (UnavailableReason.PROCESS_EXITING, "进程正在退出"),
    (UnavailableReason.NOT_IN_SESSION, "进程不在当前会话，不支持采集"),
    (UnavailableReason.NOT_SUPPORTED, "仅支持 Windows"),
    (UnavailableReason.ERROR, "采集失败"),
])
def test_unavailable_reason_text(reason, text):
    assert unavailable_reason_text(reason) == text


def test_unavailable_reason_text_covers_all_members():
    for reason in UnavailableReason:
        assert unavailable_reason_text(reason)


# ---------- 判定与增速文案 ----------

def test_verdict_texts():
    assert [verdict_text(v) for v in Verdict] == ["持续上涨", "平稳", "波动", "数据不足"]
    assert verdict_text('rising') == "持续上涨"


def test_judgement_reason_text_with_and_without_fit():
    fit = LinearFit(1.0, 0.0, 0.873, 0.1)
    assert judgement_reason_text(Judgement(Verdict.RISING, fit, 'rising', 0.5)) == (
        "斜率显著为正且拟合良好（R² 0.87）")
    assert judgement_reason_text(
        Judgement(Verdict.INSUFFICIENT, None, 'short_span', 0.0)) == "采集时长不足"


@pytest.mark.parametrize("metric, rate, text", [
    (MetricType.MEMORY_PRIVATE, RateText(1024 * 7.9, 1024 * 0.3, '/分', 60.0),
     "+7.9 ±0.3 MB/分"),
    (MetricType.MEMORY_PRIVATE, RateText(-512.0, None, '/小时', 3600.0), "-512 KB/小时"),
    (MetricType.MEMORY_PRIVATE, RateText(0.001, 0.5, '/分', 60.0), "±0 ±0.5 KB/分"),
    (MetricType.NUM_HANDLES, RateText(12.345, 1.5, '/小时', 3600.0), "+12.3 ±1.5 个/小时"),
    (MetricType.NUM_HANDLES, RateText(0.25, math.inf, '/分', 60.0), "+0.25 个/分"),
    (MetricType.MEMORY_PERCENT, RateText(0.5, 0.1, '/分', 60.0), "+0.5 ±0.1%/分"),
])
def test_format_rate_text(metric, rate, text):
    assert format_rate_text(metric, rate) == text


@pytest.mark.parametrize("metric, value, text", [
    (MetricType.IO_READ_BYTES, 1024 * 1.5, "1.5 MB/秒"),
    (MetricType.IO_READ_COUNT, 0.5, "0.5 次/秒"),
    (MetricType.CPU_USER_TIME, 0.25, "0.25 秒/秒"),
    (MetricType.IO_READ_COUNT, None, "--"),
    (MetricType.IO_READ_COUNT, -2.0, "-2 次/秒"),
])
def test_format_per_second(metric, value, text):
    assert format_per_second(metric, value) == text


def test_insufficient_text_prefers_span_then_points():
    # 周期 1 秒 → 门槛 10 分钟
    assert insufficient_text(11, 12, 1.0) == "数据不足（已采 11秒）"
    assert insufficient_text(3600, 20, 1.0) == "数据不足（仅 20 点）"


def test_object_usage_text_only_for_gdi_user():
    assert object_usage_text(MetricType.NUM_GDI_OBJECTS, 3750) == "已用 37.5%（上限 10000）"
    assert object_usage_text(MetricType.NUM_USER_OBJECTS, 100) == "已用 1.0%（上限 10000）"
    assert object_usage_text(MetricType.NUM_HANDLES, 100) == ""
    assert object_usage_text(MetricType.NUM_GDI_OBJECTS, None) == ""


def test_marker_badge():
    assert [marker_badge(n) for n in (1, 2, 3, 20)] == ["①", "②", "③", "⑳"]
    assert marker_badge(21) == "(21)"


# ---------- 判定摘要行 ----------

T0 = datetime(2026, 7, 16, 8, 0, 0)


def _stats(first, last, low=None, high=None, count=100, span=7200.0):
    return {'count': count, 'min': first if low is None else low,
            'max': last if high is None else high, 'avg': (first + last) / 2,
            'first': first, 'first_ts': T0, 'last': last,
            'last_ts': T0 + timedelta(seconds=span), 'slope_per_hour': None,
            'span_seconds': span}


def _snapshot(metric, stats, **fields):
    return HistorySnapshot(request=build_request("t", metric, 'all'), stats=stats,
                           interval_seconds=fields.pop('interval_seconds', 60.0), **fields)


def _row(snapshot, metric=None):
    metric = metric or snapshot.request.metric
    return build_summary_row(metric, snapshot)


def test_summary_row_without_stats_is_dimmed_placeholders():
    row = _row(_snapshot(MetricType.NUM_THREADS, None))
    assert row.dimmed
    assert row.text("metric") == "线程数"
    assert [row.text(c) for c in ("baseline", "current", "delta", "rate", "verdict")] ==         ["--"] * 5
    assert build_summary_row(MetricType.NUM_THREADS, None).dimmed


def test_summary_row_level_rate_and_verdict():
    fit = LinearFit(1.0, 0.0, 0.95, 0.01)
    snapshot = _snapshot(
        MetricType.MEMORY_PRIVATE, _stats(100 * 1024.0, 150 * 1024.0, count=8100, span=8100),
        judgement=Judgement(Verdict.RISING, fit, 'rising', 0.9),
        rate=RateText(1024 * 12.0, 1024 * 0.4, '/小时', 3600.0), fit_unit='/小时')
    row = _row(snapshot)
    assert [row.text(c) for c in ("baseline", "current", "delta", "rate", "verdict")] == [
        "100 MB", "150 MB", "+50 MB (+50.0%)", "+12 ±0.4 MB/小时 · R² 0.95", "持续上涨"]
    assert not row.dimmed
    assert "R² 0.95" in row.tooltips["verdict"]


def test_summary_row_insufficient_is_dimmed():
    snapshot = _snapshot(
        MetricType.NUM_THREADS, _stats(10.0, 10.0, count=12, span=11),
        judgement=Judgement(Verdict.INSUFFICIENT, None, 'few_points', 0.0),
        fit_unit='/分', interval_seconds=1.0)
    row = _row(snapshot)
    assert (row.text("rate"), row.text("verdict"), row.dimmed) == (
        "数据不足（已采 11秒）", "数据不足", True)
    assert row.text("delta") == "±0"


def test_summary_row_zero_start_has_no_percent():
    assert _row(_snapshot(MetricType.NUM_HANDLES, _stats(0.0, 5.0))).text("delta") == "+5 个"


def test_summary_row_baseline_and_rounds():
    marker = Marker("t", T0 + timedelta(minutes=20), "基线", kind='baseline')
    rounds = [RoundDelta(0, "第1轮", T0, T0, 0.0, 15 * 1024.0, 15 * 1024.0),
              RoundDelta(1, "第2轮", T0, T0, 0.0, 25 * 1024.0, 25 * 1024.0),
              RoundDelta(2, "第3轮", T0, T0, None, None, None)]
    snapshot = _snapshot(
        MetricType.MEMORY_PRIVATE, _stats(140 * 1024.0, 190 * 1024.0, count=86, span=6000),
        baseline=BaselineInfo(marker, 141 * 1024.0, 5), stats_since=marker.timestamp,
        rounds=rounds, judgement=Judgement(Verdict.RISING, LinearFit(1, 0, 1.0, 0), 'rising', 1),
        rate=RateText(1024 * 0.5, 0.0, '/小时', 3600.0), fit_unit='/小时')
    row = _row(snapshot)
    assert row.text("baseline") == "141 MB"
    assert "5 个样本的中位数" in row.tooltips["baseline"]
    assert row.text("delta") == "+49 MB (+34.8%)"
    assert row.text("per_round") == "+20 MB"
    assert "第1轮：+15 MB" in row.tooltips["per_round"]
    assert "第2轮：+25 MB" in row.tooltips["per_round"]
    assert basis_text(snapshot) == "基线 08:20:00 起 · 1小时40分 · 86 点"


def test_summary_row_baseline_only_off_shows_start_value():
    marker = Marker("t", T0, "基线", kind='baseline')
    snapshot = HistorySnapshot(
        request=build_request("t", MetricType.NUM_HANDLES, 'all', baseline_only=False),
        stats=_stats(10.0, 20.0), baseline=BaselineInfo(marker, 12.0, 5))
    assert _row(snapshot).text("baseline") == "10 个"


def test_summary_row_gdi_usage():
    row = _row(_snapshot(MetricType.NUM_GDI_OBJECTS, _stats(100.0, 3750.0)))
    assert row.text("usage") == "37.5% / 10000"
    assert row.tooltips["usage"].startswith("已用 37.5%（上限 10000）")


def test_summary_row_cumulative_shows_average_rate_not_growth():
    snapshot = _snapshot(
        MetricType.IO_READ_BYTES, _stats(1024.0, 3072.0, count=10, span=600),
        value_class=VALUE_CLASS_RATE, rate_stats=RateStats(1024 / 600 * 2, 1024 * 1.5))
    row = _row(snapshot)
    assert (row.text("rate"), row.text("verdict")) == ("平均 3.41 KB/秒", "累计量")


def test_summary_row_peak_shows_rises():
    snapshot = _snapshot(
        MetricType.MEMORY_PEAK_WSET, _stats(100.0, 180.0),
        value_class=VALUE_CLASS_PEAK, peak_rises=PeakRises(2, 80.0))
    assert (_row(snapshot).text("rate"), _row(snapshot).text("verdict")) == ("抬升 2 次", "峰值")
    none = _snapshot(MetricType.MEMORY_PEAK_WSET, _stats(100.0, 100.0),
                     value_class=VALUE_CLASS_PEAK, peak_rises=PeakRises(0, 0.0))
    assert _row(none).text("rate") == "未抬升"


def test_marker_context_locates_interval():
    times = [100.0, 200.0, 400.0]
    labels = ["开始", "第1轮", "第2轮"]
    assert marker_context(times, labels, 50.0) is None
    assert marker_context(times, labels, 100.0) == "打点「开始」后 0秒"
    assert marker_context(times, labels, 330.0) == "打点「第1轮」后 2分钟"
    assert marker_context(times, labels, 9999.0).startswith("打点「第2轮」后")


def test_abnormal_stop_only_excludes_user():
    """只有用户停止算正常结束；进程退出、内部错误等都显示"异常结束"。"""
    assert is_abnormal_stop(None) is False
    assert is_abnormal_stop(StopReason.USER) is False
    for reason in StopReason:
        if reason is not StopReason.USER:
            assert is_abnormal_stop(reason) is True
            assert task_status_text(False, abnormal=is_abnormal_stop(reason)) == "异常结束"
