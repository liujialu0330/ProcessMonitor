"""
长时间监控的存储与方案：停止信息（exit_code + meta_json 的 stop 键）、续接策略同步到专用列、
遗留运行中任务的校正范围，以及方案新字段的校验与 JSON 往返。
"""
import json
from datetime import datetime, timedelta

import pytest

from core.automation.plan import (
    DEFAULT_RESTART_LIMIT, RESTART_LIMIT_MAX, RESTART_LIMIT_MIN, AutomationPlan,
    plan_from_json_or_none,
)
from data.mappers import stop_reason_of
from data.models import MonitorTask

NOW = datetime(2026, 11, 6, 9, 0, 0)


def _task(db, task_id='t1', status='running', last_sample=None):
    task = MonitorTask(task_id=task_id, pid=1, process_name='a.exe', metric_types=['memory_rss'],
                       interval=1.0, start_time=NOW, end_time=None, status=status)
    db.save_task(task)
    if last_sample is not None:
        with db._get_connection() as conn:
            conn.execute('UPDATE tasks SET last_sample_at = ? WHERE task_id = ?',
                         (last_sample.isoformat(sep=' '), task_id))
    return task


# ========== save_stop / meta_json ==========

def test_save_stop_records_exit_code_and_reason(db):
    _task(db)
    assert db.tasks.save_stop('t1', 'crashed', 0xC0000005, "0xC0000005（访问冲突）") is True
    task = db.tasks.get('t1')
    assert task.exit_code == 0xC0000005
    assert task.stop_reason == 'crashed'
    meta = json.loads(db.tasks.load_meta('t1'))
    assert meta['stop'] == {'reason': 'crashed', 'text': '0xC0000005（访问冲突）'}


def test_save_stop_missing_task_returns_false(db):
    assert db.tasks.save_stop('nope', 'user', None) is False


def test_save_stop_without_reason_only_records_exit_code(db):
    """进程退出后正在等待重启的中间状态：只记退出码，不写停止原因"""
    _task(db)
    db.tasks.save_stop('t1', None, 1)
    task = db.tasks.get('t1')
    assert task.exit_code == 1 and task.stop_reason is None


def test_save_stop_none_exit_code_keeps_or_clears_on_request(db):
    _task(db)
    db.tasks.save_stop('t1', None, 7)
    db.tasks.save_stop('t1', 'user', None)
    assert db.tasks.get('t1').exit_code == 7
    db.tasks.save_stop('t1', None, None, clear_exit_code=True)
    task = db.tasks.get('t1')
    assert task.exit_code is None and task.stop_reason == 'user'


def test_save_meta_preserves_stop_key(db):
    """后写的被测元数据不能把已记录的停止原因冲掉"""
    _task(db)
    db.tasks.save_stop('t1', 'crashed', 3, "x")
    assert db.tasks.save_meta('t1', json.dumps({'exe': 'a.exe'})) is True
    meta = json.loads(db.tasks.load_meta('t1'))
    assert meta['exe'] == 'a.exe' and meta['stop']['reason'] == 'crashed'
    assert db.tasks.get('t1').stop_reason == 'crashed'


def test_save_stop_preserves_existing_meta(db):
    _task(db)
    db.tasks.save_meta('t1', json.dumps({'exe': 'a.exe'}))
    db.tasks.save_stop('t1', 'process_exited', 0)
    meta = json.loads(db.tasks.load_meta('t1'))
    assert meta['exe'] == 'a.exe' and meta['stop']['reason'] == 'process_exited'


def test_save_meta_none_clears_meta_but_keeps_stop(db):
    _task(db)
    db.tasks.save_stop('t1', 'user', None)
    db.tasks.save_meta('t1', None)
    assert db.tasks.get('t1').stop_reason == 'user'


def test_stop_reason_of_tolerates_bad_json():
    assert stop_reason_of(None) is None
    assert stop_reason_of('') is None
    assert stop_reason_of('{坏') is None
    assert stop_reason_of('{"exe": "a"}') is None
    assert stop_reason_of('{"stop": {"reason": "crashed"}}') == 'crashed'
    assert stop_reason_of('{"stop": "x"}') is None


# ========== 方案 -> 专用列 ==========

def test_save_plan_syncs_restart_policy_columns(db):
    _task(db)
    plan = AutomationPlan(resume_on_restart=True, restart_limit=5)
    assert db.tasks.save_plan('t1', plan.to_json()) is True
    assert db.tasks.load_restart_policy('t1') == (True, 5)
    task = db.tasks.get('t1')
    assert task.resume_on_restart is True and task.restart_limit == 5


def test_save_plan_without_resume_or_cleared_plan_turns_policy_off(db):
    _task(db)
    db.tasks.save_plan('t1', AutomationPlan(resume_on_restart=True).to_json())
    db.tasks.save_plan('t1', AutomationPlan(timed_marker_seconds=60).to_json())
    assert db.tasks.load_restart_policy('t1') == (False, 0)
    db.tasks.save_plan('t1', AutomationPlan(resume_on_restart=True).to_json())
    db.tasks.save_plan('t1', None)
    assert db.tasks.load_restart_policy('t1') == (False, 0)
    assert db.tasks.load_restart_policy('missing') == (False, 0)


@pytest.mark.parametrize("payload", ['{坏', '[]', '{"resume_on_restart": 1}',
                                     '{"resume_on_restart": true, "restart_limit": 0}',
                                     '{"resume_on_restart": true, "restart_limit": true}'])
def test_save_plan_bad_policy_payload_is_treated_as_off(db, payload):
    _task(db)
    db.tasks.save_plan('t1', payload)
    assert db.tasks.load_restart_policy('t1') == (False, 0)


# ========== 校正范围 ==========

def test_reconcile_orphans_exclude_and_only(db):
    for tid in ('a', 'b', 'c'):
        _task(db, tid, last_sample=NOW + timedelta(minutes=5))
    assert db.reconcile_orphan_tasks(exclude=['a']) == 2
    assert db.tasks.get('a').status == 'running'
    assert db.tasks.get('b').status == 'stopped'
    assert db.tasks.get('b').end_time == NOW + timedelta(minutes=5)
    assert db.reconcile_orphan_tasks(only=['a']) == 1
    assert db.tasks.get('a').status == 'stopped'


def test_reconcile_orphans_only_empty_list_touches_nothing(db):
    _task(db, 'a')
    assert db.reconcile_orphan_tasks(only=[]) == 0
    assert db.tasks.get('a').status == 'running'


def test_reconcile_orphans_default_closes_everything(db):
    _task(db, 'a')
    _task(db, 'b')
    assert db.reconcile_orphan_tasks() == 2


def test_list_running_only_running_oldest_first(db):
    _task(db, 'late')
    old = MonitorTask(task_id='early', pid=1, process_name='a.exe', metric_types=['memory_rss'],
                      interval=1.0, start_time=NOW - timedelta(hours=1), end_time=None,
                      status='running')
    db.save_task(old)
    _task(db, 'done', status='stopped')
    assert [t.task_id for t in db.tasks.list_running()] == ['early', 'late']


# ========== 方案字段 ==========

def test_plan_defaults_and_json_roundtrip():
    plan = AutomationPlan()
    assert plan.resume_on_restart is False
    assert plan.restart_limit == DEFAULT_RESTART_LIMIT == 3
    assert plan.is_empty()
    custom = AutomationPlan(resume_on_restart=True, restart_limit=7, timed_marker_seconds=60)
    assert plan_from_json_or_none(custom.to_json()) == custom
    assert not AutomationPlan(resume_on_restart=True).is_empty()


def test_plan_resume_alone_needs_no_service():
    """续接由采集线程自己完成，不需要自动打点服务跟踪"""
    assert AutomationPlan(resume_on_restart=True).needs_service() is False


@pytest.mark.parametrize("limit, valid", [(0, False), (RESTART_LIMIT_MIN, True), (3, True),
                                          (RESTART_LIMIT_MAX, True), (RESTART_LIMIT_MAX + 1, False),
                                          (-1, False)])
def test_plan_restart_limit_range(limit, valid):
    errors = AutomationPlan(resume_on_restart=True, restart_limit=limit).validate()
    assert (errors == []) is valid
    if not valid:
        assert any("次" in e for e in errors)


def test_plan_from_json_rejects_non_bool_resume_and_non_int_limit():
    assert plan_from_json_or_none('{"resume_on_restart": "yes"}') is None
    assert plan_from_json_or_none('{"restart_limit": "3"}') is None
    assert plan_from_json_or_none('{"restart_limit": 2.5}') is None
    plan = plan_from_json_or_none('{"resume_on_restart": true}')
    assert plan.resume_on_restart is True and plan.restart_limit == 3
