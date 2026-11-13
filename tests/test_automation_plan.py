"""
AutomationPlan：JSON 往返无损、缺省即空方案、未知字段忽略并记日志、版本过高报错、
字段类型检查与表驱动的校验文案；停止条件纯函数；阶段快照字段（成对、绝对路径、范围）与简述。
"""
import json
import logging
from datetime import datetime, timedelta

import pytest

from core.automation.plan import (
    ERR_EXPORT_DIR_MISSING, ERR_EXPORT_DIR_NOT_ABSOLUTE, ERR_LOG_ENCODING_UNSUPPORTED,
    ERR_LOG_PATH_MISSING, ERR_LOG_PATH_NOT_ABSOLUTE, ERR_LOG_PATTERN_MISSING,
    ERR_SNAPSHOT_DIR_MISSING, ERR_SNAPSHOT_DIR_NOT_ABSOLUTE, ERR_SNAPSHOT_HOURS_MISSING,
    ERR_STOP_ROUNDS_TOO_SMALL, ERR_TIMED_TOO_SMALL, PLAN_SCHEMA_VERSION, AutomationPlan,
    MAX_PLAN_SECONDS, PlanFormatError, PlanVersionError, is_absolute_path, is_device_path,
    plan_from_json_or_none,
)
from core.automation.stop_conditions import (
    AUTO_STOP_DURATION, AUTO_STOP_ROUNDS, StopState, should_stop,
)

FULL = AutomationPlan(
    timed_marker_seconds=600, log_path=r"D:\日志\app.log", log_pattern=r"Case (?P<label>\d+) done",
    log_encoding="gbk", stop_after_seconds=7200, stop_after_rounds=10,
    stop_when_target_exits=True, export_on_stop=True, export_dir=r"D:\导出",
    stage_snapshot_hours=6.0, stage_snapshot_dir=r"D:\快照")


# ---------------------------------------------------------------- 往返


@pytest.mark.parametrize("plan", [AutomationPlan(), FULL,
                                  AutomationPlan(timed_marker_seconds=5),
                                  AutomationPlan(stop_after_rounds=3, export_on_stop=False)])
def test_json_round_trip_is_lossless(plan):
    text = plan.to_json()
    assert AutomationPlan.from_json(text) == plan
    assert AutomationPlan.from_dict(plan.to_dict()) == plan


def test_json_shape_is_stable():
    """字段名是持久化契约：改名会让旧任务的方案读不回来"""
    data = json.loads(FULL.to_json())
    assert data == {
        "schema_version": 1,
        "timed_marker_seconds": 600,
        "log_path": r"D:\日志\app.log",
        "log_pattern": r"Case (?P<label>\d+) done",
        "log_encoding": "gbk",
        "stop_after_seconds": 7200,
        "stop_after_rounds": 10,
        "stop_when_target_exits": True,
        "export_on_stop": True,
        "export_dir": r"D:\导出",
        "stage_snapshot_hours": 6.0,
        "stage_snapshot_dir": r"D:\快照",
        "resume_on_restart": False,
        "restart_limit": 3,
    }
    # 中文不转义，人能直接读
    assert "日志" in FULL.to_json()


def test_default_plan_is_empty_and_needs_no_service():
    plan = AutomationPlan()
    assert plan.is_empty() and not plan.needs_service()
    assert plan.stop_when_target_exits is True and plan.log_encoding == "utf-8"
    assert not AutomationPlan(timed_marker_seconds=60).is_empty()
    assert AutomationPlan(export_on_stop=True, export_dir="D:\\x").needs_service()


def test_missing_fields_take_defaults_and_missing_version_means_v1():
    plan = AutomationPlan.from_json('{"timed_marker_seconds": 30}')
    assert plan == AutomationPlan(timed_marker_seconds=30)


def test_unknown_fields_are_ignored_and_logged(caplog):
    text = json.dumps({"schema_version": 1, "timed_marker_seconds": 60,
                       "scenario": "建模", "targets": [1, 2]})
    with caplog.at_level(logging.WARNING, logger="core.automation.plan"):
        plan = AutomationPlan.from_json(text)
    assert plan == AutomationPlan(timed_marker_seconds=60)
    assert "scenario" in caplog.text and "targets" in caplog.text


def test_newer_schema_version_is_rejected():
    with pytest.raises(PlanVersionError) as info:
        AutomationPlan.from_json(json.dumps({"schema_version": PLAN_SCHEMA_VERSION + 1}))
    assert info.value.version == PLAN_SCHEMA_VERSION + 1
    assert "升级" in str(info.value)
    # 版本错误也是格式错误的一种：只关心"读不了"的调用方一个 except 即可
    assert issubclass(PlanVersionError, PlanFormatError)


@pytest.mark.parametrize("text", [
    "不是 JSON", "[1, 2]", '{"schema_version": "1"}', '{"timed_marker_seconds": "60"}',
    '{"timed_marker_seconds": true}', '{"export_on_stop": 1}', '{"log_path": 3}',
    '{"timed_marker_seconds": 1.5}', '{"stage_snapshot_hours": "6"}',
    '{"stage_snapshot_hours": true}', '{"stage_snapshot_dir": 1}',
])
def test_malformed_json_raises_format_error(text):
    with pytest.raises(PlanFormatError):
        AutomationPlan.from_json(text)


def test_integral_floats_and_empty_strings_are_normalized():
    plan = AutomationPlan.from_json(
        '{"timed_marker_seconds": 60.0, "log_path": "", "log_encoding": ""}')
    assert plan == AutomationPlan(timed_marker_seconds=60)


def test_plan_from_json_or_none_swallows_bad_input(caplog):
    assert plan_from_json_or_none(None) is None
    assert plan_from_json_or_none("") is None
    assert plan_from_json_or_none(FULL.to_json()) == FULL
    with caplog.at_level(logging.WARNING):
        assert plan_from_json_or_none('{"schema_version": 99}') is None
        assert plan_from_json_or_none("{坏的") is None


# ---------------------------------------------------------------- 校验


@pytest.mark.parametrize("plan, interval, expected", [
    (AutomationPlan(), 1.0, []),
    (FULL, 5.0, []),
    (AutomationPlan(timed_marker_seconds=0), None, [ERR_TIMED_TOO_SMALL]),
    (AutomationPlan(timed_marker_seconds=5), 10.0,
     ["定时打点间隔（5 秒）不能小于采集间隔（10 秒）"]),
    (AutomationPlan(timed_marker_seconds=10), 10.0, []),
    (AutomationPlan(log_pattern="x"), None, [ERR_LOG_PATH_MISSING]),
    (AutomationPlan(log_path=r"C:\a.log"), None, [ERR_LOG_PATTERN_MISSING]),
    (AutomationPlan(log_path="logs\\a.log", log_pattern="x"), None, [ERR_LOG_PATH_NOT_ABSOLUTE]),
    (AutomationPlan(log_path=r"C:\a.log", log_pattern="x", log_encoding="utf-16"), None,
     [ERR_LOG_ENCODING_UNSUPPORTED]),
    (AutomationPlan(log_path=r"C:\a.log", log_pattern="x", log_encoding="no-such"), None,
     ["不认识的日志文件编码：no-such"]),
    (AutomationPlan(stop_after_seconds=3), 5.0, ["停止时长（3 秒）不能小于采集间隔（5 秒）"]),
    (AutomationPlan(stop_after_rounds=0), None, [ERR_STOP_ROUNDS_TOO_SMALL]),
    (AutomationPlan(export_on_stop=True), None, [ERR_EXPORT_DIR_MISSING]),
    (AutomationPlan(export_on_stop=True, export_dir="out"), None, [ERR_EXPORT_DIR_NOT_ABSOLUTE]),
])
def test_validate_table(plan, interval, expected):
    assert plan.validate(interval) == expected


def test_invalid_regex_is_reported_in_chinese():
    errors = AutomationPlan(log_path=r"C:\a.log", log_pattern="Case (\\d+").validate()
    assert len(errors) == 1 and errors[0].startswith("日志匹配规则不是合法的正则表达式")


def test_multiple_errors_are_all_reported():
    errors = AutomationPlan(timed_marker_seconds=1, stop_after_rounds=0,
                            export_on_stop=True).validate(2.0)
    assert len(errors) == 3


@pytest.mark.parametrize("path, expected", [
    (r"C:\logs\a.log", True), (r"\\server\share\a.log", True), ("C:/logs/a.log", True),
    ("logs\\a.log", False), ("C:a.log", False), ("", False), ("\\logs\\a.log", False),
])
def test_is_absolute_path(path, expected):
    assert is_absolute_path(path) is expected


# ---------------------------------------------------------------- 停止条件

T0 = datetime(2026, 11, 7, 9, 0, 0)


@pytest.mark.parametrize("plan, state, now, expected", [
    (AutomationPlan(), StopState(T0, 99), T0 + timedelta(days=9), None),
    (AutomationPlan(stop_after_seconds=60), StopState(T0), T0 + timedelta(seconds=59), None),
    (AutomationPlan(stop_after_seconds=60), StopState(T0), T0 + timedelta(seconds=60),
     AUTO_STOP_DURATION),
    (AutomationPlan(stop_after_seconds=60), StopState(None), T0 + timedelta(days=1), None),
    (AutomationPlan(stop_after_rounds=3), StopState(T0, 2), T0, None),
    (AutomationPlan(stop_after_rounds=3), StopState(T0, 3), T0, AUTO_STOP_ROUNDS),
    (AutomationPlan(stop_after_seconds=60, stop_after_rounds=3), StopState(T0, 3),
     T0 + timedelta(seconds=60), AUTO_STOP_DURATION),
])
def test_should_stop_table(plan, state, now, expected):
    assert should_stop(state, plan, now) == expected


# ---------------------------------------------------------------- 阶段快照

SNAP_DIR = r"D:\快照"
ERR_SNAPSHOT_RANGE = "阶段快照间隔须在 0.5 到 168 小时之间"


@pytest.mark.parametrize("plan", [
    AutomationPlan(stage_snapshot_hours=0.5, stage_snapshot_dir=SNAP_DIR),
    AutomationPlan(stage_snapshot_hours=168.0, stage_snapshot_dir=r"\\nas\share\快照"),
])
def test_snapshot_fields_round_trip(plan):
    assert AutomationPlan.from_json(plan.to_json()) == plan


def test_snapshot_hours_integer_json_reads_as_float():
    plan = AutomationPlan.from_json('{"stage_snapshot_hours": 6, "stage_snapshot_dir": "D:/s"}')
    assert plan.stage_snapshot_hours == 6.0 and isinstance(plan.stage_snapshot_hours, float)
    assert plan.has_stage_snapshot


def test_old_json_without_snapshot_fields_has_no_snapshot():
    plan = AutomationPlan.from_json('{"schema_version": 1, "timed_marker_seconds": 60}')
    assert plan.stage_snapshot_hours is None and plan.stage_snapshot_dir is None
    assert not plan.has_stage_snapshot


def test_snapshot_alone_needs_service():
    plan = AutomationPlan(stage_snapshot_hours=6, stage_snapshot_dir=SNAP_DIR)
    assert not plan.is_empty() and plan.needs_service()
    # 只有一半（没有目录）不算开启
    assert not AutomationPlan(stage_snapshot_hours=6).has_stage_snapshot


@pytest.mark.parametrize("hours, directory, expected", [
    (0.5, SNAP_DIR, []),
    (168, SNAP_DIR, []),
    (6, SNAP_DIR, []),
    (0.4, SNAP_DIR, [ERR_SNAPSHOT_RANGE]),
    (168.5, SNAP_DIR, [ERR_SNAPSHOT_RANGE]),
    (0, SNAP_DIR, [ERR_SNAPSHOT_RANGE]),
    (float("nan"), SNAP_DIR, [ERR_SNAPSHOT_RANGE]),
    (6, None, [ERR_SNAPSHOT_DIR_MISSING]),
    (None, SNAP_DIR, [ERR_SNAPSHOT_HOURS_MISSING]),
    (6, "快照", [ERR_SNAPSHOT_DIR_NOT_ABSOLUTE]),
    (None, "快照", [ERR_SNAPSHOT_HOURS_MISSING, ERR_SNAPSHOT_DIR_NOT_ABSOLUTE]),
])
def test_snapshot_validate_table(hours, directory, expected):
    plan = AutomationPlan(stage_snapshot_hours=hours, stage_snapshot_dir=directory)
    assert plan.validate(1.0) == expected


@pytest.mark.parametrize("plan, expected", [
    (AutomationPlan(stage_snapshot_hours=6, stage_snapshot_dir=SNAP_DIR), "每 6 小时写阶段快照"),
    (AutomationPlan(stage_snapshot_hours=0.5, stage_snapshot_dir=SNAP_DIR),
     "每 0.5 小时写阶段快照"),
    (AutomationPlan(timed_marker_seconds=600, stage_snapshot_hours=12.0,
                    stage_snapshot_dir=SNAP_DIR), "自动打点 每 10 分钟 · 每 12 小时写阶段快照"),
    # 不成对（没有目录）不进简述
    (AutomationPlan(stage_snapshot_hours=6), ""),
])
def test_plan_summary_mentions_stage_snapshot(plan, expected):
    from ui.formatting import plan_summary
    assert plan_summary(plan) == expected


# ---------------------------------------------------------------- 评审修复：编码、上限、设备路径


@pytest.mark.parametrize("encoding", ["base64", "rot13", "zlib", "hex", "uu", "bz2"])
def test_non_text_codecs_are_rejected(encoding):
    """bytes→bytes 编解码器能 lookup 成功，但不能用来解码日志行（修复前读尾时才抛 LookupError）"""
    errors = AutomationPlan(log_path=r"C:\a.log", log_pattern="x",
                            log_encoding=encoding).validate()
    assert errors == [f"{encoding} 不是文本编码，请改用 UTF-8 或 GBK 等"]


@pytest.mark.parametrize("encoding", ["utf-8", "gbk", "GB18030", "latin-1", "cp936", "UTF8"])
def test_text_codecs_are_accepted(encoding):
    assert AutomationPlan(log_path=r"C:\a.log", log_pattern="x",
                          log_encoding=encoding).validate() == []


@pytest.mark.parametrize("plan, expected", [
    (AutomationPlan(timed_marker_seconds=MAX_PLAN_SECONDS), []),
    (AutomationPlan(timed_marker_seconds=MAX_PLAN_SECONDS + 1),
     [f"定时打点间隔不能超过 {MAX_PLAN_SECONDS} 秒（30 天）"]),
    (AutomationPlan(timed_marker_seconds=10 ** 18),
     [f"定时打点间隔不能超过 {MAX_PLAN_SECONDS} 秒（30 天）"]),
    (AutomationPlan(stop_after_seconds=MAX_PLAN_SECONDS), []),
    (AutomationPlan(stop_after_seconds=10 ** 18),
     [f"停止时长不能超过 {MAX_PLAN_SECONDS} 秒（30 天）"]),
])
def test_seconds_are_capped_at_30_days(plan, expected):
    """上限防 timedelta 溢出：修复前 10**18 秒的方案通过校验，挂接时 OverflowError"""
    assert MAX_PLAN_SECONDS == 30 * 24 * 3600
    assert plan.validate(1.0) == expected


@pytest.mark.parametrize("path", [
    r"\\.\C:\logs\a.log", r"\\?\C:\logs\a.log", r"\\.\PhysicalDrive0", "//./pipe/x",
    r"\\?\UNC\server\share\a.log",
])
def test_device_paths_are_not_absolute(path):
    assert is_device_path(path) is True
    assert is_absolute_path(path) is False
    errors = AutomationPlan(log_path=path, log_pattern="x").validate()
    assert errors == [ERR_LOG_PATH_NOT_ABSOLUTE]


def test_unc_and_drive_paths_are_not_device_paths():
    assert not is_device_path(r"\\server\share\a.log") and not is_device_path(r"C:\a.log")
    assert is_absolute_path(None) is False
