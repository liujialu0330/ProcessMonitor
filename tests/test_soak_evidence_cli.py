"""
证据包与命令行：崩溃任务的退出信息进入证据包 / CSV 元数据 / 命令行状态与判定。

- IPC export evidence=true：产出一个 zip（报告图片、明细与摘要 CSV、task.json、README），
  task.json 与 CSV 元数据写明退出码与中文说明、真实停止原因（崩溃）；
- 同一份数据按普通 export 导出时 CSV 元数据同样带退出行；
- verdict / status 回复附退出信息；
- pmctl export --evidence、run --evidence <目录> 的参数与调用顺序。
"""
import json
import os
import zipfile

import pytest
from PyQt5.QtGui import QImage

from core.contracts import StopReason
from core.ipc import protocol as p
from core.ipc.commands import CommandContext, CommandDispatcher, CommandSettings
from core.monitor_manager import MonitorManager
from core.shutdown import ThreadRegistry
import pmctl
from tests.helpers import process_events_until, stop_and_join
from tests.soak_doubles import ACCESS_VIOLATION, build_rig, run_to_end
from tests.test_ipc_server import FakeConn, _request
from tests.test_pmctl import FakeSession, ok, run_cli

METRIC = 'memory_rss'


@pytest.fixture
def started(qapp):
    workers = []
    yield workers
    for w in workers:
        w.stop()
        w.wait(5000)


def _fake_renderer(task, analysis, now):
    image = QImage(40, 30, QImage.Format_RGB32)
    image.fill(0xFFFFFF)
    return image


@pytest.fixture
def crashed(db, started):
    """一个以 0xC0000005 崩溃结束的任务（库里有数据、退出码与停止原因）。"""
    rig = build_rig(db, segments={100: [float(i) for i in range(1, 41)]},
                    codes={100: ACCESS_VIOLATION}, metrics=[METRIC], interval=1.0)
    run_to_end(rig.worker, started)
    assert rig.stops == [StopReason.CRASHED]
    return rig.worker.task_id


@pytest.fixture
def dispatcher(db, qapp):
    manager = MonitorManager(db=db)
    registry = ThreadRegistry()
    settings = CommandSettings(default_metrics=lambda: (METRIC,))
    d = CommandDispatcher(app_version='9.9.9', settings=settings)
    d.set_context(CommandContext(manager=manager, db=db, auto_marker=None, registry=registry,
                                 report_renderer=_fake_renderer))
    yield d
    d.deleteLater()
    registry.cancel_all()
    registry.shutdown_all()
    stop_and_join(manager)


def _call_async(dispatcher, cmd, args, timeout=30.0):
    conn = FakeConn()
    dispatcher.handle(conn, _request(cmd, args))
    assert process_events_until(lambda: bool(conn.sent), timeout=timeout), f"{cmd} 没有回复"
    return conn.sent[-1]


def _csv_text(path):
    with open(path, encoding='utf-8-sig') as f:
        return f.read()


# ========== IPC export evidence ==========

def test_export_evidence_produces_zip_with_exit_info(dispatcher, crashed, tmp_path):
    out = tmp_path / 'ev'
    reply = _call_async(dispatcher, 'export', {'task_id': crashed, 'dir': str(out),
                                               'evidence': True})
    assert reply['ok'] is True, reply
    paths = reply['result']['paths']
    assert len(paths) == 1 and paths[0].endswith('.zip') and os.path.isfile(paths[0])

    with zipfile.ZipFile(paths[0]) as z:
        names = z.namelist()
        assert 'task.json' in names
        assert any(n.lower().startswith('readme') for n in names)
        assert any(n.endswith('.png') for n in names)
        assert sum(1 for n in names if n.endswith('.csv')) == 2
        info = json.loads(z.read('task.json').decode('utf-8'))
        readme = z.read(next(n for n in names if n.lower().startswith('readme'))).decode('utf-8')
        detail_csv = next(n for n in names if n.endswith('.csv') and '摘要' not in n
                          and 'summary' not in n.lower())
        csv_head = z.read(detail_csv).decode('utf-8-sig')
    task = info['task']
    assert task['exit_code'] == ACCESS_VIOLATION
    assert task['exit_code_hex'] == '0xC0000005'
    assert task['exit_text'] == '0xC0000005（访问冲突）'
    assert task['exit_exception'] == '访问冲突'
    assert task['stop_reason'] == 'crashed'
    assert '访问冲突' in readme and '0xC0000005' in readme
    assert '退出: 异常退出 0xC0000005（访问冲突）' in csv_head


def test_export_evidence_does_not_overwrite_existing_zip(dispatcher, crashed, tmp_path):
    out = tmp_path / 'ev'
    first = _call_async(dispatcher, 'export', {'task_id': crashed, 'dir': str(out),
                                               'evidence': True})['result']['paths'][0]
    second = _call_async(dispatcher, 'export', {'task_id': crashed, 'dir': str(out),
                                                'evidence': True})['result']['paths'][0]
    assert first != second and os.path.isfile(first) and os.path.isfile(second)


def test_plain_export_csv_metadata_carries_exit_line(dispatcher, crashed, tmp_path):
    out = tmp_path / 'plain'
    reply = _call_async(dispatcher, 'export', {'task_id': crashed, 'dir': str(out),
                                               'kinds': ['csv']})
    assert reply['ok'] is True
    text = _csv_text(reply['result']['paths'][0])
    assert '退出: 异常退出 0xC0000005（访问冲突）' in text


def test_export_evidence_requires_renderer(db, qapp, tmp_path):
    manager = MonitorManager(db=db)
    d = CommandDispatcher(app_version='9.9.9')
    d.set_context(CommandContext(manager=manager, db=db, auto_marker=None, report_renderer=None))
    conn = FakeConn()
    d.handle(conn, _request('export', {'task_id': 'x', 'dir': str(tmp_path), 'evidence': True}))
    assert conn.sent[-1]['ok'] is False and conn.sent[-1]['code'] == p.ERR_INVALID_STATE
    d.deleteLater()
    stop_and_join(manager)


def test_export_evidence_flag_must_be_boolean(dispatcher, tmp_path):
    conn = FakeConn()
    dispatcher.handle(conn, _request('export', {'task_id': 'x', 'dir': str(tmp_path),
                                                'evidence': 'yes'}))
    assert conn.sent[-1]['code'] == p.ERR_INVALID_ARGS


# ========== verdict / status ==========

def test_verdict_reports_crash_exit_info(dispatcher, crashed):
    reply = _call_async(dispatcher, 'verdict', {'task_id': crashed})
    assert reply['ok'] is True, reply
    result = reply['result']
    assert result['verdict'] == 'target_exited'
    exit_info = result['target_exit']
    assert exit_info['reason'] == 'crashed'
    assert exit_info['exit_code'] == ACCESS_VIOLATION
    assert exit_info['exit_text'] == '0xC0000005（访问冲突）'
    assert 'segment' not in result       # 没重启过：不附分段说明


def test_verdict_notes_segment_when_task_restarted(dispatcher, db, crashed):
    from datetime import timedelta
    from tests.soak_doubles import T0
    db.markers.add(crashed, T0 + timedelta(seconds=-1), "第 2 次启动 PID 200", 'restart')
    reply = _call_async(dispatcher, 'verdict', {'task_id': crashed})
    assert reply['ok'] is True, reply
    assert reply['result']['segment'] == {'number': 2, 'count': 2,
                                          'note': '仅统计第 2 次启动后的数据'}


def test_status_task_dict_has_restart_and_exit_fields(dispatcher, db):
    manager = dispatcher.context.manager
    manager.set_worker_options(collector_factory=lambda pid, spec: _Forever())
    tid = manager.create_task(os.getpid(), 'app.exe', [METRIC], interval=0.05,
                              resume_on_restart=True, restart_limit=4)
    manager.start_task(tid)
    conn = FakeConn()
    dispatcher.handle(conn, _request('status', {}))
    task = next(t for t in conn.sent[-1]['result']['tasks'] if t['task_id'] == tid)
    assert task['waiting_restart'] is False
    assert (task['restart_count'], task['restart_limit']) == (0, 4)
    assert task['exit_code'] is None and task['exit_text'] is None


class _Forever:
    last_unavailable = {}

    def is_process_running(self):
        return True

    def get_create_time(self):
        return None

    def prime_cpu(self):
        pass

    def collect_metrics(self, metric_types):
        return {m: 1.0 for m in metric_types}


# ========== pmctl ==========

def test_pmctl_export_evidence_flag_sets_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = FakeSession({'export': ok({'paths': ['task.zip']})})
    code, out, *_ = run_cli(['export', '--task', 'T', '--dir', 'out', '--evidence'], session)
    assert code == 0 and 'task.zip' in out
    payload = session.calls[0][1]
    assert payload['evidence'] is True
    assert payload['dir'] == str(tmp_path / 'out')


def test_pmctl_export_without_flag_has_no_evidence_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = FakeSession({'export': ok({'paths': ['x.csv']})})
    run_cli(['export', '--task', 'T', '--dir', 'out'], session)
    assert 'evidence' not in session.calls[0][1]


def _run_session():
    def verdict(payload):
        return ok({'verdict': 'target_exited', 'task_id': payload['task_id'],
                   'target_exit': {'reason': 'crashed', 'before_stop_condition': True,
                                   'exit_code': ACCESS_VIOLATION,
                                   'exit_text': '0xC0000005（访问冲突）'},
                   'segment': {'number': 2, 'count': 2, 'note': '仅统计第 2 次启动后的数据'},
                   'metrics': []})

    return FakeSession({'start': ok({'task_id': 'T1', 'pid': 42, 'name': 'app.exe'}),
                        'wait': ok({'reached': True, 'stopped': True}),
                        'export': ok({'paths': ['a.csv']}), 'verdict': verdict})


def test_pmctl_run_evidence_exports_zip_between_export_and_verdict(tmp_path):
    session = _run_session()
    code, out, errs, _ = run_cli(['run', '--pid', '42', '--stop-after', '60',
                                  '--out', str(tmp_path / 'o'),
                                  '--evidence', str(tmp_path / 'ev')], session)
    assert code == pmctl.ipc.EXIT_TARGET_EXITED
    cmds = [(c, bool(pl.get('evidence'))) for c, pl, _ in session.calls]
    assert cmds == [('start', False), ('wait', False), ('export', False), ('export', True),
                    ('verdict', False)]
    evidence_call = session.calls[3][1]
    assert evidence_call['dir'] == str(tmp_path / 'ev') and evidence_call['task_id'] == 'T1'
    assert '已导出证据包' in out
    # 退出信息与分段说明显示在判定结果里
    assert '0xC0000005（访问冲突）' in out and '仅统计第 2 次启动后的数据' in out


def test_pmctl_run_evidence_without_out_still_exports_evidence(tmp_path):
    session = _run_session()
    run_cli(['run', '--pid', '42', '--stop-after', '60', '--evidence', str(tmp_path)], session)
    cmds = [(c, bool(pl.get('evidence'))) for c, pl, _ in session.calls]
    assert cmds == [('start', False), ('wait', False), ('export', True), ('verdict', False)]


def test_pmctl_status_shows_waiting_restart():
    session = FakeSession({'status': ok({'tasks': [{
        'task_id': 'T1', 'pid': 1, 'name': 'app.exe', 'status': 'running', 'paused': False,
        'waiting_restart': True, 'restart_count': 1, 'restart_limit': 3, 'rounds': 0}],
        'finished': [], 'waits': [], 'read_only': False})})
    code, out, *_ = run_cli(['status'], session)
    assert code == 0
    assert '等待被测进程重启（已续接 1/3 次）' in out
