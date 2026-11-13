"""新解释器验证后台依赖边界，以及首次按需渲染的真实配置与退出路径。"""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest

from core.ipc import client as ipc
from tests.test_ipc_end_to_end import visible_top_level_windows
from utils.instance_name import pipe_path, single_instance_name


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(sys.platform != 'win32', reason='真实 Windows 后台管道')]

# 子进程在入口导入前安装拦截器。父pytest已经加载Fluent，不能证明后台的真实依赖边界。
_SERVER = r'''
import importlib.abc, json, os, sys
from pathlib import Path
data, audit = map(Path, sys.argv[1:3])
audit.mkdir(parents=True, exist_ok=True)
blocked = []
class DenyUi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (audit / 'allow_render').exists():
            return None
        if any(fullname == p or fullname.startswith(p + '.')
               for p in ('ui', 'qfluentwidgets', 'qframelesswindow', 'app_config')):
            blocked.append(fullname)
            raise ImportError('后台轻路径不应导入 ' + fullname)
        return None
sys.meta_path.insert(0, DenyUi())
from utils.runtime_env import configure_math_threads
configure_math_threads()
from app import bootstrap
from app.single_instance import SingleInstanceGuard
from core.headless import HeadlessRuntime
from PyQt5.QtCore import QTimer
ctx = runtime = None
listen_calls = []
def write(name, value):
    path = audit / name
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)
def state():
    value = {
        'blocked': list(blocked),
        'ui_modules': sorted(n for n in sys.modules if n == 'ui' or n.startswith('ui.')),
        'fluent': 'qfluentwidgets' in sys.modules,
        'listen_calls': list(listen_calls),
        'registry': len(ctx.registry.entries()) if ctx is not None else 0,
        'exports': ctx.manager.has_running_exports() if ctx is not None else False,
    }
    if 'qfluentwidgets' in sys.modules and 'app_config' in sys.modules:
        fluent = sys.modules['qfluentwidgets']
        cfg = sys.modules['app_config'].cfg
        value.update(theme_preference=cfg.get(cfg.themeMode).name,
                     report_scheme=cfg.get(cfg.report_color_scheme),
                     resolved_dark=fluent.isDarkTheme())
    return value
original_build = bootstrap.build_context
def build(*args, **kwargs):
    global ctx
    ctx = original_build(*args, **kwargs)
    return ctx
bootstrap.build_context = build
original_listen = SingleInstanceGuard.listen
def listen(self, *args, **kwargs):
    listen_calls.append(True)
    return original_listen(self, *args, **kwargs)
SingleInstanceGuard.listen = listen
original_start = HeadlessRuntime.start
def start(self):
    global runtime
    runtime = self
    original_start(self)
    self.snapshot_writer.written.connect(
        lambda task_id, paths: write('stage.json', {'task_id': task_id, 'paths': list(paths)}))
    self.snapshot_writer.failed.connect(
        lambda task_id, error: write('stage.json', {'task_id': task_id, 'error': error}))
    # 只约束这个测试启动的实例，失败时也会正常收尾，不留下监控进程。
    QTimer.singleShot(25000, self.quit)
HeadlessRuntime.start = start
last_command = None
def control():
    global last_command
    path = audit / 'control.json'
    if not path.exists():
        return
    request = json.loads(path.read_text(encoding='utf-8'))
    if request['id'] == last_command:
        return
    last_command = request['id']
    if request['action'] == 'stage':
        runtime.auto_marker.stage_snapshot_due.emit(
            request['task_id'], request['directory'], 1)
    write('reply.json', {'id': last_command, 'state': state()})
timer = None
original_trace = bootstrap._report_trace
def ready(trace):
    global timer
    original_trace(trace)
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(control)
    timer.start()
    write('ready.json', state())
bootstrap._report_trace = ready
code = bootstrap.run(['main.py', '--background', '--startup-trace',
                      '--idle-exit-s', '15', '--data-dir', str(data)],
                     exit_process=lambda value: None)
result = state()
result.update(exit_code=code, session_lock=(data / 'session.lock').exists())
write('final.json', result)
raise SystemExit(code)
'''


def _configuration(path, *, enabled=True, theme='Dark', report='follow'):
    path.mkdir(parents=True, exist_ok=True)
    config_path = path / 'config.json'
    # 高版本与未知键同时守护“首次图片接入配置不能重新落盘/丢字段”。
    config_path.write_text(json.dumps({
        'config_version': 99,
        'Monitor': {'DefaultInterval': 1, 'LastMetrics': ['memory_private'],
                    'KeepAwakeWhileMonitoring': False},
        'Automation': {'Enabled': enabled},
        'Export': {'ReportColorScheme': report, 'FutureExportSetting': '保留'},
        'QFluentWidgets': {'ThemeMode': theme, 'FontFamilies': ['Microsoft YaHei']},
        'FutureGroup': {'token': ['原样', 7]},
    }, ensure_ascii=False), encoding='utf-8')
    return config_path


def _environment(data):
    return dict(os.environ, PROCESS_MONITOR_DATA_DIR=str(data),
                QT_QPA_PLATFORM='windows', PYTHONIOENCODING='utf-8')


def _json_when(path, predicate=lambda value: True, *, process=None, log=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = json.loads(path.read_text(encoding='utf-8'))
            if predicate(value):
                return value
        if process is not None and process.poll() is not None:
            break
        time.sleep(0.025)
    detail = log.read_text(encoding='utf-8', errors='replace') if log and log.exists() else ''
    raise AssertionError(f'等待 {path.name} 未完成\n{detail}')


class _Server:
    def __init__(self, process, data, audit, log):
        self.process, self.data, self.audit, self.log = process, data, audit, log
        self.sequence = 0
        self.pipe = pipe_path(single_instance_name(os.path.abspath(data)))

    def call(self, command, payload=None):
        with ipc.IpcClient.connect(self.pipe) as client:
            return client.call(command, payload, timeout=20)

    def inspect(self, action='inspect', **kwargs):
        self.sequence += 1
        temporary = self.audit / 'control.tmp'
        temporary.write_text(json.dumps({'id': self.sequence, 'action': action, **kwargs}),
                             encoding='utf-8')
        temporary.replace(self.audit / 'control.json')
        return _json_when(self.audit / 'reply.json', lambda d: d['id'] == self.sequence,
                          process=self.process, log=self.log)['state']


@contextlib.contextmanager
def _running_server(data, audit):
    audit.mkdir(parents=True)
    log_path = audit / 'child.log'
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-c', _SERVER, str(data), str(audit)],
                                   cwd=ROOT, env=_environment(data), stdout=log,
                                   stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        server = _Server(process, data, audit, log_path)
        try:
            ready = _json_when(audit / 'ready.json', process=process, log=log_path)
            assert ready['blocked'] == [] and ready['ui_modules'] == []
            assert ready['fluent'] is False
            assert server.call('ping')['headless'] is True
            yield server
        finally:
            if process.poll() is None:
                try:
                    server.call('quit')
                except ipc.ClientError:
                    pass
                # 被测子进程有自己的25秒正常退出看门狗，仅等待本测试的实例。
                process.wait(timeout=30)
            assert process.returncode == 0, log_path.read_text(encoding='utf-8', errors='replace')
    final = _json_when(audit / 'final.json')
    assert final['exit_code'] == 0
    assert final['registry'] == 0 and final['exports'] is False
    assert final['session_lock'] is False
    assert final['blocked'] == []


def _sample_task(server):
    task = server.call('start', {'pid': os.getpid()})
    task_id = task['task_id']
    server.call('marker', {'task_id': task_id, 'label': '基线'})
    server.call('wait', {'task_id': task_id, 'until': 'elapsed>=2', 'timeout_s': 10})
    return task_id


def _stop_task(server, task_id):
    server.call('stop', {'task_id': task_id})
    server.call('wait', {'task_id': task_id, 'until': 'stopped', 'timeout_s': 10})


def _png(path):
    from PyQt5.QtGui import QImage
    image = QImage(str(path))
    assert not image.isNull() and image.width() == 1600
    return image


def test_background_light_commands_never_import_ui(tmp_path):
    data, audit = tmp_path / 'data', tmp_path / 'audit'
    config_path = _configuration(data)
    before = config_path.read_bytes()
    with _running_server(data, audit) as server:
        task_id = _sample_task(server)
        assert server.call('pause', {'task_id': task_id})['paused'] is True
        assert server.call('resume', {'task_id': task_id})['paused'] is False
        server.call('marker', {'task_id': task_id, 'label': '第1轮'})
        _stop_task(server, task_id)
        result = server.call('export', {'task_id': task_id, 'dir': str(tmp_path / 'csv'),
                                        'kinds': ['csv', 'summary']})
        assert len(result['paths']) == 2
        assert all(Path(path).is_file() and path.endswith('.csv') for path in result['paths'])
        server.call('verdict', {'task_id': task_id})
        state = server.inspect()
        assert state['ui_modules'] == [] and state['fluent'] is False
        assert visible_top_level_windows(server.process.pid) == []
    assert config_path.read_bytes() == before


def test_background_disabled_exits_before_listening_without_ui(tmp_path):
    data, audit = tmp_path / 'data', tmp_path / 'audit'
    config_path = _configuration(data, enabled=False)
    before = config_path.read_bytes()
    result = subprocess.run([sys.executable, '-c', _SERVER, str(data), str(audit)],
                            cwd=ROOT, env=_environment(data), capture_output=True,
                            text=True, encoding='utf-8', timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    final = _json_when(audit / 'final.json')
    assert final['listen_calls'] == [] and not (audit / 'ready.json').exists()
    assert final['blocked'] == [] and final['ui_modules'] == []
    assert final['fluent'] is False and final['session_lock'] is False
    assert config_path.read_bytes() == before


@pytest.mark.parametrize('theme,scheme,first_image', [
    ('Dark', 'follow', 'stage'), ('Dark', 'light', 'report'), ('Auto', 'follow', 'report'),
])
def test_background_lazy_images_preserve_config_and_no_visible_window(
        tmp_path, theme, scheme, first_image):
    data, audit = tmp_path / 'data', tmp_path / 'audit'
    config_path = _configuration(data, theme=theme, report=scheme)
    before = config_path.read_bytes()
    with _running_server(data, audit) as server:
        task_id = _sample_task(server)
        assert server.inspect()['ui_modules'] == []
        (audit / 'allow_render').write_text('图片路径才允许加载界面模块', encoding='utf-8')

        def stage():
            server.inspect('stage', task_id=task_id, directory=str(tmp_path / 'stage'))
            result = _json_when(audit / 'stage.json', process=server.process, log=server.log)
            assert 'error' not in result, result
            assert len(result['paths']) == 2 and all(Path(p).is_file() for p in result['paths'])
            return next(p for p in result['paths'] if p.endswith('.png'))

        def report():
            result = server.call('export', {'task_id': task_id, 'dir': str(tmp_path / 'report'),
                                            'kinds': ['report']})
            assert len(result['paths']) == 1
            return result['paths'][0]

        first = stage() if first_image == 'stage' else report()
        image = _png(first)
        state = server.inspect()
        assert state['fluent'] is True and state['ui_modules']
        assert state['theme_preference'] == theme.upper()
        assert state['report_scheme'] == scheme
        dark = scheme == 'follow' and state['resolved_dark']
        color = image.pixelColor(0, 0)
        assert (sum((color.red(), color.green(), color.blue())) < 384) is dark

        _png(report())
        if first_image != 'stage':
            _png(stage())
        _stop_task(server, task_id)
        result = server.call('export', {'task_id': task_id, 'dir': str(tmp_path / 'evidence'),
                                        'evidence': True})
        assert len(result['paths']) == 1
        with zipfile.ZipFile(result['paths'][0]) as archive:
            names = archive.namelist()
            assert any(name.endswith('.png') for name in names)
            assert len([name for name in names if name.endswith('.csv')]) == 2
            assert 'task.json' in names and 'README.txt' in names
            assert archive.testzip() is None
        assert visible_top_level_windows(server.process.pid) == []
    assert config_path.read_bytes() == before
