"""
注销/关机（commitDataRequest）跳过关闭确认直接真退出；
监控页 task_stopped 槽内延后释放 MonitorTask 的最后引用。
"""
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QApplication

from core.monitor_manager import MonitorManager
from core.process_collector import ProcessCollector
from data.database import Database
from ui.main_window import MainWindow
from ui.pages.monitor_page import MonitorPage


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    db = Database(str(tmp_path / "session_end.db"))
    win = MainWindow(db=db)
    win.about_page.check_update = lambda *args, **kwargs: None
    win.show()
    yield win
    win.monitor_manager.stop_all_tasks()


def test_commit_data_request_skips_close_confirmation(window, monkeypatch):
    """会话结束：处理函数置 _really_quit，随后的 closeEvent 不弹三选确认、走真退出"""
    asked = []
    monkeypatch.setattr(window, '_ask_close_action', lambda: asked.append(1) or 'cancel')
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

    assert asked == []
    stop_all.assert_called_once()
    quit_mock.assert_called_once()
    event.accept.assert_called_once()
    event.ignore.assert_not_called()


def test_commit_data_request_signal_is_connected(qapp, tmp_path):
    """MainWindow 构造时把处理函数连接到 QApplication.commitDataRequest
    （无法在测试里构造真实 QSessionManager 来 emit，故校验接收者数量增加）"""
    app = QApplication.instance()
    before = app.receivers(app.commitDataRequest)
    win = MainWindow(db=Database(str(tmp_path / "signal.db")))
    try:
        assert app.receivers(app.commitDataRequest) == before + 1
    finally:
        win.monitor_manager.stop_all_tasks()
        win.close()


def test_task_stopped_defers_remove_task(qapp, db, monkeypatch):
    """卡片即时转已结束，remove_task 延后到下一轮事件循环"""
    monkeypatch.setattr(
        ProcessCollector, "get_all_processes", staticmethod(lambda: []))
    manager = MonitorManager(db=db)
    page = MonitorPage(manager=manager)
    removed = []
    manager.remove_task = lambda tid: removed.append(tid) or True

    page._on_task_stopped("t-unknown", "用户停止")
    assert removed == []
    QApplication.processEvents()
    assert removed == ["t-unknown"]
    page.close()
