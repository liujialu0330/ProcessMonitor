"""
ui/formatting.py 纯函数：时长、任务下拉文案、统计条文本、打点区间、默认导出文件名。
"""
from datetime import datetime

import pytest

from core.contracts import StopReason, UnavailableReason
from data.models import MonitorTask
from ui.formatting import (
    build_stat_texts, default_export_filename, format_count, format_duration,
    format_task_option_text, format_task_tooltip, marker_context,
    sanitize_filename, stop_reason_text, task_duration_seconds, task_status_text,
    truncate_text, unavailable_reason_text
)
from utils.metrics import RATE_NOT_APPLICABLE, MetricType

# 已停止任务的时长不看 now；随便给一个远离起止时间的时刻，证明确实没被用到
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
    assert "状态：已停止" in format_task_tooltip(_task(), False, FAR_NOW)
    assert task_status_text(True) == "运行中" and task_status_text(False) == "已停止"


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


def test_build_stat_texts_growth():
    """起点/当前/变化(绝对值+百分比)/增速/最小/最大"""
    stats = {
        'count': 100, 'min': 100 * 1024.0, 'max': 150 * 1024.0, 'avg': 120 * 1024.0,
        'first': 100 * 1024.0, 'last': 150 * 1024.0,
        'slope_per_hour': 12 * 1024.0,
    }
    texts = build_stat_texts(MetricType.MEMORY_PRIVATE, stats, "MB")
    assert texts["起点"] == "100 MB"
    assert texts["当前"] == "150 MB"
    assert texts["变化"] == "+50 MB (+50.0%)"
    assert texts["增速"] == "+12 MB/小时"
    assert texts["最小"] == "100 MB"
    assert texts["最大"] == "150 MB"


def test_build_stat_texts_flat_and_missing():
    flat = {'count': 3, 'min': 10.0, 'max': 10.0, 'avg': 10.0, 'first': 10.0,
            'last': 10.0, 'slope_per_hour': None}
    texts = build_stat_texts(MetricType.NUM_THREADS, flat)
    assert texts["变化"] == "±0"
    assert texts["增速"] == "--"

    empty = build_stat_texts(MetricType.NUM_THREADS, None)
    assert set(empty.values()) == {"--"}
    assert tuple(empty) == ("起点", "当前", "变化", "增速", "最小", "最大")


def test_build_stat_texts_zero_baseline_has_no_percent():
    stats = {'count': 2, 'min': 0.0, 'max': 5.0, 'avg': 2.5, 'first': 0.0,
             'last': 5.0, 'slope_per_hour': 3.0}
    texts = build_stat_texts(MetricType.NUM_HANDLES, stats)
    assert texts["变化"] == "+5 个"


def test_build_stat_texts_cumulative_metric_rate_not_applicable():
    """累计类指标（IO 字节等）单调不减：变化量照常显示，增速不外推"""
    stats = {'count': 10, 'min': 1024.0, 'max': 3072.0, 'avg': 2048.0,
             'first': 1024.0, 'last': 3072.0, 'slope_per_hour': 4096.0}
    texts = build_stat_texts(MetricType.IO_READ_BYTES, stats, "MB")
    assert texts["变化"] == "+2 MB (+200.0%)"
    assert texts["增速"] == RATE_NOT_APPLICABLE
    assert texts["当前"] == "3 MB"


def test_marker_context_locates_interval():
    times = [100.0, 200.0, 400.0]
    labels = ["开始", "第1轮", "第2轮"]
    assert marker_context(times, labels, 50.0) is None
    assert marker_context(times, labels, 100.0) == "打点「开始」后 0秒"
    assert marker_context(times, labels, 330.0) == "打点「第1轮」后 2分钟"
    assert marker_context(times, labels, 9999.0).startswith("打点「第2轮」后")


def test_default_export_filename():
    assert default_export_filename(_task()) == "MPrintExp_1234_20260716_205451.csv"
    weird = _task(process_name='a:b*c?.EXE')
    assert default_export_filename(weird) == "a_b_c__1234_20260716_205451.csv"
    assert sanitize_filename('  ') == 'task'
