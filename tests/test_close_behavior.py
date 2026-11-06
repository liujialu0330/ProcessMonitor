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
from tests.helpers import stop_and_join
from ui.components.dialogs import CloseActionDialog
from ui.services import ManagerTaskStatus
from utils.metrics import MetricType


@pytest.fixture
def window(tmp_path, qapp):
    """构造真实 MainWindow（tmp 库），并做好测试隔离：覆写 about_page.
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
    win.about_page.check_update = lambda *args, **kwargs: None
    win.show()
    yield win
    stop_and_join(win.monitor_manager)


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
    """真退出的固定顺序：stop_all_tasks → join_all(3000) → 登记表线程收尾 → 关库；
    主窗口不再自己遍历任务线程"""
    calls = []
    monkeypatch.setattr(window.monitor_manager, 'stop_all_tasks',
                        lambda: calls.append('stop_all'))
    monkeypatch.setattr(window.monitor_manager, 'join_all',
                        lambda timeout_ms: calls.append(('join_all', timeout_ms)) or True)
    monkeypatch.setattr(window.monitor_manager, 'get_all_tasks',
                        lambda: pytest.fail("主窗口不应直接取任务线程对象"))
    monkeypatch.setattr(window.services.threads, 'shutdown_all',
                        lambda on_progress=None: calls.append('threads') or True)
    monkeypatch.setattr(window.db, 'close', lambda: calls.append('db_close'))
    monkeypatch.setattr(QApplication, 'quit', lambda: calls.append('quit'))

    window.closeEvent(_fake_event())

    assert calls == ['stop_all', ('join_all', 3000), 'threads', 'db_close', 'quit']


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
    assert window.tray.messages, "首次隐藏到托盘应弹一次提示气泡"

    # 首次提示只弹一次：再关闭一次（先还原可见性，模拟用户从托盘还原后再关）
    # 不应重复追加气泡
    window.show()
    window.closeEvent(_fake_event())
    assert len(window.tray.messages) == 1


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


def test_about_page_install_request_goes_through_quit_for_install(window, monkeypatch):
    """关于页只发 install_requested，主窗口连接到 quit_for_install（不靠 hasattr 反向调用）"""
    _enable_close_to_tray(window)
    window.tray = _FakeTray()
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    window.about_page.install_requested.emit()

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
    host.close()


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
        host.close()


def test_abnormal_end_bubble_only_when_window_not_in_front(window):
    """异常结束提醒：无托盘时短路；有托盘且窗口隐藏时弹气泡；窗口在前台不重复提醒"""
    window._on_task_ended_abnormally("标题", "内容")   # 托盘不可用，不应抛错

    window.tray = _FakeTray()
    window.hide()
    window._on_task_ended_abnormally("任务已结束", "demo.exe 进程已终止")
    assert window.tray.messages == [("任务已结束", "demo.exe 进程已终止")]

    window.tray.messages.clear()
    window.show()
    window.isActiveWindow = lambda: True
    window._on_task_ended_abnormally("任务已结束", "x")
    assert window.tray.messages == []


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
    """静默检查发现新版本：主窗口弹非模态提示，「查看」切到关于页并弹确认框"""
    shown = []
    monkeypatch.setattr(window.about_page, 'show_update_dialog_for', shown.append)
    info = {'version': '9.9.9'}

    window.about_page.update_available_silent.emit(info)

    notice = window.services.notifier.last("info")
    assert notice.title == "发现新版本 9.9.9" and notice.duration == 10000
    notice.bar.widgets[0].click()
    assert window.stackedWidget.currentWidget() is window.about_page
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
    win.about_page.check_update = lambda *args, **kwargs: None
    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    win.close()


def test_main_window_requires_injected_dependencies(qapp):
    """主窗口不再自建数据库与管理器：三项依赖必填"""
    from ui.main_window import MainWindow
    with pytest.raises(TypeError):
        MainWindow()


def test_pages_share_the_window_services(window):
    """主窗口把同一份 services 分发给全部页面"""
    pages = (window.monitor_page, window.history_page, window.export_page,
             window.setting_page, window.about_page)
    assert all(page.services is window.services for page in pages)
    assert all(page.db is window.db for page in pages if page is not window.about_page)
    assert all(page.manager is window.monitor_manager for page in pages
               if page is not window.about_page)
