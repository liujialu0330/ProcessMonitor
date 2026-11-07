"""
AutomationPlan：JSON 往返无损、缺省即空方案、未知字段忽略并记日志、版本过高报错、
字段类型检查与表驱动的校验文案；停止条件纯函数。
"""
import json
import logging
from datetime import datetime, timedelta

import pytest

from core.automation.plan import (
    ERR_EXPORT_DIR_MISSING, ERR_EXPORT_DIR_NOT_ABSOLUTE, ERR_LOG_ENCODING_UNSUPPORTED,
    ERR_LOG_PATH_MISSING, ERR_LOG_PATH_NOT_ABSOLUTE, ERR_LOG_PATTERN_MISSING,
    ERR_STOP_ROUNDS_TOO_SMALL, ERR_TIMED_TOO_SMALL, PLAN_SCHEMA_VERSION, AutomationPlan,
    PlanFormatError, PlanVersionError, is_absolute_path, plan_from_json_or_none,
)
from core.automation.stop_conditions import (
    AUTO_STOP_DURATION, AUTO_STOP_ROUNDS, StopState, should_stop,
)

FULL = AutomationPlan(
    timed_marker_seconds=600, log_path=r"D:\日志\app.log", log_pattern=r"Case (?P<label>\d+) done",
    log_encoding="gbk", stop_after_seconds=7200, stop_after_rounds=10,
    stop_when_target_exits=True, export_on_stop=True, export_dir=r"D:\导出")


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
    '{"timed_marker_seconds": 1.5}',
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
