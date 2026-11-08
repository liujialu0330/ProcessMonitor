"""
主窗口关闭行为验证（托盘驻留与退出路径——本项目风险最高的改动之一，覆盖退出
路径的几个不变式）

覆盖：
① 默认配置（close_to_tray=False）关闭仍走既有"清理 -> QApplication.quit()"路径；
② 托盘"退出"（_really_quit）与 About 页"立即安装"（quit_for_install）都绕开
   隐藏分支，走同一条真退出路径；
③ 隐藏到托盘不停止任务、不做任何清理；
④ 清理步骤中途抛异常时 QApplication.quit() 仍必须执行（try/finally）；
⑤ 无托盘环境下隐藏分支天然短路；
⑥ 真退出的收尾只经 ShutdownCoordinator（停任务 → 带超时 join → 登记表线程 →
   关库），先隐藏窗口再收尾，进度经 on_progress 报告。

构造真实 MainWindow（tmp 库与真实 MonitorManager 注入，服务用替身；模式对齐
tests/e2e/test_gui_smoke.py）而非 mock 整个窗口，只有真实对象才能验证到实际
调用链路。"关闭到托盘"偏好由 FakePreferences 给出，关窗三选一由 ScriptedDialogs
预设；托盘用 _FakeTray 顶替 TrayController（offscreen 下真实托盘不可用）。
"""
import threading
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QThread
from PyQt5.QtWidgets import QApplication

import ui.main_window as main_window_module
from core.shutdown import (
    PROGRESS_SAVING, PROGRESS_STOPPING_TASKS, PROGRESS_STOPPING_THREADS,
)
from core.monitor_manager import MonitorManager
from data.database import Database
from tests.doubles import FakePreferences, RecordingNotifier, ScriptedDialogs
from tests.factories import make_main_window, make_services
from tests.helpers import dispose_widget, stop_and_join
from ui.components.dialogs import CloseActionDialog
from ui.services import (
    EVENT_QUICK_MARKER, EVENT_TRAY_HIDDEN, EVENT_UPDATE_AVAILABLE, ManagerTaskStatus,
)
from utils.metrics import MetricType


@pytest.fixture
def window(tmp_path, qapp):
    """构造真实 MainWindow（tmp 库），并做好测试隔离：覆写 about_group.
    check_update 为 no-op，防止 3 秒后 QTimer 触发真实联网检查更新（对齐
    tests/e2e/test_gui_smoke.py 的同一手法）。用例结束后兜底停止残留任务，
    避免遗留 QThread。

    关窗三选一未预设：意外询问时 ScriptedDialogs 直接断言失败而不是弹出模态
    对话框阻塞测试；需要验证该分支的用例自行预设返回值（见 _patch_ask）。
    """
    db = Database(str(tmp_path / "close_behavior.db"))
    manager = MonitorManager(db=db)
    services = make_services(
        notifier=RecordingNotifier(), dialogs=ScriptedDialogs(),
        preferences=FakePreferences(), task_status=ManagerTaskStatus(manager))
    win = make_main_window(db, manager, services)
    win.about_group.check_update = lambda *args, **kwargs: None
    win.show()
    yield win
    stop_and_join(win.monitor_manager)
    dispose_widget(win)


def _enable_close_to_tray(window):
    """模拟用户在设置页打开"关闭到系统托盘"开关"""
    window.services.preferences.tray = True


class _FakeTray:
    """顶替 TrayController 的"托盘可用"替身（offscreen/CI 下真实托盘不可用，
    MainWindow 的 TrayController.is_available() 恒为 False）"""

    def __init__(self):
        self.icon = object()
        self.messages = []
        self.tooltips = []
        self.hidden = False

    def is_available(self):
        return True

    def show_message(self, title, content, kind='info'):
        self.messages.append((title, content))

    def set_tooltip(self, text):
        self.tooltips.append(text)

    def hide(self):
        self.hidden = True


def _fake_event():
    """伪造一个记录 accept/ignore 调用的 QCloseEvent 替身：closeEvent 方法体
    只调用了 event.accept()/event.ignore() 两个方法，MagicMock 足以承接并
    留痕这两个调用，不需要构造真实 Qt 事件对象"""
    return MagicMock()


def test_close_quits_when_tray_disabled(window, monkeypatch):
    """不变式①：默认配置（close_to_tray=False）关闭仍走既有清理 -> quit 路径。
    FakePreferences 的关闭到托盘默认即为 False。"""
    assert window.services.preferences.close_to_tray() is False

    stop_all = MagicMock(wraps=window.monitor_manager.stop_all_tasks)
    monkeypatch.setattr(window.monitor_manager, 'stop_all_tasks', stop_all)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    stop_all.assert_called_once()
    quit_mock.assert_called_once()
    event.accept.assert_called_once()
    event.ignore.assert_not_called()


def test_close_shutdown_order_through_coordinator(window, monkeypatch):
    """真退出的固定顺序：stop_all_tasks → 登记表线程统一取消 → join_all(3000) →
    登记表线程等待 → 关库；两次等待拿到同一个截止时间；主窗口不再自己遍历任务线程"""
    calls, deadlines = [], []
    monkeypatch.setattr(window.monitor_manager, 'stop_all_tasks',
                        lambda: calls.append('stop_all'))
    monkeypatch.setattr(window.monitor_manager, 'join_all',
                        lambda timeout_ms, deadline=None: (
                            calls.append(('join_all', timeout_ms)),
                            deadlines.append(deadline)) and True)
    monkeypatch.setattr(window.monitor_manager, 'get_all_tasks',
                        lambda: pytest.fail("主窗口不应直接取任务线程对象"))
    monkeypatch.setattr(window.services.threads, 'cancel_all',
                        lambda: calls.append('cancel_threads'))
    monkeypatch.setattr(window.services.threads, 'shutdown_all',
                        lambda on_progress=None, deadline=None: (
                            calls.append('threads'), deadlines.append(deadline)) and True)
    monkeypatch.setattr(window.db, 'close', lambda: calls.append('db_close'))
    monkeypatch.setattr(QApplication, 'quit', lambda: calls.append('quit'))

    window.closeEvent(_fake_event())

    assert calls == ['stop_all', 'cancel_threads', ('join_all', 3000), 'threads',
                     'db_close', 'quit']
    assert deadlines[0] is not None and deadlines[0] == deadlines[1]


class _RecordingCoordinator:
    """顶替 ShutdownCoordinator：记录构造参数与 run 调用时窗口是否已隐藏"""
    instances = []

    def __init__(self, manager, registry, **kwargs):
        self.manager, self.registry, self.kwargs = manager, registry, kwargs
        self.runs = []
        _RecordingCoordinator.instances.append(self)

    def run(self, on_progress=None):
        self.runs.append(on_progress)
        on_progress("测试进度")
        return True


def test_close_runs_coordinator_with_window_dependencies(window, monkeypatch):
    """closeEvent 只调协调器：注入 manager、页面共用的线程登记表与数据库；
    运行前先隐藏窗口（即时反馈），进度经 _on_shutdown_progress 记录"""
    _RecordingCoordinator.instances = []
    hidden_at_run = []
    monkeypatch.setattr(main_window_module, 'ShutdownCoordinator', _RecordingCoordinator)
    monkeypatch.setattr(_RecordingCoordinator, 'run',
                        lambda self, on_progress=None: (
                            hidden_at_run.append(window.isHidden()),
                            self.runs.append(on_progress),
                            on_progress("测试进度"))[-1] or True)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    event = _fake_event()

    window.closeEvent(event)

    [coordinator] = _RecordingCoordinator.instances
    assert coordinator.manager is window.monitor_manager
    assert coordinator.registry is window.services.threads
    assert coordinator.kwargs == {'db': window.db}
    assert coordinator.runs == [window._on_shutdown_progress]
    assert hidden_at_run == [True]
    assert window.shutdown_progress == ["测试进度"]
    event.accept.assert_called_once()
    quit_mock.assert_called_once()


def test_close_reports_real_progress_in_order(window, monkeypatch):
    """真实协调器：停任务、结束后台线程、保存三段进度按顺序报告，并显示在托盘悬停文字上"""
    window.tray = _FakeTray()
    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    thread = _SlowThread(0.2)
    window.services.threads.register(thread, name="慢线程", timeout_ms=3000)
    thread.start()
    assert thread.started_evt.wait(5)

    window.closeEvent(_fake_event())

    assert window.shutdown_progress == [
        PROGRESS_STOPPING_TASKS, PROGRESS_STOPPING_THREADS, PROGRESS_SAVING]
    assert window.tray.tooltips[-1].endswith(PROGRESS_SAVING)
    assert thread.isFinished()
    assert window.tray.hidden is True


def test_close_logs_when_shutdown_exceeds_budget(window, monkeypatch, caplog):
    """收尾未在期限内完成：只记日志，照常 accept 并退出（不再无限等待）"""
    monkeypatch.setattr(main_window_module, 'ShutdownCoordinator', _RecordingCoordinator)
    monkeypatch.setattr(_RecordingCoordinator, 'run', lambda self, on_progress=None: False)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    event = _fake_event()

    with caplog.at_level('ERROR', logger='ui.main_window'):
        window.closeEvent(event)

    assert any("退出收尾未在期限内全部完成" in r.getMessage() for r in caplog.records)
    event.accept.assert_called_once()
    quit_mock.assert_called_once()


def test_close_hides_when_tray_enabled(window, monkeypatch):
    """不变式③：托盘启用（close_to_tray=True 且托盘可用）时关闭改为
    隐藏窗口，不停止任务、不做任何清理，event 被 ignore 而非 accept"""
    _enable_close_to_tray(window)
    window.tray = _FakeTray()

    stop_all = MagicMock()
    monkeypatch.setattr(window.monitor_manager, 'stop_all_tasks', stop_all)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    assert window.isHidden() is False  # 前置：fixture 里已 show()

    event = _fake_event()
    window.closeEvent(event)

    assert window.isHidden() is True
    stop_all.assert_not_called()
    quit_mock.assert_not_called()
    event.ignore.assert_called_once()
    event.accept.assert_not_called()
    assert window._tray_tip_shown is True
    # 首次隐藏到托盘的提示经 Notifier 发出（隐藏后它会改发托盘气泡）
    tips = [n for n in window.services.notifier.messages() if n.event == EVENT_TRAY_HIDDEN]
    assert len(tips) == 1

    # 首次提示只弹一次：再关闭一次（先还原可见性，模拟用户从托盘还原后再关）
    # 不应重复追加气泡
    window.show()
    window.closeEvent(_fake_event())
    assert len([n for n in window.services.notifier.messages()
                if n.event == EVENT_TRAY_HIDDEN]) == 1


def test_really_quit_bypasses_tray(window, monkeypatch):
    """不变式②（前半）：_really_quit=True 时即使 close_to_tray=True 也绕开
    隐藏分支，走完整清理 -> quit 的真退出路径"""
    _enable_close_to_tray(window)
    window.tray = _FakeTray()
    window._really_quit = True

    stop_all = MagicMock(wraps=window.monitor_manager.stop_all_tasks)
    monkeypatch.setattr(window.monitor_manager, 'stop_all_tasks', stop_all)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    stop_all.assert_called_once()
    quit_mock.assert_called_once()
    event.accept.assert_called_once()
    event.ignore.assert_not_called()


def test_quit_for_install_bypasses_tray(window, monkeypatch):
    """不变式②（后半）：quit_for_install() 等价于置
    _really_quit=True 后调用 close()，同样绕开托盘隐藏分支，走真退出路径——
    对应 About 页"立即安装"场景：即使托盘开关开启，也不能让安装向导启动后
    主程序仍常驻托盘占用文件"""
    _enable_close_to_tray(window)
    window.tray = _FakeTray()
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    assert window._really_quit is False

    window.quit_for_install()

    assert window._really_quit is True
    quit_mock.assert_called_once()
    assert window.isHidden() is True  # 真退出路径下 close() 使窗口被隐藏/关闭


def test_about_group_install_request_goes_through_quit_for_install(window, monkeypatch):
    """关于页只发 install_requested，主窗口连接到 quit_for_install（不靠 hasattr 反向调用）"""
    _enable_close_to_tray(window)
    window.tray = _FakeTray()
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    window.about_group.install_requested.emit()

    assert window._really_quit is True
    quit_mock.assert_called_once()


def test_tray_quit_request_goes_through_real_quit(window, monkeypatch):
    """托盘控制器的 quit_requested 连到真退出；show_requested 还原窗口"""
    _enable_close_to_tray(window)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    window.hide()
    window.tray.show_requested.emit()
    assert window.isHidden() is False

    window.tray.quit_requested.emit()
    assert window._really_quit is True
    quit_mock.assert_called_once()


# ========== 注销/关机（commitDataRequest） ==========

def test_session_end_while_hidden_to_tray_runs_coordinator_once(window, monkeypatch):
    """隐藏到托盘时注销/关机：Qt 的会话结束流程只关闭可见窗口，处理函数必须自己
    close()，让隐藏态也进 closeEvent 走协调器；之后再来的关闭事件不重复收尾"""
    _enable_close_to_tray(window)
    window.tray = _FakeTray()
    _RecordingCoordinator.instances = []
    monkeypatch.setattr(main_window_module, 'ShutdownCoordinator', _RecordingCoordinator)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    window.close()                       # 关闭到托盘：只隐藏
    assert window.isHidden() and _RecordingCoordinator.instances == []

    window._on_commit_data_request(MagicMock())

    [coordinator] = _RecordingCoordinator.instances
    assert len(coordinator.runs) == 1
    quit_mock.assert_called_once()
    assert window.isHidden()

    # Qt 随后的关闭流程（或任何重复关闭）不再收尾第二次
    event = _fake_event()
    window.closeEvent(event)
    window._on_commit_data_request(MagicMock())
    assert len(_RecordingCoordinator.instances) == 1 and len(coordinator.runs) == 1
    event.accept.assert_called_once()
    quit_mock.assert_called_once()


def _session_end_cancelled(window, monkeypatch):
    """会话结束信号到来但随后被取消、窗口没有真正关闭（close 被顶替为空操作）"""
    with monkeypatch.context() as m:
        m.setattr(window, 'close', lambda: None)
        window._on_commit_data_request(MagicMock())
    assert window._really_quit is True


@pytest.mark.parametrize("restore", ["tray_menu", "show"])
def test_cancelled_session_end_restores_close_confirmation(window, monkeypatch, restore):
    """会话结束被取消后窗口重新显示：会话结束置的真退出标志被复位，之后有任务时
    关窗仍弹三选确认，不会跳过确认直接全停"""
    _start_running_task(window)
    _session_end_cancelled(window, monkeypatch)

    window.hide()
    if restore == "tray_menu":
        window._show_from_tray()
    else:
        window.show()
    assert window._really_quit is False

    _patch_ask(window, 'cancel')
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    event = _fake_event()
    window.closeEvent(event)

    assert len(window.services.dialogs.calls_of('ask_close_action')) == 1
    event.ignore.assert_called_once()
    quit_mock.assert_not_called()
    assert window.monitor_manager.has_running_tasks()


def test_cancelled_session_end_still_hides_to_tray(window, monkeypatch):
    """会话结束被取消后窗口重新显示：开启关闭到托盘时关窗仍只隐藏，不停任务"""
    _enable_close_to_tray(window)
    window.tray = _FakeTray()
    _session_end_cancelled(window, monkeypatch)
    window.hide()
    window._show_from_tray()

    stop_all = MagicMock()
    monkeypatch.setattr(window.monitor_manager, 'stop_all_tasks', stop_all)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    event = _fake_event()
    window.closeEvent(event)

    assert window.isHidden()
    event.ignore.assert_called_once()
    stop_all.assert_not_called()
    quit_mock.assert_not_called()


def test_show_does_not_clear_tray_quit_flag(window):
    """窗口显示只复位会话结束置的标志：托盘退出/立即安装置的真退出标志保持不变"""
    window._really_quit = True
    window.hide()
    window.show()
    window._show_from_tray()
    assert window._really_quit is True


def test_cleanup_exception_still_quits(window, monkeypatch):
    """不变式④：收尾中途抛异常，QApplication.quit() 仍
    必须在 finally 中被调用——否则 setQuitOnLastWindowClosed(False) 拆掉了
    "closeEvent 异常也能退出"的隐性保险丝后，异常会导致事件循环永不退出且
    窗口已被清理/隐藏，用户无法通过任何界面操作恢复"""
    quit_mock = MagicMock()
    monkeypatch.setattr(main_window_module, 'ShutdownCoordinator', _RecordingCoordinator)
    monkeypatch.setattr(_RecordingCoordinator, 'run', MagicMock(
        side_effect=RuntimeError("模拟清理异常")))
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    window.tray = _FakeTray()

    event = _fake_event()
    with pytest.raises(RuntimeError, match="模拟清理异常"):
        window.closeEvent(event)

    quit_mock.assert_called_once()
    assert window.tray.hidden is True   # finally 里先隐藏托盘图标


def test_task_stop_failure_inside_coordinator_still_quits(window, monkeypatch):
    """停任务抛异常由协调器吞掉并记日志：后续步骤照走，正常 accept 并退出"""
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    with monkeypatch.context() as m:
        m.setattr(window.monitor_manager, 'stop_all_tasks',
                  MagicMock(side_effect=RuntimeError("模拟清理异常")))
        event = _fake_event()
        window.closeEvent(event)

    event.accept.assert_called_once()
    quit_mock.assert_called_once()


# ========== 仍有任务在监控时的关闭确认 ==========

def _start_running_task(window):
    """在真实 MonitorManager 上启动一个监控当前进程的任务（真线程）"""
    import os
    task_id = window.monitor_manager.create_task(
        os.getpid(), "pytest.exe", [MetricType.NUM_THREADS], 1.0)
    assert task_id
    assert window.monitor_manager.start_task(task_id)
    assert window.monitor_manager.has_running_tasks()
    return task_id


def _patch_ask(window, answer):
    """预设关窗三选一的回答；返回该对话框的调用记录（每次询问一条参数字典）"""
    dialogs = window.services.dialogs
    dialogs.always('ask_close_action', answer)
    return dialogs.calls_of('ask_close_action')


def test_close_with_running_task_cancel_keeps_everything(window, monkeypatch):
    """选"取消"：忽略关闭事件，任务继续跑，不清理、不退出"""
    _start_running_task(window)
    _patch_ask(window, 'cancel')
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    asked = window.services.dialogs.calls_of('ask_close_action')
    assert len(asked) == 1
    assert asked[0]["running_count"] == 1
    assert asked[0]["parent"] is window
    event.ignore.assert_called_once()
    event.accept.assert_not_called()
    quit_mock.assert_not_called()
    assert window.monitor_manager.has_running_tasks()
    assert window.isHidden() is False


def test_close_confirmation_warns_about_data_held_for_a_full_disk(window, monkeypatch):
    """磁盘已满、数据只留在内存里时，退出确认明确告诉用户"退出后将丢失"和条数"""
    _start_running_task(window)
    monkeypatch.setattr(window.monitor_manager, 'held_point_count', lambda: 1234)
    _patch_ask(window, 'cancel')
    window.closeEvent(_fake_event())
    asked = window.services.dialogs.calls_of('ask_close_action')
    assert asked[0]["notice"] == "有 1234 条因磁盘已满未写入的数据，退出后将丢失"


def test_close_confirmation_has_no_notice_without_held_data(window, monkeypatch):
    _start_running_task(window)
    _patch_ask(window, 'cancel')
    window.closeEvent(_fake_event())
    assert "notice" not in window.services.dialogs.calls_of('ask_close_action')[0]


def test_close_action_dialog_shows_the_notice_label(qapp):
    from PyQt5.QtWidgets import QWidget
    host = QWidget()
    host.resize(800, 600)
    text = "有 3 条因磁盘已满未写入的数据，退出后将丢失"
    with_notice = CloseActionDialog(1, can_tray=True, parent=host, notice=text)
    without = CloseActionDialog(1, can_tray=True, parent=host)
    assert with_notice.notice_label.text() == text
    assert not hasattr(without, 'notice_label')
    host.deleteLater()


def test_close_with_running_task_quit_stops_and_quits(window, monkeypatch):
    """选"停止监控并退出"：走完整清理 -> quit 路径，任务被停止"""
    _start_running_task(window)
    _patch_ask(window, 'quit')
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    quit_mock.assert_called_once()
    event.accept.assert_called_once()
    assert window.monitor_manager.running_task_count() == 0
    assert all(task.isFinished() for task in window.monitor_manager.get_all_tasks())


def test_close_with_running_task_tray_hides_without_stopping(window, monkeypatch):
    """选"最小化到托盘"（托盘可用）：隐藏窗口，不停止任务、不退出"""
    window.tray = _FakeTray()
    _start_running_task(window)
    _patch_ask(window, 'tray')
    stop_all = MagicMock()
    monkeypatch.setattr(window.monitor_manager, 'stop_all_tasks', stop_all)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    assert window.isHidden() is True
    stop_all.assert_not_called()
    quit_mock.assert_not_called()
    event.ignore.assert_called_once()
    assert window.monitor_manager.has_running_tasks()
    monkeypatch.undo()  # 还原 stop_all_tasks，供 fixture 收尾真正停止任务


def test_close_with_running_task_really_quit_skips_confirmation(window, monkeypatch):
    """_really_quit（托盘退出 / 立即安装）绕开确认，直接真退出"""
    _start_running_task(window)
    window._really_quit = True
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    quit_mock.assert_called_once()
    event.accept.assert_called_once()


def test_close_without_running_task_does_not_ask(window, monkeypatch):
    """没有运行中任务时不弹确认（fixture 未预设关窗三选一，一旦询问即断言失败）"""
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    quit_mock.assert_called_once()
    event.accept.assert_called_once()


def test_ask_close_action_passes_count_and_tray_availability(window):
    """_ask_close_action 经 services.dialogs 询问：传入运行中任务数与托盘是否可用，
    原样返回 quit / tray / cancel（对话框按钮到取值的映射见 test_services）"""
    dialogs = window.services.dialogs
    dialogs.push('ask_close_action', 'quit', 'tray', 'cancel', 'quit')

    assert window._ask_close_action() == 'quit'
    assert window._ask_close_action() == 'tray'
    assert window._ask_close_action() == 'cancel'
    first = dialogs.calls_of('ask_close_action')[0]
    assert first == {"parent": window, "running_count": 0, "can_tray": False}  # offscreen 无托盘

    window.tray = _FakeTray()
    window._ask_close_action()
    assert dialogs.last_call('ask_close_action')["can_tray"] is True


def test_tray_icon_compat_property(window):
    """兼容属性 tray_icon：指向控制器的托盘图标，托盘不可用（offscreen）时为 None"""
    assert window.tray is not None and window.tray.is_available() is False
    assert window.tray_icon is None
    window.tray = _FakeTray()
    assert window.tray_icon is window.tray.icon


def test_close_action_dialog_tray_button_only_when_tray_available(qapp):
    from PyQt5.QtWidgets import QWidget
    host = QWidget()
    host.resize(800, 600)

    with_tray = CloseActionDialog(2, can_tray=True, parent=host)
    assert with_tray.tray_button is not None
    with_tray.tray_button.click()
    assert with_tray.choice == 'tray'

    quit_dialog = CloseActionDialog(2, can_tray=True, parent=host)
    quit_dialog.quit_button.click()
    assert quit_dialog.choice == 'quit'

    without_tray = CloseActionDialog(1, can_tray=False, parent=host)
    assert without_tray.tray_button is None
    dispose_widget(host)


@pytest.mark.parametrize("can_tray", [True, False])
def test_close_action_dialog_default_is_never_destructive(qapp, can_tray):
    """跑一整夜的测试不能被顺手一个回车全停：主按钮（强调色、默认焦点）不是
    "停止监控并退出"，对话框弹出后直接回车也不会得到 'quit'"""
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QWidget
    from qfluentwidgets import PrimaryPushButton

    host = QWidget()
    host.resize(800, 600)
    host.show()
    dialog = CloseActionDialog(2, can_tray=can_tray, parent=host)
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(dialog.choice))
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.quit_button.text() == "停止监控并退出"
        assert not isinstance(dialog.quit_button, PrimaryPushButton)
        focused = dialog.focusWidget()
        assert focused is not None and focused is not dialog.quit_button
        if can_tray:
            assert focused is dialog.tray_button is dialog.yesButton
            assert dialog.yesButton.text() == "最小化到托盘"
        else:
            # 无托盘时隐藏主按钮，焦点落在"取消"
            assert dialog.yesButton.isHidden()
            assert focused is dialog.cancelButton

        QTest.keyClick(focused, Qt.Key_Return)
        # MaskDialogBase.done 带淡出动画，等动画结束后 accepted 才会发出
        QTest.qWait(400)
        assert dialog.choice != 'quit'
        assert 'quit' not in accepted
        if can_tray:
            assert accepted == ['tray']
    finally:
        dialog.close()
        dispose_widget(host)


# ========== 退出时经线程登记表收尾后台线程 ==========

class _SlowThread(QThread):
    """可取消的慢线程：取消后提前结束，否则睡满 duration 秒"""

    def __init__(self, duration):
        super().__init__()
        self.duration = duration
        self.started_evt = threading.Event()
        self.cancel_evt = threading.Event()
        self.log = []

    def run(self):
        self.started_evt.set()
        self.cancel_evt.wait(self.duration)

    def cancel(self):
        self.log.append(("cancel", self.isRunning()))
        self.cancel_evt.set()


def test_close_shuts_down_registered_threads(window, monkeypatch):
    """页面登记到 services.threads 的在途线程在真退出时都被收尾：有取消回调的
    先取消，closeEvent 返回时线程都已结束"""
    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    cancellable = _SlowThread(10.0)
    plain = _SlowThread(0.3)
    window.services.threads.register(cancellable, name="下载", cancel=cancellable.cancel,
                                     timeout_ms=3000)
    window.services.threads.register(plain, name="历史刷新", timeout_ms=3000)
    for thread in (cancellable, plain):
        thread.start()
        assert thread.started_evt.wait(5)

    window.closeEvent(_fake_event())

    assert cancellable.log == [("cancel", True)]
    assert cancellable.isFinished() and plain.isFinished()


def test_main_window_has_no_per_page_thread_shutdown(window):
    """收线程只走登记表：主窗口不再逐页调用 background_threads()"""
    assert not hasattr(window, '_shutdown_page_threads')
    assert not hasattr(window, '_thread_owner_pages')


def test_migration_failed_tip_holds_for_both_failure_cases(window):
    """migration_failed 既可能是"已从备份还原"，也可能是"还原与改名都失败、旧库
    原样保留"：提示不能断言"已还原旧数据"，只说两种情况都成立的事实"""
    notifier = window.services.notifier

    window._show_migration_failed_tip()

    notice = notifier.last("warning")
    assert notice.parent is window
    assert "还原" not in notice.content
    assert "旧数据已保留" in notice.content
    assert "新数据无法保存" in notice.content
    assert notice.duration == -1


@pytest.mark.parametrize("tip, title", [
    ("_show_data_reset_tip", "数据库已重置"),
    ("_show_backup_aborted_tip", "数据库升级已中止"),
])
def test_other_migration_tips_stay_until_closed(window, tip, title):
    getattr(window, tip)()

    notice = window.services.notifier.last()
    assert (notice.level, notice.title, notice.duration) == ("warning", title, -1)


def test_silent_update_notice_offers_view_button(window, monkeypatch):
    """静默检查发现新版本：主窗口弹非模态提示，「查看」切到设置页"关于与更新"并弹确认框"""
    shown = []
    monkeypatch.setattr(window.about_group, 'show_update_dialog_for', shown.append)
    info = {'version': '9.9.9'}

    window.about_group.update_available_silent.emit(info)

    notice = window.services.notifier.last("info")
    assert notice.title == "发现新版本 9.9.9" and notice.duration == 10000
    assert notice.event == EVENT_UPDATE_AVAILABLE
    assert notice.action.text == "查看"
    notice.action.callback()
    assert window.stackedWidget.currentWidget() is window.setting_page
    assert window.about_group is window.setting_page.about_group
    assert shown == [info]


def test_main_window_does_not_run_startup_maintenance(tmp_path, qapp, monkeypatch):
    """启动校正（孤儿任务、采样计数、过期清理）归组合根的 MaintenanceService
    （见 tests/test_bootstrap.py、tests/test_maintenance_service.py），主窗口构造时不再调用"""
    db = Database(str(tmp_path / "startup.db"))
    for name in ('reconcile_orphan_tasks', 'reconcile_sample_counts', 'cleanup_old_tasks'):
        monkeypatch.setattr(db, name, MagicMock(
            side_effect=AssertionError(f"主窗口不应调用 {name}")))
    manager = MonitorManager(db=db)
    services = make_services(preferences=FakePreferences(retention_days=7),
                             task_status=ManagerTaskStatus(manager))
    win = make_main_window(db, manager, services)
    win.about_group.check_update = lambda *args, **kwargs: None
    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    win.close()
    dispose_widget(win)


def test_main_window_requires_injected_dependencies(qapp):
    """主窗口不再自建数据库与管理器：三项依赖必填"""
    from ui.main_window import MainWindow
    with pytest.raises(TypeError):
        MainWindow()


def test_pages_share_the_window_services(window):
    """主窗口把同一份 services 分发给全部页面"""
    pages = (window.monitor_page, window.analysis_page,
             window.setting_page, window.about_group)
    assert all(page.services is window.services for page in pages)
    assert all(page.db is window.db for page in pages if page is not window.about_group)
    assert all(page.manager is window.monitor_manager for page in pages
               if page is not window.about_group)


# ---------- 导航、快速打点与全局快捷键接线 ----------

def _nav_texts(window):
    items = window.navigationInterface.panel.items
    return {key: item.widget.text() for key, item in items.items()}


def test_navigation_is_monitor_analysis_settings(window):
    texts = _nav_texts(window)
    assert sorted(texts.values()) == sorted(["监控", "分析", "设置"])
    assert window.stackedWidget.currentWidget() is window.monitor_page
    assert window.navigationInterface.panel.returnButton.isHidden()
    assert window.navigationInterface.panel.minimumExpandWidth == 1200
    # 只有三个一级页面（旧的历史/导出/关于页都已并入）
    assert {name for name in vars(window) if name.endswith('_page')} == {
        'monitor_page', 'analysis_page', 'setting_page'}


def test_analysis_page_is_the_new_page(window):
    from ui.pages.analysis_page import AnalysisPage
    assert isinstance(window.analysis_page, AnalysisPage)


def test_card_analysis_switches_to_analysis_page_and_selects(window, monkeypatch):
    selected = []
    monkeypatch.setattr(window.analysis_page, 'select_task', selected.append)
    window.monitor_page.history_requested.emit("t-42")
    assert window.stackedWidget.currentWidget() is window.analysis_page
    assert selected == ["t-42"]


def test_navigation_expands_when_crossing_1200(window):
    panel = window.navigationInterface.panel
    window.resize(1000, 700)
    panel.collapse()
    window._sync_navigation_expand(1000, 1300)
    assert not panel.isCollapsed()


def test_quick_marker_feedback_is_routed_as_a_mergeable_notice(window):
    """渠道（窗口在前台是提示条，后台是托盘气泡）由 Notifier 选；这里只管级别、事件与合并键"""
    feedback = window.monitor_page.presenter.quick_marker_feedback
    feedback.emit('ok', "已打点：基线 16:41:03", "x")
    feedback.emit('none', "没有可打点的任务", "y")
    ok, none = window.services.notifier.messages()
    assert (ok.level, ok.title, ok.event) == ("success", "已打点：基线 16:41:03", EVENT_QUICK_MARKER)
    assert (none.level, none.title) == ("warning", "没有可打点的任务")
    assert ok.key == none.key == EVENT_QUICK_MARKER


def test_tray_marker_request_marks_through_presenter(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.monitor_page.presenter, 'quick_marker', calls.append)
    window.tray.marker_requested.emit("")
    window.tray.marker_requested.emit("t1")
    assert calls == [None, "t1"]


class _FakeHotkeyBackend:
    def __init__(self):
        self.registered = {}

    def register(self, hotkey_id, modifiers, vk):
        self.registered[hotkey_id] = (modifiers, vk)
        return True

    def unregister(self, hotkey_id):
        self.registered.pop(hotkey_id, None)


def test_hotkey_wiring_and_unbind_on_quit(tmp_path, qapp, monkeypatch):
    """全局快捷键：触发即快速打点；改键同步监控页引导；真退出时注销"""
    from ui.hotkey import MarkerHotkey
    backend = _FakeHotkeyBackend()
    hotkey = MarkerHotkey(backend)
    hotkey.bind("Ctrl+Alt+M")
    db = Database(str(tmp_path / "hotkey_window.db"))
    manager = MonitorManager(db=db)
    services = make_services(
        notifier=RecordingNotifier(), dialogs=ScriptedDialogs(),
        preferences=FakePreferences(), task_status=ManagerTaskStatus(manager))
    from ui.main_window import MainWindow
    win = MainWindow(db, manager, services, hotkey=hotkey)
    win.about_group.check_update = lambda *args, **kwargs: None
    try:
        calls = []
        monkeypatch.setattr(win.monitor_page.presenter, 'quick_marker', calls.append)
        hotkey.handle_hotkey(hotkey._hotkey_id)
        assert calls == [None]
        assert win.setting_page.hotkey_status().startswith("已启用")

        guide = win.monitor_page.board.empty_state
        assert any("Ctrl+Alt+M" in label.text() for label in guide.step_hints)
        hotkey.bind("")
        assert not any("Ctrl+Alt+M" in label.text() for label in guide.step_hints)
        hotkey.bind("Ctrl+Alt+M")

        monkeypatch.setattr(QApplication, 'quit', MagicMock())
        win.closeEvent(_fake_event())
        assert backend.registered == {}
    finally:
        stop_and_join(manager)
        dispose_widget(win)


# ---------- 界面看门狗 ----------

class _RecordingWatchdog:
    def __init__(self, order):
        self.order = order

    def stop(self):
        self.order.append('watchdog.stop')


def test_real_quit_stops_watchdog_before_shutdown(window, monkeypatch):
    """真退出收尾会阻塞界面线程数秒：先停看门狗，免得把正常收尾当成卡死转储。"""
    order = []
    window.attach_watchdog(_RecordingWatchdog(order))
    monkeypatch.setattr(window, '_run_shutdown', lambda: order.append('shutdown'))
    monkeypatch.setattr(QApplication, 'quit', MagicMock())

    window.closeEvent(_fake_event())

    assert order == ['watchdog.stop', 'shutdown']


def test_hide_to_tray_keeps_watchdog_running(window, monkeypatch):
    _enable_close_to_tray(window)
    window.tray = _FakeTray()
    order = []
    window.attach_watchdog(_RecordingWatchdog(order))

    window.closeEvent(_fake_event())

    assert order == []


# ---------- 磁盘已满时留在内存的数据：托盘退出与更新安装前的确认 ----------

HELD_NOTICE = "有 1234 条因磁盘已满未写入的数据，退出后将丢失"


def _hold_data(window, monkeypatch, count=1234):
    monkeypatch.setattr(window.monitor_manager, 'held_point_count', lambda: count)


def test_tray_quit_with_held_data_asks_and_cancel_keeps_running(window, monkeypatch):
    _hold_data(window, monkeypatch)
    window.services.dialogs.always('confirm', False)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    window.tray.quit_requested.emit()

    asked = window.services.dialogs.calls_of('confirm')
    assert len(asked) == 1
    assert HELD_NOTICE in asked[0]["content"]
    assert asked[0]["confirm_text"] == "仍然退出"
    assert window._really_quit is False
    quit_mock.assert_not_called()
    assert window._shutdown_started is False


def test_tray_quit_with_held_data_confirmed_quits(window, monkeypatch):
    _hold_data(window, monkeypatch)
    window.services.dialogs.always('confirm', True)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    window.tray.quit_requested.emit()

    assert window._really_quit is True
    quit_mock.assert_called_once()


def test_tray_quit_without_held_data_does_not_ask(window, monkeypatch):
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    window.tray.quit_requested.emit()          # confirm 未预设：被问到会直接断言失败
    assert window.services.dialogs.calls_of('confirm') == []
    quit_mock.assert_called_once()


def test_quit_for_install_called_directly_asks_too(window, monkeypatch):
    _hold_data(window, monkeypatch)
    window.services.dialogs.always('confirm', False)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    window.quit_for_install()

    assert len(window.services.dialogs.calls_of('confirm')) == 1
    assert window._really_quit is False
    quit_mock.assert_not_called()


def test_install_flow_asks_before_launching_the_installer(window, monkeypatch):
    """"安装更新并退出"：确认发生在启动安装程序之前，用户取消则安装程序根本不启动；
    确认过的不会在 quit_for_install 里重复问"""
    import ui.pages.about_update as about_module
    _hold_data(window, monkeypatch)
    monkeypatch.setattr(about_module.InstallConfirmDialog, 'exec', lambda self: True)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    group = window.about_group
    group._pending_info = {'version': '9.9.9'}
    dialogs = window.services.dialogs

    dialogs.push('confirm', False)
    group._on_download_finished(r"C:\temp\setup.exe")
    assert dialogs.methods_called() == ['confirm']          # 没有 open_file
    assert "已取消安装" in group.status_label.text()
    quit_mock.assert_not_called()

    dialogs.push('confirm', True)
    group._on_download_finished(r"C:\temp\setup.exe")
    assert dialogs.methods_called() == ['confirm', 'confirm', 'open_file']
    assert window._really_quit is True                      # install_requested → quit_for_install
    assert len(dialogs.calls_of('confirm')) == 2            # 退出路径里没有第三次确认
    quit_mock.assert_called_once()


def test_session_end_with_held_data_only_logs_a_warning(window, monkeypatch, caplog):
    import logging
    _hold_data(window, monkeypatch)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    with caplog.at_level(logging.WARNING, logger='ui.main_window'):
        window._on_commit_data_request()
    assert window.services.dialogs.calls_of('confirm') == []          # 不弹窗
    assert any(r.levelno == logging.WARNING and HELD_NOTICE in r.getMessage()
               for r in caplog.records)
    assert window._really_quit is True
    quit_mock.assert_called_once()


def test_ipc_quit_with_held_data_logs_and_does_not_ask(window, monkeypatch, caplog):
    """命令行 quit 没有人可以确认：不弹窗，只记 WARNING"""
    import logging
    _hold_data(window, monkeypatch)
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    with caplog.at_level(logging.WARNING, logger='ui.main_window'):
        window.request_quit()
    assert window.services.dialogs.calls_of('confirm') == []
    assert any(HELD_NOTICE in r.getMessage() for r in caplog.records)
    quit_mock.assert_called_once()
