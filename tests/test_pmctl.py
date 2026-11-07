"""
pmctl（仓库根 pmctl.py）：参数解析、退出码映射、run 串联流程（假会话）、JUnit XML 结构、
不依赖 PyQt5 / psutil / ui。
"""
import io
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

import pmctl
from core.ipc import client as ipc
from core.ipc.protocol import Response


def ok(result=None):
    return Response(id=1, ok=True, result=dict(result or {}),
                    raw={'v': 1, 'id': 1, 'ok': True, 'result': dict(result or {})})


def err(code, message="出错了"):
    error = {'code': code, 'message': message}
    return Response(id=1, ok=False, error_code=code, error_message=message, error=error,
                    raw={'v': 1, 'id': 1, 'ok': False, 'error': error})


class FakeSession:
    """按命令名脚本化的会话：handlers[cmd] 可以是 Response 或 (payload) -> Response。"""

    def __init__(self, handlers=None):
        self.handlers = handlers or {}
        self.calls = []
        self.closed = False

    def request(self, cmd, payload, timeout=None):
        self.calls.append((cmd, payload, timeout))
        handler = self.handlers.get(cmd, ok())
        if isinstance(handler, Exception):
            raise handler
        return handler(payload) if callable(handler) else handler

    def close(self):
        self.closed = True


def run_cli(argv, session=None, factory=None):
    out, errs = io.StringIO(), io.StringIO()
    session = session or FakeSession()
    code = pmctl.main(argv, session_factory=factory or (lambda args: session), out=out, err=errs)
    return code, out.getvalue(), errs.getvalue(), session


# ---------- 参数 ----------

def test_parser_has_all_subcommands():
    parser = pmctl.build_parser()
    sub = next(a for a in parser._actions if a.dest == 'command')
    assert set(sub.choices) == {'ping', 'status', 'start', 'marker', 'pause', 'resume', 'stop',
                                'wait', 'export', 'verdict', 'quit', 'run'}


@pytest.mark.parametrize('argv', [
    [], ['nope'], ['start'], ['start', '--pid', '1', '--name', 'a'], ['stop'],
    ['run', '--stop-after', '5', '--rounds', '3', '--pid', '1'], ['--timeout', 'x', 'ping'],
])
def test_bad_arguments_exit_2(argv, capsys):
    code, out, errs, session = run_cli(argv)
    assert code == ipc.EXIT_USAGE
    assert session.calls == []


def test_help_exits_0(capsys):
    assert pmctl.main(['--help'], session_factory=lambda a: FakeSession()) == 0
    assert "退出码" in capsys.readouterr().out


def test_run_needs_target():
    code, out, errs, session = run_cli(['run', '--stop-after', '5'])
    assert code == ipc.EXIT_USAGE
    assert "--pid" in errs


def test_bad_plan_file_exit_2(tmp_path):
    bad = tmp_path / 'plan.json'
    bad.write_text('[1, 2]', encoding='utf-8')
    code, out, errs, session = run_cli(['start', '--pid', '1', '--plan', str(bad)])
    assert code == ipc.EXIT_USAGE
    code, *_ = run_cli(['start', '--pid', '1', '--plan', str(tmp_path / 'missing.json')])
    assert code == ipc.EXIT_USAGE


# ---------- 退出码 ----------

@pytest.mark.parametrize('error_code, expected', [
    ('bad_request', 2), ('unknown_command', 2), ('invalid_args', 2), ('disabled', 3),
    ('not_ready', 3), ('not_found', 1), ('invalid_state', 1), ('busy', 1), ('forbidden', 1),
    ('timeout', 1), ('internal', 1),
])
def test_server_errors_map_to_exit_codes(error_code, expected):
    code, out, errs, session = run_cli(['stop', '--task', 't1'],
                                       FakeSession({'stop': err(error_code)}))
    assert code == expected
    assert error_code in errs
    assert session.closed


@pytest.mark.parametrize('exc', [ipc.PipeNotFound("没有"), ipc.PipeTimeout("超时"),
                                 ipc.IdentityError("冒名"), ipc.PipeClosed("断开")])
def test_connection_failures_exit_3(exc):
    def factory(args):
        raise exc
    code, out, errs, session = run_cli(['ping'], factory=factory)
    assert code == ipc.EXIT_CONNECTION
    assert str(exc) in errs


@pytest.mark.parametrize('verdict, expected', [
    ('stable', 0), ('rising', 10), ('insufficient', 20), ('target_exited', 30), ('???', 1),
])
def test_verdict_exit_codes(verdict, expected):
    code, out, errs, session = run_cli(['verdict', '--task', 't1'],
                                       FakeSession({'verdict': ok({'verdict': verdict,
                                                                   'metrics': []})}))
    assert code == expected
    assert session.calls == [('verdict', {'task_id': 't1'}, None)]


def test_wait_not_reached_exit_1():
    code, out, *_ = run_cli(['wait', '--task', 't', '--until', 'rounds>=3', '--timeout-s', '9'],
                            FakeSession({'wait': ok({'reached': False})}))
    assert code == ipc.EXIT_ERROR
    code, out, errs, session = run_cli(['wait', '--task', 't'],
                                       FakeSession({'wait': ok({'reached': True})}))
    assert code == 0
    assert session.calls == [('wait', {'task_id': 't', 'until': 'stopped'}, None)]


def test_json_prints_raw_response():
    code, out, errs, session = run_cli(['--json', 'ping'],
                                       FakeSession({'ping': ok({'pid': 5, 'ready': True})}))
    assert code == 0
    assert json.loads(out) == {'v': 1, 'id': 1, 'ok': True, 'result': {'pid': 5, 'ready': True}}


def test_json_prints_error_response_too():
    code, out, *_ = run_cli(['--json', 'status'], FakeSession({'status': err('disabled')}))
    assert code == 3
    assert json.loads(out)['error']['code'] == 'disabled'


# ---------- 命令参数 ----------

def test_start_payload(tmp_path):
    plan = tmp_path / 'plan.json'
    plan.write_text('{"timed_marker_seconds": 60}', encoding='utf-8')
    session = FakeSession({'start': ok({'task_id': 'T'})})
    code, out, *_ = run_cli(['--timeout', '7', 'start', '--name', 'a.exe',
                             '--metrics', 'memory_rss, handles', '--interval', '2',
                             '--note', '夜间', '--plan', str(plan)], session)
    assert code == 0 and out.strip() == 'T'
    assert session.calls == [('start', {'name': 'a.exe', 'metrics': ['memory_rss', 'handles'],
                                        'interval': 2.0, 'note': '夜间',
                                        'plan': {'timed_marker_seconds': 60}}, 7.0)]


def test_start_wait_has_no_client_timeout():
    session = FakeSession({'start': ok({'task_id': 'T'})})
    run_cli(['start', '--name', 'a.exe', '--wait', '--wait-timeout', '30'], session)
    assert session.calls == [('start', {'name': 'a.exe', 'wait': True,
                                        'wait_timeout_s': 30.0}, None)]


def test_export_uses_absolute_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = FakeSession({'export': ok({'paths': ['x.csv']})})
    code, out, *_ = run_cli(['export', '--task', 'T', '--dir', 'out', '--kinds', 'csv'], session)
    assert code == 0 and 'x.csv' in out
    assert session.calls[0][1] == {'task_id': 'T', 'dir': str(tmp_path / 'out'),
                                   'kinds': ['csv']}


def test_marker_defaults_to_all():
    session = FakeSession({'marker': ok({'markers': []})})
    run_cli(['marker'], session)
    assert session.calls[0][1] == {'task_id': 'all'}


# ---------- run 串联 ----------

def _run_session(verdicts):
    """两个目标：start 依次分配 T1/T2；verdict 按任务回给定的判定。"""
    counter = iter(range(1, 10))

    def start(payload):
        n = next(counter)
        return ok({'task_id': f'T{n}', 'pid': payload.get('pid', 100 + n),
                   'name': payload.get('name', 'app.exe')})

    def verdict(payload):
        v = verdicts[payload['task_id']]
        return ok({'verdict': v, 'task_id': payload['task_id'], 'metrics': [
            {'key': 'memory_rss', 'name': '内存', 'verdict': 'rising' if v == 'rising' else
             'stable', 'reason': None, 'samples': 9, 'rate_per_hour': 1.5}]})

    return FakeSession({'start': start, 'verdict': verdict,
                        'wait': ok({'reached': True, 'stopped': True}),
                        'export': ok({'paths': ['a.csv']})})


def test_run_chains_commands_and_takes_most_severe(tmp_path):
    session = _run_session({'T1': 'stable', 'T2': 'rising'})
    junit = tmp_path / 'r' / 'leak.xml'
    code, out, errs, _ = run_cli(['run', '--pid', '42', '--name', 'app.exe', '--interval', '1',
                                  '--rounds-every', '60', '--stop-after', '600',
                                  '--out', str(tmp_path), '--junit', str(junit)], session)
    assert code == ipc.EXIT_RISING
    cmds = [(c, p.get('task_id')) for c, p, _ in session.calls]
    assert cmds == [('start', None), ('start', None),
                    ('wait', 'T1'), ('export', 'T1'), ('verdict', 'T1'),
                    ('wait', 'T2'), ('export', 'T2'), ('verdict', 'T2')]
    start = session.calls[0][1]
    assert start['pid'] == 42 and start['interval'] == 1.0
    assert start['plan'] == {'timed_marker_seconds': 60, 'stop_after_seconds': 600}
    wait = session.calls[2][1]
    assert wait['until'] == 'stopped' and wait['timeout_s'] == 600 + pmctl.RUN_STOP_MARGIN_S
    assert session.calls[3][1]['dir'] == str(tmp_path)
    assert junit.exists()


def test_run_rounds_plan_and_no_export_without_out():
    session = _run_session({'T1': 'insufficient'})
    code, *_ = run_cli(['run', '--pid', '1', '--rounds-every', '30', '--rounds', '5'], session)
    assert code == ipc.EXIT_INSUFFICIENT
    assert session.calls[0][1]['plan'] == {'timed_marker_seconds': 30, 'stop_after_rounds': 5}
    # 等待上限 = 轮数 × 定时打点间隔 + 余量（修复前不设上限，可能永远等下去）
    assert session.calls[1][1]['timeout_s'] == 5 * 30 + pmctl.RUN_STOP_MARGIN_S
    assert [c for c, *_ in session.calls] == ['start', 'wait', 'verdict']


def test_run_target_exited_beats_rising():
    session = _run_session({'T1': 'rising', 'T2': 'target_exited', 'T3': 'stable'})
    code, *_ = run_cli(['run', '--pid', '1', '--pid', '2', '--pid', '3', '--stop-after', '60'],
                       session)
    assert code == ipc.EXIT_TARGET_EXITED


def test_run_stops_on_server_error():
    session = _run_session({})
    session.handlers['start'] = err('not_found')
    code, out, errs, _ = run_cli(['run', '--pid', '1', '--stop-after', '60'], session)
    assert code == ipc.EXIT_ERROR
    assert [c for c, *_ in session.calls] == ['start']


# ---------- JUnit ----------

def _results():
    return [
        {'target': 'a.exe（PID 1）', 'verdict': 'rising', 'metrics': [
            {'key': 'memory_rss', 'name': '内存', 'verdict': 'rising', 'rate_per_hour': 3.0},
            {'key': 'handles', 'name': '句柄', 'verdict': 'stable'},
            {'key': 'gdi', 'name': 'GDI', 'verdict': 'insufficient', 'reason': 'few_points'},
            {'key': 'cpu', 'name': 'CPU', 'verdict': 'not_judged'},
        ]},
        {'target': 'b.exe（PID 2）', 'verdict': 'target_exited', 'metrics': [
            {'key': 'memory_rss', 'name': '内存', 'verdict': 'stable'},
        ]},
    ]


def test_junit_structure(tmp_path):
    path = tmp_path / 'junit.xml'
    pmctl.write_junit(str(path), _results(), elapsed_s=12.5)
    root = ET.parse(str(path)).getroot()
    assert root.tag == 'testsuite'
    assert root.get('name') == '进程监控助手'
    assert root.get('tests') == '5'
    assert root.get('failures') == '1'
    assert root.get('errors') == '1'
    assert root.get('skipped') == '2'
    assert root.get('time') == '12.500'
    cases = root.findall('testcase')
    assert [(c.get('classname'), c.get('name')) for c in cases] == [
        ('a.exe（PID 1）', '内存'), ('a.exe（PID 1）', '句柄'), ('a.exe（PID 1）', 'GDI'),
        ('a.exe（PID 1）', 'CPU'), ('b.exe（PID 2）', '内存')]
    rising, stable, insufficient, not_judged, exited = cases
    assert rising.find('failure') is not None and 'rate_per_hour=3.0' in rising.find('failure').text
    assert stable.find('failure') is None and stable.find('skipped') is None
    assert stable.find('error') is None
    assert insufficient.find('skipped') is not None
    assert not_judged.find('skipped') is not None
    assert exited.find('error') is not None and exited.find('error').get('type') == 'target_exited'


def test_junit_without_metrics_has_one_case():
    tree = pmctl.build_junit([{'task_id': 'T', 'verdict': 'insufficient', 'metrics': []}])
    cases = tree.getroot().findall('testcase')
    assert len(cases) == 1 and cases[0].find('skipped') is not None


def test_junit_written_by_run_is_parseable(tmp_path):
    path = tmp_path / 'x.xml'
    run_cli(['run', '--pid', '1', '--stop-after', '60', '--junit', str(path)],
            _run_session({'T1': 'stable'}))
    root = ET.parse(str(path)).getroot()
    assert root.get('tests') == '1' and root.get('failures') == '0'


# ---------- 依赖与实例名 ----------

def test_pmctl_does_not_import_qt_or_psutil():
    code = ("import sys, pmctl; pmctl.resolve_instance_name(None); "
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('PyQt5', 'psutil', 'ui', 'qfluentwidgets', 'data')); print(bad)")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.abspath(pmctl.__file__)), timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == '[]'


def test_instance_name_follows_data_dir(tmp_path):
    from utils.instance_name import single_instance_name
    from app.paths import AppPaths
    name = pmctl.resolve_instance_name(str(tmp_path))
    assert name == single_instance_name(AppPaths.resolve(
        argv=['x', '--data-dir', str(tmp_path)]).data_dir)
    assert name != pmctl.resolve_instance_name(str(tmp_path / 'other'))


def test_pmclient_example_is_small_and_stdlib_only():
    path = os.path.join(os.path.dirname(os.path.abspath(pmctl.__file__)), 'tools', 'pmclient.py')
    with open(path, encoding='utf-8') as f:
        source = f.read()
    assert len(source.splitlines()) < 50
    for banned in ('PyQt5', 'psutil', 'qfluentwidgets', 'from ui', 'import ui'):
        assert banned not in source


# ---------- 启动被测程序 ----------

def test_start_launch_payload(tmp_path):
    exe = tmp_path / 'app' / 'demo.exe'
    session = FakeSession({'start': ok({'task_id': 'T'})})
    code, out, *_ = run_cli(['start', '--launch', str(exe), '--args', '--mode "a b" -v',
                             '--cwd', str(tmp_path), '--close-on-stop', '--interval', '2'],
                            session)
    assert code == 0 and out.strip() == 'T'
    [(cmd, payload, _timeout)] = session.calls
    assert cmd == 'start'
    assert payload == {'launch': {'exe': str(exe), 'args': ['--mode', 'a b', '-v'],
                                  'cwd': str(tmp_path), 'close_on_stop': True},
                       'interval': 2.0}


def test_start_launch_relative_paths_become_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = FakeSession({'start': ok({'task_id': 'T'})})
    run_cli(['start', '--launch', 'bin\\demo.exe', '--cwd', 'work'], session)
    launch = session.calls[0][1]['launch']
    assert launch == {'exe': str(tmp_path / 'bin' / 'demo.exe'), 'args': [],
                      'cwd': str(tmp_path / 'work')}


@pytest.mark.parametrize('argv', [
    ['start', '--launch', 'C:\\a.exe', '--pid', '1'],
    ['start', '--launch', 'C:\\a.exe', '--name', 'a.exe'],
    ['start', '--pid', '1', '--close-on-stop'],
    ['start', '--name', 'a.exe', '--args', '-v'],
    ['start', '--pid', '1', '--cwd', 'C:\\'],
    ['start', '--launch', 'C:\\a.exe', '--wait'],
])
def test_start_launch_option_conflicts_exit_2(argv):
    session = FakeSession()
    code, *_ = run_cli(argv, session)
    assert code == 2
    assert session.calls == []


def test_run_with_launch_target(tmp_path):
    exe = tmp_path / 'demo.exe'
    session = _run_session({'T1': 'stable'})
    code, *_ = run_cli(['run', '--launch', str(exe), '--close-on-stop', '--stop-after', '60'],
                       session)
    assert code == 0
    start = [payload for cmd, payload, _ in session.calls if cmd == 'start']
    assert start == [{'launch': {'exe': str(exe), 'args': [], 'close_on_stop': True},
                      'plan': {'stop_after_seconds': 60.0}}]


def test_status_prints_quality_text():
    session = FakeSession({'status': ok({'tasks': [
        {'task_id': 'T1', 'name': 'a.exe', 'pid': 1, 'status': 'running', 'rounds': 2,
         'quality': {'text': '采集完成率 99.8%（998 / 1,000 周期）', 'planned_cycles': 1000}},
        {'task_id': 'T2', 'name': 'b.exe', 'pid': 2, 'status': 'running', 'rounds': 0,
         'quality': None},
    ]})})
    code, out, *_ = run_cli(['status'], session)
    assert code == 0
    first, second = out.strip().splitlines()
    assert first.endswith('采集完成率 99.8%（998 / 1,000 周期）')
    assert '采集完成率' not in second


# ---------- 评审修复：run 的停止条件、等待上限、超时停止、清理与判定 ----------

def test_run_without_stop_condition_is_usage_error():
    session = _run_session({'T1': 'stable'})
    code, out, errs, _ = run_cli(['run', '--pid', '1', '--rounds-every', '60'], session)
    assert code == ipc.EXIT_USAGE
    assert "停止条件" in errs
    assert session.calls == []


def test_run_accepts_stop_condition_from_plan_file(tmp_path):
    plan = tmp_path / 'plan.json'
    plan.write_text('{"stop_after_rounds": 3, "timed_marker_seconds": 10}', encoding='utf-8')
    session = _run_session({'T1': 'stable'})
    code, *_ = run_cli(['run', '--pid', '1', '--plan', str(plan)], session)
    assert code == 0
    assert session.calls[1][1]['timeout_s'] == 3 * 10 + pmctl.RUN_STOP_MARGIN_S


@pytest.mark.parametrize('plan, limit', [
    ({'stop_after_seconds': 600}, 660.0),
    ({'stop_after_rounds': 5, 'timed_marker_seconds': 30}, 210.0),
    ({'stop_after_seconds': 600, 'stop_after_rounds': 5, 'timed_marker_seconds': 30}, 210.0),
    ({'stop_after_seconds': 100, 'stop_after_rounds': 5, 'timed_marker_seconds': 30}, 160.0),
    ({'stop_after_rounds': 5}, None),               # 轮数靠日志或手动打点：无法估计
    ({}, None),
])
def test_run_wait_limit(plan, limit):
    assert pmctl.run_wait_limit(plan) == limit


def _timeout_session(verdict='stable', stop_reply=None):
    """第一次 wait 超时未停止；stop 之后的 wait 停止。"""
    session = _run_session({'T1': verdict})
    waits = []

    def wait(payload):
        waits.append(payload)
        if len(waits) == 1:
            return ok({'reached': False, 'timed_out': True, 'stopped': False})
        return ok({'reached': True, 'stopped': True})

    session.handlers['wait'] = wait
    if stop_reply is not None:
        session.handlers['stop'] = stop_reply
    return session, waits


def test_run_timeout_stops_then_exports_and_judges(tmp_path):
    session, waits = _timeout_session('rising')
    junit = tmp_path / 'j.xml'
    code, out, errs, _ = run_cli(['run', '--pid', '1', '--stop-after', '600',
                                  '--out', str(tmp_path), '--junit', str(junit)], session)
    assert code == ipc.EXIT_RISING                     # 退出码按判定，不按超时
    assert [(c, p.get('task_id')) for c, p, _ in session.calls] == [
        ('start', None), ('wait', 'T1'), ('stop', 'T1'), ('wait', 'T1'), ('export', 'T1'),
        ('verdict', 'T1')]
    assert waits[0]['timeout_s'] == 660.0
    assert waits[1] == {'task_id': 'T1', 'until': 'stopped',
                        'timeout_s': pmctl.RUN_STOP_GRACE_S}
    assert "等待超时" in out and "超时停止" in out
    root = ET.parse(str(junit)).getroot()
    props = {p.get('name'): p.get('value') for p in root.find('properties')}
    assert 'stopped_by_timeout' in props and 'app.exe' in props['stopped_by_timeout']
    case = root.find('testcase')
    assert case.find('failure').text.startswith("超时停止")
    assert "超时停止" in root.find('system-out').text


def test_run_timeout_when_task_ends_just_before_stop(tmp_path):
    """发 stop 时任务恰好自己结束（invalid_state）：照常等收尾、判定。"""
    session, _ = _timeout_session('stable', stop_reply=err('invalid_state'))
    code, *_ = run_cli(['run', '--pid', '1', '--stop-after', '60'], session)
    assert code == 0
    assert [c for c, *_ in session.calls] == ['start', 'wait', 'stop', 'wait', 'verdict']


def test_run_second_start_failure_stops_first_task():
    session = _run_session({})
    starts = []

    def start(payload):
        starts.append(payload)
        if len(starts) == 2:
            return err('not_found')
        return ok({'task_id': 'T1', 'pid': 1, 'name': 'a.exe'})

    session.handlers['start'] = start
    code, out, errs, _ = run_cli(['run', '--pid', '1', '--pid', '2', '--stop-after', '60'],
                                 session)
    assert code == ipc.EXIT_ERROR
    assert [(c, p.get('task_id')) for c, p, _ in session.calls] == [
        ('start', None), ('start', None), ('stop', 'T1')]
    assert session.calls[-1][2] == pmctl.CLEANUP_STOP_TIMEOUT_S
    assert "T1" in errs and "已停止" in errs


def test_run_ctrl_c_stops_started_tasks_and_reconnects():
    first = _run_session({})

    def interrupted(payload):
        raise KeyboardInterrupt

    first.handlers['wait'] = interrupted
    second = FakeSession()
    sessions = [first, second]
    code, out, errs, _ = run_cli(['run', '--pid', '1', '--pid', '2', '--stop-after', '60'],
                                 factory=lambda args: sessions.pop(0))
    assert code == ipc.EXIT_CONNECTION
    assert first.closed                                # 被打断的连接丢弃，重连后再停
    assert [(c, p.get('task_id')) for c, p, _ in second.calls] == [('stop', 'T1'),
                                                                    ('stop', 'T2')]


def test_run_target_exit_after_stop_condition_is_reported_not_judged():
    session = _run_session({'T1': 'stable'})

    def verdict(payload):
        return ok({'verdict': 'stable', 'metrics': [],
                   'target_exit': {'reason': 'process_exited', 'before_stop_condition': False}})

    session.handlers['verdict'] = verdict
    code, out, *_ = run_cli(['run', '--pid', '1', '--rounds', '3', '--rounds-every', '10'],
                            session)
    assert code == 0
    assert "停止条件达成后退出" in out and "process_exited" in out


def test_default_session_factory_launches_with_idle_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(ipc, 'open_session', lambda name, **kw: calls.append((name, kw)) or 'S')
    args = pmctl.build_parser().parse_args(['ping'])
    assert pmctl.default_session_factory(args) == 'S'
    launch = calls[0][1]['launch']
    assert launch[launch.index('--idle-exit-s') + 1] == '300'
    assert calls[0][1]['on_waiting'] is not None
    args = pmctl.build_parser().parse_args(['--idle-exit', '0', 'ping'])
    pmctl.default_session_factory(args)
    launch = calls[1][1]['launch']
    assert launch[launch.index('--idle-exit-s') + 1] == '0'
    args = pmctl.build_parser().parse_args(['--no-launch', 'ping'])
    pmctl.default_session_factory(args)
    assert calls[2][1]['launch'] is None


def test_waiting_notice_goes_to_stderr(monkeypatch):
    def fake_open(name, **kw):
        kw['on_waiting'](ipc.MSG_WAITING_READY)
        return FakeSession({'ping': ok({'pid': 1, 'app_version': 't'})})

    monkeypatch.setattr(ipc, 'open_session', fake_open)
    out, errs = io.StringIO(), io.StringIO()
    code = pmctl.main(['ping'], out=out, err=errs)
    assert code == 0
    assert "正在等待实例就绪" in errs.getvalue() and "正在等待" not in out.getvalue()


def test_launched_instance_disabled_exits_3_with_hint():
    def factory(args):
        raise ipc.LaunchedProcessExited(0)
    code, out, errs, _ = run_cli(['ping'], factory=factory)
    assert code == ipc.EXIT_CONNECTION
    assert "设置里已关闭命令行控制" in errs


# ---------- R10：JUnit 按 Surefire / Ant 结构（手写结构断言，不引第三方 schema 库） ----------

SUITE_ATTRS = {'name', 'tests', 'failures', 'errors', 'skipped', 'time', 'timestamp', 'hostname'}


def _assert_surefire(root):
    """testsuite 根：必需属性齐全且计数自洽；子元素顺序为 properties? testcase* system-out?
    system-err?；testcase 有 classname/name/time，至多一个 failure/error/skipped。"""
    import re
    assert root.tag == 'testsuite'
    assert SUITE_ATTRS <= set(root.attrib), set(root.attrib)
    for key in ('tests', 'failures', 'errors', 'skipped'):
        assert root.get(key).isdigit()
    assert re.fullmatch(r'\d+\.\d{3}', root.get('time'))
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', root.get('timestamp'))
    assert root.get('hostname')
    order = {'properties': 0, 'testcase': 1, 'system-out': 2, 'system-err': 3}
    tags = [child.tag for child in root]
    assert set(tags) <= set(order), tags
    assert [order[t] for t in tags] == sorted(order[t] for t in tags)
    assert tags.count('properties') <= 1 and tags.count('system-out') <= 1
    assert tags.count('system-err') <= 1
    for prop in root.find('properties') if root.find('properties') is not None else []:
        assert prop.tag == 'property' and {'name', 'value'} <= set(prop.attrib)
    counts = {'failure': 0, 'error': 0, 'skipped': 0}
    cases = root.findall('testcase')
    for case in cases:
        assert {'classname', 'name', 'time'} <= set(case.attrib)
        assert re.fullmatch(r'\d+\.\d{3}', case.get('time'))
        outcome = [c.tag for c in case if c.tag in counts]
        assert len(outcome) <= 1
        for tag in outcome:
            counts[tag] += 1
            node = case.find(tag)
            if tag != 'skipped':
                assert 'type' in node.attrib and 'message' in node.attrib
        assert set(c.tag for c in case) <= set(counts) | {'system-out', 'system-err'}
    assert int(root.get('tests')) == len(cases)
    assert int(root.get('failures')) == counts['failure']
    assert int(root.get('errors')) == counts['error']
    assert int(root.get('skipped')) == counts['skipped']


def test_junit_follows_surefire_structure(tmp_path):
    from datetime import datetime
    results = _results()
    results[0]['timeout_stopped'] = True
    tree = pmctl.build_junit(results, 3.25, timestamp=datetime(2026, 1, 2, 3, 4, 5),
                             hostname='ci-01')
    root = tree.getroot()
    _assert_surefire(root)
    assert root.get('timestamp') == '2026-01-02T03:04:05' and root.get('hostname') == 'ci-01'
    tags = [child.tag for child in root]
    assert tags[0] == 'properties' and tags[-2:] == ['system-out', 'system-err']
    # 写到文件再读回（带 XML 声明、UTF-8）结构不变
    path = tmp_path / 'r.xml'
    pmctl.write_junit(str(path), results, 3.25)
    assert path.read_bytes().startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    _assert_surefire(ET.parse(str(path)).getroot())
    _assert_surefire(pmctl.build_junit([]).getroot())
