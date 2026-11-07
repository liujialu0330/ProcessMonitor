"""
命令处理器的 start launch=… 与 status 的数据质量：

- launch 成功：以启动结果（PID、创建时间）建任务并开始，回复带 launched / close_on_stop；
- launch 失败：回 invalid_state、不建任务；
- 参数校验：与 pid/name 同给、带 wait、相对路径、未知字段、close_on_stop 非布尔都回
  invalid_args，且不启动任何程序；
- close_on_stop：用户停止后经后台线程调 launcher.terminate；进程自己退出时不调；
- status 的 quality：一句话文本 + 原始数值。

被测程序一律用 FakeLauncher：成功时返回本测试进程的 PID 与创建时间，采集线程有真实
进程可采，关闭只记录、不结束任何进程。
"""
import os
import sys

import psutil
import pytest

from core.contracts import StopReason
from core.ipc import protocol as p
from core.ipc.commands import CommandContext, CommandDispatcher, CommandSettings, quality_dict
from core.monitor_manager import MonitorManager
from core.process_launcher import LaunchResult, LaunchSpec
from core.shutdown import ThreadRegistry
from data.models import QualityStats
from tests.doubles import FakeLauncher
from tests.helpers import process_events_until, stop_and_join

EXE = sys.executable


class FakeConn:
    def __init__(self):
        self.sent = []

    def client_pid(self):
        return 4321

    def client_integrity(self):
        return None

    def on_closed(self, callback):
        pass

    def send_ok(self, request_id, result=None):
        self.sent.append({'id': request_id, 'ok': True, 'result': result})
        return True

    def send_error(self, request_id, code, message, **extra):
        self.sent.append({'id': request_id, 'ok': False, 'code': code, 'message': message,
                          **extra})
        return True

    def send_event(self, request_id, event, data=None):
        self.sent.append({'id': request_id, 'event': event, 'data': data})
        return True

    def flush(self, *args):
        pass


def _call(dispatcher, cmd, args=None):
    conn = FakeConn()
    dispatcher.handle(conn, p.Request(id=1, cmd=cmd, args=dict(args or {})))
    return conn.sent[-1]


@pytest.fixture
def manager(db, qapp):
    mgr = MonitorManager(db=db)
    yield mgr
    stop_and_join(mgr)


@pytest.fixture
def launcher():
    fake = FakeLauncher()
    me = psutil.Process()
    # 默认"启动"出来的就是本测试进程：采集线程有真实进程可采
    fake.result = LaunchResult(pid=os.getpid(), create_time=me.create_time(), name=me.name())
    return fake


@pytest.fixture
def registry(qapp):
    return ThreadRegistry()


@pytest.fixture
def dispatcher(manager, db, launcher, registry):
    settings = CommandSettings(automation_enabled=lambda: True,
                               default_metrics=lambda: ('memory_rss',))
    d = CommandDispatcher(app_version='9.9.9', settings=settings)
    d.set_context(CommandContext(manager=manager, db=db, auto_marker=None,
                                 registry=registry, launcher=launcher))
    yield d
    d.deleteLater()


def _launch(**extra):
    payload = {'exe': EXE, 'args': ['-c', 'pass'], 'cwd': os.path.dirname(EXE)}
    payload.update(extra)
    return payload


def test_start_launch_creates_task_from_launch_result(dispatcher, manager, launcher):
    reply = _call(dispatcher, 'start', {'launch': _launch(close_on_stop=True)})

    assert reply['ok'] is True, reply
    result = reply['result']
    assert result['launched'] is True and result['close_on_stop'] is True
    assert launcher.launched == [LaunchSpec(exe=EXE, args=('-c', 'pass'),
                                            cwd=os.path.dirname(EXE))]
    info = manager.get_task_info(result['task_id'])
    assert info.pid == os.getpid()
    assert abs(info.create_time - psutil.Process().create_time()) < 0.01


def test_start_launch_failure_is_invalid_state_and_creates_nothing(dispatcher, manager,
                                                                    launcher):
    launcher.fail("找不到程序：X")

    reply = _call(dispatcher, 'start', {'launch': _launch()})

    assert reply['code'] == p.ERR_INVALID_STATE
    assert "找不到程序" in reply['message']
    assert manager.running_task_count() == 0
    assert _call(dispatcher, 'status')['result']['tasks'] == []


@pytest.mark.parametrize('args', [
    {'launch': _launch(), 'pid': 1},
    {'launch': _launch(), 'name': 'a.exe'},
    {'launch': _launch(), 'wait': True},
    {'launch': {'exe': 'relative\\a.exe'}},
    {'launch': _launch(cwd='relative\\dir')},
    {'launch': _launch(env={'A': '1'})},
    {'launch': _launch(close_on_stop='yes')},
    {'launch': 'C:\\a.exe'},
    {'launch': {'exe': ''}},
])
def test_start_launch_bad_args_launch_nothing(dispatcher, launcher, args):
    reply = _call(dispatcher, 'start', args)

    assert reply['code'] == p.ERR_INVALID_ARGS, reply
    assert launcher.launched == []


def test_start_launch_refused_during_maintenance(dispatcher, manager, launcher, monkeypatch):
    monkeypatch.setattr(manager, 'is_maintenance_running', lambda: True)

    reply = _call(dispatcher, 'start', {'launch': _launch()})

    assert reply['code'] == p.ERR_BUSY
    assert launcher.launched == []


def test_close_on_stop_terminates_after_user_stop(dispatcher, manager, launcher):
    task_id = _call(dispatcher, 'start',
                    {'launch': _launch(close_on_stop=True)})['result']['task_id']

    assert _call(dispatcher, 'stop', {'task_id': task_id})['ok'] is True

    assert process_events_until(lambda: launcher.terminated, timeout=10)
    assert launcher.terminated == [(os.getpid(), launcher.result.create_time)]


def test_close_on_stop_ignores_other_stop_reasons(dispatcher, manager, launcher):
    task_id = _call(dispatcher, 'start',
                    {'launch': _launch(close_on_stop=True)})['result']['task_id']

    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    # 之后真正的用户停止也不再关闭：同一任务只处理一次
    manager.task_stopped.emit(task_id, StopReason.USER)

    assert not process_events_until(lambda: launcher.terminated, timeout=0.3)


def test_without_close_on_stop_user_stop_leaves_program(dispatcher, manager, launcher):
    task_id = _call(dispatcher, 'start', {'launch': _launch()})['result']['task_id']

    manager.task_stopped.emit(task_id, StopReason.USER)

    assert not process_events_until(lambda: launcher.terminated, timeout=0.3)


def test_status_lists_quality_for_running_task(dispatcher, manager):
    task_id = _call(dispatcher, 'start', {'launch': _launch()})['result']['task_id']

    [task] = _call(dispatcher, 'status')['result']['tasks']

    assert task['task_id'] == task_id
    assert 'quality' in task


def test_quality_dict_has_text_and_raw_numbers():
    quality = QualityStats(planned_cycles=1000, done_cycles=998, missed_cycles=2,
                           lag_p95_ms=12, collect_p95_ms=4, write_p95_ms=3)

    data = quality_dict(quality)

    assert data == {
        'text': "采集完成率 99.8%（998 / 1,000 周期，跳过 2，调度延迟 p95 12 ms）",
        'planned_cycles': 1000, 'done_cycles': 998, 'missed_cycles': 2,
        'completion_ratio': 0.998, 'lag_p95_ms': 12, 'collect_p95_ms': 4, 'write_p95_ms': 3,
    }
    assert quality_dict(None) is None
    # 计划周期为 0：没有可说的完成率，文本为 None，数值照给
    assert quality_dict(QualityStats(0, 0, 0))['text'] is None
