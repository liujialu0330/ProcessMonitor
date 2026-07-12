"""
主窗口关闭行为验证（v1.3.0 批4，D 托盘驻留，退出路径重构——本项目风险最高的
一批改动，方案 §4.1 五个不变式对应的最小回归场景）

覆盖：
① 默认配置（close_to_tray=False）关闭仍走既有"清理 -> QApplication.quit()"路径；
② 托盘"退出"（_really_quit）与 About 页"立即安装"（quit_for_install）都绕开
   隐藏分支，走同一条真退出路径；
③ 隐藏到托盘不停止任务、不做任何清理；
④ 清理步骤中途抛异常时 QApplication.quit() 仍必须执行（try/finally，评审
   修订 B4）。

构造真实 MainWindow（tmp 库注入，模式对齐 tests/e2e/test_gui_smoke.py）而非
mock 整个窗口：closeEvent 的清理路径直接引用 self.monitor_manager/
self.about_page/self.export_page/self.setting_page 等真实属性，只有真实对象
才能验证到实际调用链路。MonitorManager 是进程级单例，本文件独立管理其重置
（不在 tests/e2e/conftest.py 的作用范围内，那份 conftest 只对 tests/e2e/
目录下的用例生效）。
"""
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QApplication

from app_config import cfg
from core.monitor_manager import MonitorManager
from data.database import Database
from ui.main_window import MainWindow


@pytest.fixture(autouse=True)
def _reset_monitor_manager_singleton():
    """重置 MonitorManager 单例，避免用例间残留任务/db 互相污染（对齐
    tests/e2e/conftest.py 的同名夹具）"""
    MonitorManager._instance = None
    yield
    old_instance = MonitorManager._instance
    if old_instance is not None:
        old_instance.stop_all_tasks()
    MonitorManager._instance = None


@pytest.fixture
def window(tmp_path, qapp):
    """构造真实 MainWindow（tmp 库），并做好测试隔离：覆写 about_page.
    check_update 为 no-op，防止 3 秒后 QTimer 触发真实联网检查更新（对齐
    tests/e2e/test_gui_smoke.py 的同一手法）。用例结束后兜底停止残留任务，
    避免遗留 QThread。
    """
    db = Database(str(tmp_path / "close_behavior.db"))
    win = MainWindow(db=db)
    win.about_page.check_update = lambda *args, **kwargs: None
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
    """不变式②（后半，评审修订 B3）：quit_for_install() 等价于置
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
    """不变式④（评审修订 B4）：清理步骤中途抛异常，QApplication.quit() 仍
    必须在 finally 中被调用——否则 setQuitOnLastWindowClosed(False) 拆掉了
    "closeEvent 异常也能退出"的隐性保险丝后，异常会导致事件循环永不退出且
    窗口已被清理/隐藏，用户无法通过任何界面操作恢复"""
    monkeypatch.setattr(
        window.monitor_manager, 'stop_all_tasks',
        MagicMock(side_effect=RuntimeError("模拟清理异常")))
    quit_mock = MagicMock()
    monkeypatch.setattr(QApplication, 'quit', quit_mock)

    event = _fake_event()
    with pytest.raises(RuntimeError, match="模拟清理异常"):
        window.closeEvent(event)

    quit_mock.assert_called_once()
