"""
主窗口关闭行为验证（托盘驻留与退出路径——本项目风险最高的改动之一，覆盖退出
路径的几个不变式）

覆盖：
① 默认配置（close_to_tray=False）关闭仍走既有"清理 -> QApplication.quit()"路径；
② 托盘"退出"（_really_quit）与 About 页"立即安装"（quit_for_install）都绕开
   隐藏分支，走同一条真退出路径；
③ 隐藏到托盘不停止任务、不做任何清理；
④ 清理步骤中途抛异常时 QApplication.quit() 仍必须执行（try/finally）。

构造真实 MainWindow（tmp 库注入，模式对齐 tests/e2e/test_gui_smoke.py）而非
mock 整个窗口：closeEvent 的清理路径直接引用 self.monitor_manager/
self.about_page/self.export_page/self.setting_page 等真实属性，只有真实对象
才能验证到实际调用链路。MonitorManager 单例由根 conftest 的 autouse fixture 重置。
"""
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QApplication

from app_config import cfg
from data.database import Database
from ui.components.dialogs import CloseActionDialog
from ui.main_window import MainWindow
from utils.metrics import MetricType


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    """构造真实 MainWindow（tmp 库），并做好测试隔离：覆写 about_page.
    check_update 为 no-op，防止 3 秒后 QTimer 触发真实联网检查更新（对齐
    tests/e2e/test_gui_smoke.py 的同一手法）。用例结束后兜底停止残留任务，
    避免遗留 QThread。
    """
    db = Database(str(tmp_path / "close_behavior.db"))
    win = MainWindow(db=db)
    win.about_page.check_update = lambda *args, **kwargs: None
    # 关闭确认对话框是模态的：默认让意外弹出直接失败而不是阻塞测试，
    # 需要验证该分支的用例自行 monkeypatch 为具体返回值
    monkeypatch.setattr(
        win, '_ask_close_action',
        lambda: pytest.fail("无运行中任务时不应弹出关闭确认"))
    win.show()
    yield win
    win.monitor_manager.stop_all_tasks()


class _FakeTrayIcon:
    """closeEvent 隐藏分支所需的最小伪托盘对象：只需 showMessage 可调用即可，
    不依赖真实系统托盘（offscreen/CI 下 QSystemTrayIcon.isSystemTrayAvailable()
    恒为 False，MainWindow._init_tray() 不会真正创建托盘图标，故用例里直接
    赋值伪对象来模拟"托盘可用"的场景）"""

    def __init__(self):
        self.messages = []

    def showMessage(self, title, msg, *args, **kwargs):
        self.messages.append((title, msg))


def _fake_event():
    """伪造一个记录 accept/ignore 调用的 QCloseEvent 替身：closeEvent 方法体
    只调用了 event.accept()/event.ignore() 两个方法，MagicMock 足以承接并
    留痕这两个调用，不需要构造真实 Qt 事件对象"""
    return MagicMock()


def test_close_quits_when_tray_disabled(window, monkeypatch):
    """不变式①：默认配置（close_to_tray=False）关闭仍走既有清理 -> quit 路径。
    cfg.close_to_tray 由顶层 conftest.py 的 _reset_app_config 自动夹具复位为
    默认 False，此处无需再显式设置。"""
    assert cfg.get(cfg.close_to_tray) is False

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


def test_close_hides_when_tray_enabled(window, monkeypatch):
    """不变式③：托盘启用（close_to_tray=True 且 tray_icon 可用）时关闭改为
    隐藏窗口，不停止任务、不做任何清理，event 被 ignore 而非 accept"""
    cfg.close_to_tray.value = True
    window.tray_icon = _FakeTrayIcon()

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
    assert window.tray_icon.messages, "首次隐藏到托盘应弹一次提示气泡"

    # 首次提示只弹一次：再关闭一次（先还原可见性，模拟用户从托盘还原后再关）
    # 不应重复追加气泡
    window.show()
    window.closeEvent(_fake_event())
    assert len(window.tray_icon.messages) == 1


def test_really_quit_bypasses_tray(window, monkeypatch):
    """不变式②（前半）：_really_quit=True 时即使 close_to_tray=True 也绕开
    隐藏分支，走完整清理 -> quit 的真退出路径"""
    cfg.close_to_tray.value = True
    window.tray_icon = _FakeTrayIcon()
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
    cfg.close_to_tray.value = True
    window.tray_icon = _FakeTrayIcon()
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    assert window._really_quit is False

    window.quit_for_install()

    assert window._really_quit is True
    quit_mock.assert_called_once()
    assert window.isHidden() is True  # 真退出路径下 close() 使窗口被隐藏/关闭


def test_cleanup_exception_still_quits(window, monkeypatch):
    """不变式④：清理步骤中途抛异常，QApplication.quit() 仍
    必须在 finally 中被调用——否则 setQuitOnLastWindowClosed(False) 拆掉了
    "closeEvent 异常也能退出"的隐性保险丝后，异常会导致事件循环永不退出且
    窗口已被清理/隐藏，用户无法通过任何界面操作恢复"""
    quit_mock = MagicMock()
    # 用局部 context 打补丁：window fixture 的 teardown 还要真实调用 stop_all_tasks
    with monkeypatch.context() as m:
        m.setattr(
            window.monitor_manager, 'stop_all_tasks',
            MagicMock(side_effect=RuntimeError("模拟清理异常")))
        m.setattr(QApplication, 'quit', quit_mock)

        event = _fake_event()
        with pytest.raises(RuntimeError, match="模拟清理异常"):
            window.closeEvent(event)

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


def _patch_ask(window, monkeypatch, answer):
    asked = []
    monkeypatch.setattr(
        window, '_ask_close_action', lambda: asked.append(1) or answer)
    return asked


def test_close_with_running_task_cancel_keeps_everything(window, monkeypatch):
    """选"取消"：忽略关闭事件，任务继续跑，不清理、不退出"""
    _start_running_task(window)
    asked = _patch_ask(window, monkeypatch, 'cancel')
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    assert asked == [1]
    event.ignore.assert_called_once()
    event.accept.assert_not_called()
    quit_mock.assert_not_called()
    assert window.monitor_manager.has_running_tasks()
    assert window.isHidden() is False


def test_close_with_running_task_quit_stops_and_quits(window, monkeypatch):
    """选"停止监控并退出"：走完整清理 -> quit 路径，任务被停止"""
    _start_running_task(window)
    _patch_ask(window, monkeypatch, 'quit')
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    quit_mock.assert_called_once()
    event.accept.assert_called_once()
    assert window.monitor_manager.running_task_count() == 0


def test_close_with_running_task_tray_hides_without_stopping(window, monkeypatch):
    """选"最小化到托盘"（托盘可用）：隐藏窗口，不停止任务、不退出"""
    window.tray_icon = _FakeTrayIcon()
    _start_running_task(window)
    _patch_ask(window, monkeypatch, 'tray')
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
    monkeypatch.setattr(
        window, '_ask_close_action', lambda: pytest.fail("真退出不应再询问"))
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    quit_mock.assert_called_once()
    event.accept.assert_called_once()


def test_close_without_running_task_does_not_ask(window, monkeypatch):
    """没有运行中任务时不弹确认（窗口 fixture 默认的 _ask_close_action 一旦被调用即失败）"""
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    window.closeEvent(event)

    quit_mock.assert_called_once()
    event.accept.assert_called_once()


def test_ask_close_action_maps_dialog_result(window, monkeypatch):
    """_ask_close_action 把对话框结果映射为 quit / tray / cancel"""
    class _Dialog:
        outcome = (True, 'quit')
        created = []

        def __init__(self, running_count, can_tray, parent=None):
            self.can_tray = can_tray
            self.running_count = running_count
            _Dialog.created.append(self)

        def exec(self):
            return self.outcome[0]

        @property
        def choice(self):
            return self.outcome[1]

    monkeypatch.undo()
    monkeypatch.setattr('ui.main_window.CloseActionDialog', _Dialog)

    _Dialog.outcome = (True, 'quit')
    assert window._ask_close_action() == 'quit'
    _Dialog.outcome = (True, 'tray')
    assert window._ask_close_action() == 'tray'
    _Dialog.outcome = (False, 'quit')
    assert window._ask_close_action() == 'cancel'
    assert _Dialog.created[0].can_tray is False   # offscreen 无托盘

    window.tray_icon = _FakeTrayIcon()
    window._ask_close_action()
    assert _Dialog.created[-1].can_tray is True


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
    window._on_task_ended_abnormally("标题", "内容")   # tray_icon 为 None，不应抛错

    window.tray_icon = _FakeTrayIcon()
    window.hide()
    window._on_task_ended_abnormally("任务已结束", "demo.exe 进程已终止")
    assert window.tray_icon.messages == [("任务已结束", "demo.exe 进程已终止")]

    window.tray_icon.messages.clear()
    window.show()
    window.isActiveWindow = lambda: True
    window._on_task_ended_abnormally("任务已结束", "x")
    assert window.tray_icon.messages == []


# ========== 退出时按页面登记收尾后台线程 ==========

class _FakeThread:
    """记录取消与等待调用的假线程：isRunning 在被 wait 之前恒为 True"""

    def __init__(self, log, name):
        self.log = log
        self.name = name
        self.waited_ms = None

    def isRunning(self):
        return self.waited_ms is None

    def cancel(self):
        self.log.append(("cancel", self.name))

    def wait(self, timeout_ms):
        self.log.append(("wait", self.name))
        self.waited_ms = timeout_ms
        return True


def test_close_shuts_down_every_page_background_thread(window, monkeypatch):
    """各页面现有的后台线程在真退出时都被收尾，取消语义与超时保持原值：
    下载 2000 可取消、检查更新 1000、导出 2000 可取消、清理 5000 不取消、
    历史自动刷新 3000"""
    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    log = []
    fakes = {name: _FakeThread(log, name) for name in
             ("download", "check", "export", "cleanup", "refresh")}
    window.about_page._downloader = fakes["download"]
    window.about_page._checker = fakes["check"]
    window.export_page._export_worker = fakes["export"]
    window.setting_page._cleanup_worker = fakes["cleanup"]
    window.history_page._refresh_worker = fakes["refresh"]
    try:
        window.closeEvent(_fake_event())
    finally:
        window.about_page._downloader = None
        window.about_page._checker = None
        window.export_page._export_worker = None
        window.setting_page._cleanup_worker = None
        window.history_page._refresh_worker = None

    assert {name: fake.waited_ms for name, fake in fakes.items()} == {
        "download": 2000, "check": 1000, "export": 2000,
        "cleanup": 5000, "refresh": 3000}
    cancelled = [name for action, name in log if action == "cancel"]
    assert cancelled == ["download", "export"]
    # 取消必须先于等待
    assert log.index(("cancel", "download")) < log.index(("wait", "download"))
    assert log.index(("cancel", "export")) < log.index(("wait", "export"))


def test_close_uses_public_background_threads_not_private_names(window, monkeypatch):
    """主窗口只认页面的 background_threads()：页面内部线程属性改名（这里私有属性
    全为 None，线程只经公开方法给出）也不会漏收"""
    from ui.thread_shutdown import BackgroundThread

    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    log = []
    owners = window._thread_owner_pages()
    assert set(owners) == {window.about_page, window.export_page,
                           window.setting_page, window.history_page}
    fakes = []
    for index, page in enumerate(owners):
        fake = _FakeThread(log, f"renamed_{index}")
        fakes.append(fake)
        monkeypatch.setattr(
            page, 'background_threads',
            lambda fake=fake: [BackgroundThread(fake.name, fake,
                                                cancel_fn=fake.cancel,
                                                timeout_ms=1234)])

    window.closeEvent(_fake_event())

    assert [fake.waited_ms for fake in fakes] == [1234] * len(owners)
    assert [name for action, name in log if action == "cancel"] == \
        [fake.name for fake in fakes]


def test_close_continues_when_one_page_fails_to_list_threads(window, monkeypatch):
    """某一页取线程列表抛异常时，其余页面的线程照样收尾，quit 照常执行"""
    from ui.thread_shutdown import BackgroundThread

    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)
    log = []
    fake = _FakeThread(log, "history")
    monkeypatch.setattr(
        window.about_page, 'background_threads',
        MagicMock(side_effect=RuntimeError("模拟页面异常")))
    monkeypatch.setattr(
        window.history_page, 'background_threads',
        lambda: [BackgroundThread("history", fake, timeout_ms=3000)])

    event = _fake_event()
    window.closeEvent(event)

    assert fake.waited_ms == 3000
    quit_mock.assert_called_once()
    event.accept.assert_called_once()


def test_page_background_threads_are_none_when_idle(window):
    """空闲时各页面登记项的线程为 None，收尾时直接跳过"""
    for page in window._thread_owner_pages():
        entries = page.background_threads()
        assert entries
        assert all(entry.thread is None for entry in entries)
        assert all(entry.cancel_fn is None for entry in entries)


def test_migration_failed_tip_holds_for_both_failure_cases(window, monkeypatch):
    """migration_failed 既可能是"已从备份还原"，也可能是"还原与改名都失败、旧库
    原样保留"：提示不能断言"已还原旧数据"，只说两种情况都成立的事实"""
    import ui.main_window as main_window_module
    shown = []
    monkeypatch.setattr(
        main_window_module.InfoBar, 'warning',
        lambda **kwargs: shown.append(kwargs))

    window._show_migration_failed_tip()

    content = shown[-1]["content"]
    assert "还原" not in content
    assert "旧数据已保留" in content
    assert "新数据无法保存" in content
    assert shown[-1]["duration"] == -1
