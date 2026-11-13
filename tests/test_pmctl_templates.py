"""完整方案命令行：逐目标反馈、共享分组和参考比较退出码。"""
import json
import pytest

from tests.test_pmctl import FakeSession, err, ok, run_cli


def test_start_template_runs_all_targets_and_keeps_partial_success(tmp_path):
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps({'schema_version': 1, 'name': '成组测试', 'targets': [
        {'pid': 100, 'scenario': '打开窗口'}, {'pid': 200}, {'pid': 300}]}), encoding='utf-8')
    def start(payload):
        assert len(payload['plan']['targets']) == 3
        return ok({'started': 2, 'failed': 1, 'group_id': 'batch', 'results': [
            {'index': 1, 'ok': True, 'result': {'task_id': '100'}},
            {'index': 2, 'ok': False, 'error': {'message': '进程不存在'}},
            {'index': 3, 'ok': True, 'result': {'task_id': '300'}}]})
    session = FakeSession({'start_plan': start})
    code, out, errors, _ = run_cli(['start', '--plan', str(path)], session)
    assert code == 1 and len(session.calls) == 1
    assert '1. 100' in out and '3. 300' in out and '目标 2' in errors
    assert '共享打点组：batch' in out
    assert session.calls[0][0] == 'start_plan'
    assert session.calls[0][1]['plan']['targets'][0]['scenario'] == '打开窗口'


def test_invalid_template_never_connects(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text('{"schema_version":99,"targets":[]}', encoding='utf-8')
    code, _, _, session = run_cli(['start', '--plan', str(path)])
    assert code == 2 and not session.calls


def test_reference_verdict_maps_regression_to_failure_code():
    session = FakeSession({'verdict': ok({'verdict': 'stable',
                                         'reference': {'status': 'regression'}})})
    code, out, _, _ = run_cli(['verdict', '--task', 'x', '--vs-reference'], session)
    assert code == 10 and '较参考发生回归' in out
    assert session.calls[0][1] == {'task_id': 'x', 'vs_reference': True}


def test_marker_group_is_explicitly_scoped():
    session = FakeSession({'marker': ok({'markers': []})})
    code, _, _, _ = run_cli(['marker', '--group', 'batch-1'], session)
    assert code == 0
    assert session.calls[0][1] == {'task_id': 'all', 'group_id': 'batch-1'}


def test_reference_result_cannot_hide_target_crash():
    session = FakeSession({'verdict': ok({'verdict': 'target_exited',
                                         'reference': {'status': 'comparable'}})})
    code, _, _, _ = run_cli(['verdict', '--task', 'x', '--vs-reference'], session)
    assert code == 30


@pytest.mark.parametrize('status', ['regression', 'comparable', 'incomparable'])
def test_crash_exit_code_has_priority_over_every_reference_result(status):
    session = FakeSession({'verdict': ok({'verdict': 'target_exited',
                                         'reference': {'status': status}})})
    code, _, _, _ = run_cli(['verdict', '--task', 'x', '--vs-reference'], session)
    assert code == 30


@pytest.mark.parametrize('extra', [['--args=x'], ['--args='], ['--cwd=x'], ['--close-on-stop']])
def test_template_rejects_launch_overrides(tmp_path, extra):
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps({'name': '方案', 'targets': [{'pid': 100}]}), encoding='utf-8')
    code, _, _, session = run_cli(['start', '--plan', str(path), *extra])
    assert code == 2 and session.calls == []


def test_old_automation_and_template_files_have_same_size_limit(tmp_path):
    path = tmp_path / 'too-big.json'
    path.write_bytes(b' ' * (1024 * 1024 + 1))
    code, _, errors, session = run_cli(['start', '--pid', '100', '--plan', str(path)])
    assert code == 2 and '1 MB' in errors and session.calls == []


def test_template_wait_uses_one_unbounded_request_not_serial_calls(tmp_path):
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps({'name': '方案', 'targets': [
        {'name': 'later.exe', 'wait': True}, {'pid': 100}]}), encoding='utf-8')
    session = FakeSession({'start_plan': ok({'started': 2, 'failed': 0, 'results': []})})
    code, _, _, _ = run_cli(['start', '--plan', str(path)], session)
    assert code == 0 and len(session.calls) == 1 and session.calls[0][2] is None
