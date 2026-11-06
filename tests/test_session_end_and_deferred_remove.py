"""
注销/关机（commitDataRequest）跳过关闭确认直接真退出；
监控页 task_stopped 槽直接调用 remove_task（不阻塞、不再延后一轮），线程对象
由 manager 在 QThread.finished 后自行移除释放，卡片保留为"已结束"。
"""
import os
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QApplication

from core.contracts import StopReason
from core.monitor_manager import MonitorManager
from core.monitor_task import WorkerState
from data.database import Database
from tests.factories import make_main_window, make_monitor_page
from tests.helpers import process_events_until, stop_and_join
from utils.metrics import MetricType


@pytest.fixture
def window(tmp_path, qapp):
    db = Database(str(tmp_path / "session_end.db"))
    win = make_main_window(db, MonitorManager(db=db))
    win.about_group.check_update = lambda *args, **kwargs: None
    win.show()
    yield win
    stop_and_join(win.monitor_manager)


def test_commit_data_request_skips_close_confirmation(window, monkeypatch):
    """会话结束：处理函数置 _really_quit，随后的 closeEvent 不弹三选确认、走真退出"""
    window.services.dialogs.always('ask_close_action', 'cancel')
    monkeypatch.setattr(window.monitor_manager, 'has_running_tasks', lambda: True)
    stop_all = MagicMock()
    monkeypatch.setattr(window.monitor_manager, 'stop_all_tasks', stop_all)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    assert window._really_quit is False
    window._on_commit_data_request(MagicMock())
    assert window._really_quit is True

    event = MagicMock()
    window.closeEvent(event)

    assert window.services.dialogs.calls_of('ask_close_action') == []
    stop_all.assert_called_once()
    quit_mock.assert_called_once()
    event.accept.assert_called_once()
    event.ignore.assert_not_called()


def test_commit_data_request_signal_is_connected(qapp, tmp_path):
    """MainWindow 构造时把处理函数连接到 QApplication.commitDataRequest
    （无法在测试里构造真实 QSessionManager 来 emit，故校验接收者数量增加）"""
    app = QApplication.instance()
    before = app.receivers(app.commitDataRequest)
    db = Database(str(tmp_path / "signal.db"))
    win = make_main_window(db, MonitorManager(db=db))
    try:
        assert app.receivers(app.commitDataRequest) == before + 1
    finally:
        stop_and_join(win.monitor_manager)
        win.about_group.check_update = lambda *a, **k: None
        win.hide()


def test_task_stopped_calls_remove_task_directly(qapp, db):
    """task_stopped 槽内直接调用 remove_task：它不阻塞，无需再延后一轮事件循环"""
    manager = MonitorManager(db=db)
    page = make_monitor_page(db, manager)  # 默认替身进程表为空，不枚举本机进程
    removed = []
    manager.remove_task = lambda tid: removed.append(tid) or True

    page.presenter.on_task_stopped("t-unknown", StopReason.USER)
    assert removed == ["t-unknown"]
    page.close()


def test_real_stop_card_kept_and_worker_removed_on_finished(qapp, db):
    """真实线程走完整链路：停止 → 卡片即时转"已结束" → 线程 finished 后 manager
    移除任务并释放对象；卡片仍保留，配额不再计入"""
    from PyQt5 import sip

    manager = MonitorManager(db=db)
    page = make_monitor_page(db, manager)
    removed = []
    manager.task_removed.connect(removed.append)
    try:
        task_id = manager.create_task(os.getpid(), "self.exe", [MetricType.NUM_THREADS], 0.05)
        worker = manager.get_task(task_id)
        assert manager.start_task(task_id)

        assert manager.stop_task(task_id) is True
        assert worker.state in (WorkerState.STOPPING, WorkerState.STOPPED)
        assert process_events_until(lambda: removed == [task_id])

        card = page.board.cards()[task_id]
        assert card.is_finished
        assert manager.get_task_info(task_id) is None
        assert process_events_until(lambda: sip.isdeleted(worker))
        assert manager.running_task_count() == 0
        summary = page.board.summary()
        assert summary.startswith("运行中 0/") and summary.endswith("已结束 1")
    finally:
        stop_and_join(manager)
        page.close()
