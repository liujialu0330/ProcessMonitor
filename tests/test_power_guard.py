"""
监控期间阻止系统空闲睡眠（core.power_guard.PowerGuard）与配置项 keep_awake_while_monitoring

系统调用由记录调用序列的假 set_state 代替，不触碰真实的 SetThreadExecutionState。
"""
import logging
import os
import threading
import time

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import qconfig

from app_config import AppConfig, cfg
from core.power_guard import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, PowerGuard

HOLD = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
RELEASE = ES_CONTINUOUS


class FakeManager(QObject):
    """只有 PowerGuard 用到的信号与 running_task_count()"""
    task_started = pyqtSignal(str)
    task_stopped = pyqtSignal(str, object)
    task_removed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = set()

    def running_task_count(self) -> int:
        return len(self.running)

    def start(self, task_id):
        self.running.add(task_id)
        self.task_started.emit(task_id)

    def stop(self, task_id):
        # 与真实管理器一致：发 task_stopped 时运行计数已不含该任务
        self.running.discard(task_id)
        self.task_stopped.emit(task_id, None)


class RecordingSetState:
    def __init__(self, result=1, error=None):
        self.calls = []
        self.threads = []
        self.result = result
        self.error = error

    def __call__(self, flags):
        self.calls.append(flags)
        self.threads.append(threading.get_ident())
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def manager(qapp):
    return FakeManager()


@pytest.fixture
def set_state():
    return RecordingSetState()


@pytest.fixture(autouse=True)
def _reset_keep_awake():
    """conftest 不复位本配置项：用例前后自行回到默认值"""
    item = cfg.keep_awake_while_monitoring
    item.value = item.defaultValue
    yield
    item.value = item.defaultValue


def _process_until(condition, timeout=5.0) -> bool:
    """处理事件循环直到 condition() 为真（排队信号要靠事件循环送达）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


def _guard_warnings(caplog):
    return [r for r in caplog.records
            if r.name == 'core.power_guard' and r.levelno >= logging.WARNING]


# ---------- 任务启停 ----------

def test_holds_once_and_releases_after_all_tasks_stop(manager, set_state):
    guard = PowerGuard(manager, enabled=True, set_state=set_state)
    assert set_state.calls == [] and not guard.is_holding()

    manager.start('a')
    assert set_state.calls == [HOLD] and guard.is_holding()

    manager.start('b')            # 第 2 个任务不重复调用
    assert set_state.calls == [HOLD]

    manager.stop('a')             # 仍有任务在跑：不撤销
    assert set_state.calls == [HOLD] and guard.is_holding()

    manager.stop('b')             # 全部停止：撤销一次
    assert set_state.calls == [HOLD, RELEASE] and not guard.is_holding()

    manager.task_removed.emit('b')  # 之后的移除信号不再调用
    assert set_state.calls == [HOLD, RELEASE]


def test_task_removed_releases_when_stopped_signal_missing(manager, set_state):
    """task_stopped 漏发时，task_removed 兜底撤销"""
    guard = PowerGuard(manager, enabled=True, set_state=set_state)
    manager.start('a')
    manager.running.discard('a')
    manager.task_removed.emit('a')
    assert set_state.calls == [HOLD, RELEASE] and not guard.is_holding()


def test_constructed_with_running_task_holds_immediately(manager, set_state):
    manager.running.add('a')
    guard = PowerGuard(manager, enabled=True, set_state=set_state)
    assert set_state.calls == [HOLD] and guard.is_holding()


class _StubCollector:
    """替代真实采集：不读系统，只回一个固定值"""
    last_unavailable = {}

    def is_process_running(self):
        return True

    def prime_cpu(self):
        pass

    def collect_metrics(self, metric_types):
        return {"memory_rss": 1.0}


def test_real_manager_holds_while_task_runs(qapp, db, set_state):
    """真实 MonitorManager：task_started 发出时计数已含新任务，task_stopped 送达时已不含"""
    from core.monitor_manager import MonitorManager
    manager = MonitorManager(db=db)
    guard = PowerGuard(manager, enabled=True, set_state=set_state)
    owner = threading.get_ident()

    task_id = manager.create_task(os.getpid(), "t.exe", ["memory_rss"], 0.05)
    manager.get_task(task_id).collector = _StubCollector()
    assert manager.start_task(task_id) is True
    assert set_state.calls == [HOLD] and guard.is_holding()

    assert manager.stop_task(task_id) is True
    assert _process_until(lambda: set_state.calls == [HOLD, RELEASE])
    assert not guard.is_holding()
    assert set(set_state.threads) == {owner}
    manager.join_all(5000)


# ---------- 开关 ----------

def test_disabled_never_calls(manager, set_state):
    guard = PowerGuard(manager, enabled=False, set_state=set_state)
    manager.start('a')
    manager.stop('a')
    assert set_state.calls == [] and not guard.is_holding()


def test_disable_while_holding_releases_immediately(manager, set_state):
    guard = PowerGuard(manager, enabled=True, set_state=set_state)
    manager.start('a')
    guard.set_enabled(False)
    assert set_state.calls == [HOLD, RELEASE] and not guard.is_holding()

    manager.start('b')            # 关闭期间任务启停都不调用
    manager.stop('a')
    assert set_state.calls == [HOLD, RELEASE]


def test_enable_with_running_task_holds_immediately(manager, set_state):
    guard = PowerGuard(manager, enabled=False, set_state=set_state)
    manager.start('a')
    guard.set_enabled(True)
    assert set_state.calls == [HOLD] and guard.is_holding()
    guard.set_enabled(True)       # 重复开启不重复调用
    assert set_state.calls == [HOLD]


def test_enable_without_tasks_does_not_call(manager, set_state):
    guard = PowerGuard(manager, enabled=False, set_state=set_state)
    guard.set_enabled(True)
    assert set_state.calls == [] and not guard.is_holding()


def test_follows_config_item(manager, set_state):
    """from_config_item：初值取配置，设置页开关改配置后即时生效"""
    qconfig.set(cfg.keep_awake_while_monitoring, False, save=False)
    guard = PowerGuard.from_config_item(manager, cfg.keep_awake_while_monitoring,
                                        set_state=set_state)
    assert not guard.is_enabled()
    manager.start('a')
    assert set_state.calls == []

    qconfig.set(cfg.keep_awake_while_monitoring, True, save=False)
    assert guard.is_enabled() and set_state.calls == [HOLD]

    qconfig.set(cfg.keep_awake_while_monitoring, False, save=False)
    assert set_state.calls == [HOLD, RELEASE] and not guard.is_holding()


# ---------- 线程 ----------

def test_calls_stay_on_owner_thread_when_signalled_from_worker(qapp, manager, set_state):
    """SetThreadExecutionState 是线程级的：工作线程发出的信号也必须回到界面线程调用"""
    guard = PowerGuard(manager, enabled=True, set_state=set_state)
    owner = threading.get_ident()

    def worker():
        manager.running.add('a')
        manager.task_started.emit('a')
        guard.set_enabled(True)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert _process_until(lambda: set_state.calls == [HOLD])
    assert set(set_state.threads) == {owner}

    def stopper():
        manager.stop('a')
        guard.set_enabled(False)

    t = threading.Thread(target=stopper)
    t.start()
    t.join()
    assert _process_until(lambda: set_state.calls == [HOLD, RELEASE])
    assert set(set_state.threads) == {owner}


# ---------- 降级 ----------

@pytest.mark.parametrize('set_state', [RecordingSetState(result=0),
                                       RecordingSetState(error=OSError("拒绝"))],
                         ids=['returns_zero', 'raises'])
def test_failure_degrades_and_logs_once(manager, set_state, caplog):
    caplog.set_level(logging.INFO, logger='core.power_guard')
    guard = PowerGuard(manager, enabled=True, set_state=set_state)

    manager.start('a')
    assert set_state.calls == [HOLD]
    assert not guard.is_holding() and not guard.is_available()

    manager.stop('a')
    manager.start('b')
    guard.set_enabled(False)
    guard.set_enabled(True)
    assert set_state.calls == [HOLD]           # 降级后不再调用
    assert len(_guard_warnings(caplog)) == 1    # 只记一次


def test_unavailable_platform_is_noop(manager, monkeypatch, caplog):
    import core.power_guard as power_guard
    monkeypatch.setattr(power_guard, 'default_set_state', lambda: None)
    caplog.set_level(logging.INFO, logger='core.power_guard')

    guard = PowerGuard(manager, enabled=True)
    manager.start('a')
    manager.stop('a')
    assert not guard.is_available() and not guard.is_holding()
    assert len([r for r in caplog.records if r.name == 'core.power_guard']) == 1


# ---------- 配置项 ----------

def test_config_default_is_true():
    assert cfg.get(cfg.keep_awake_while_monitoring) is True


def test_config_roundtrip(tmp_path):
    config_path = str(tmp_path / "config.json")
    qconfig.load(config_path, cfg)
    qconfig.set(cfg.keep_awake_while_monitoring, False)

    cfg2 = AppConfig()
    qconfig.load(config_path, cfg2)
    assert cfg2.get(cfg2.keep_awake_while_monitoring) is False
