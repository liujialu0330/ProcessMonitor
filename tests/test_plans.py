"""完整测试方案的往返、边界校验、文件原子性与多目标部分成功。"""
import json
from dataclasses import replace

import pytest

from core.automation.plan import AutomationPlan, PlanFormatError
from core.plans import PlanStore, PlanTarget, TestPlan, execute_plan, read_plan, write_plan


def _plan():
    return TestPlan('打开关闭窗口', (PlanTarget(name='app.exe', metrics=('num_handles',),
                    scenario='反复打开', build='2.0', note='{scenario} 第{index}组 {date}',
                    automation=AutomationPlan(timed_marker_seconds=10, stop_after_rounds=3)),))


def test_roundtrip_preserves_complete_configuration(tmp_path):
    plan = _plan()
    store = PlanStore(tmp_path)
    path = store.save(plan)
    assert read_plan(path) == plan
    assert store.list() == [plan]
    exported = tmp_path / 'share' / '方案.json'
    store.export_file(plan, exported)
    assert read_plan(exported) == plan
    with pytest.raises(FileExistsError):
        store.import_file(exported)
    assert store.import_file(exported, overwrite=True) == plan


@pytest.mark.parametrize('patch', [
    {'schema_version': 2}, {'schema_version': True}, {'targets': []},
    {'targets': [{}]}, {'targets': [{'pid': True}]},
    {'targets': [{'name': 'a', 'interval': float('nan')}]},
    {'targets': [{'name': 'a', 'interval': .5}]},
    {'targets': [{'name': 'a', 'note': '{process.__class__}'}]},
    {'targets': [{'name': 'a', 'wait': 'yes'}]},
    {'targets': [{'pid': 5, 'wait': True}]},
    {'targets': [{'name': 'a', 'target_kind': 'tree'}]},
    {'targets': [{'name': 'a', 'close_on_stop': True}]},
    {'targets': [{'name': 'a', 'scenario': '长' * 121}]},
    {'targets': [{'launch': {'exe': 'relative.exe'}}]},
])
def test_invalid_plan_is_rejected_before_execution(patch):
    with pytest.raises(PlanFormatError):
        TestPlan.from_dict({**_plan().to_dict(), **patch})


def test_unknown_fields_are_logged_and_ignored(caplog):
    data = _plan().to_dict()
    data['future'] = 'kept by newer program'
    data['targets'][0]['new_field'] = 1
    assert TestPlan.from_dict(data) == _plan()
    assert 'future' in caplog.text and 'new_field' in caplog.text


def test_filename_cannot_escape_store(tmp_path):
    store = PlanStore(tmp_path)
    plan = replace(_plan(), name='../../CON: 测试')
    path = store.save(plan)
    assert path.parent.resolve() == (tmp_path / 'plans').resolve()
    assert read_plan(path) == plan


def test_failed_replace_preserves_previous_file(tmp_path, monkeypatch):
    path = write_plan(_plan(), tmp_path / 'a.json')
    before = path.read_bytes()
    def fail(*args):
        raise OSError('写入被拒绝')
    monkeypatch.setattr('core.plans.os.replace', fail)
    with pytest.raises(OSError):
        write_plan(replace(_plan(), name='新方案'), path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('.plan-*'))


def test_partial_failure_keeps_successful_tasks_and_reports_each_target():
    plan = TestPlan('多目标', tuple(PlanTarget(pid=i) for i in (1, 2, 3)))
    def start(target, index):
        if target.pid == 2:
            raise RuntimeError('进程已退出')
        return f'task-{index}'
    results = execute_plan(plan, start)
    assert [r.task_id for r in results] == ['task-1', None, 'task-3']
    assert results[1].error == '进程已退出'


def test_note_template_expands_without_attribute_access():
    from datetime import datetime
    args = _plan().targets[0].start_args(index=2, now=datetime(2026, 11, 12, 10, 30))
    assert args['note'] == '反复打开 第2组 20261112_103000'
    assert args['scenario'] == '反复打开'
    assert args['plan']['stop_after_rounds'] == 3


def test_tree_plan_can_wait_for_root_by_name():
    target = PlanTarget.from_dict({'name': 'root.exe', 'target_kind': 'tree', 'wait': True})
    args = target.start_args()
    assert args['name'] == 'root.exe' and args['target_kind'] == 'tree' and args['wait']
