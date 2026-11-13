"""
真实退出路径不得污染同进程里后续用例的事件循环。

QApplication.quit() 在无主事件循环时调用后，Qt 5 的所有新建 QEventLoop.exec_() 都立即返回，
qtbot.waitSignal 等不到后台线程的信号。e2e 冒烟会走真实退出路径，tests/e2e/conftest.py
的 autouse fixture 负责在每个用例后清除；这里两个用例按文件顺序执行：先制造污染，再检查
后一个用例看到的是干净状态。去掉那个 fixture，第二个用例必失败。
"""
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication

from tests.helpers import qt_quit_pending, reset_qt_quit_state


class _Emitter(QThread):
    ping = pyqtSignal(int)

    def run(self):
        self.ping.emit(1)


def test_a_real_quit_leaves_pending_state_during_the_case(qapp):
    """制造污染：与 MainWindow 真退出同一调用；用例内部确实处于"已退出"状态。"""
    reset_qt_quit_state()
    QApplication.quit()
    assert qt_quit_pending() is True


def test_b_next_case_still_receives_cross_thread_signals(qapp, qtbot):
    """上一个用例的退出状态不能漏到这里：局部事件循环正常工作，排队信号能送达。"""
    assert qt_quit_pending() is False
    worker = _Emitter()
    with qtbot.waitSignal(worker.ping, timeout=3000):
        worker.start()
    worker.wait()
