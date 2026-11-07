"""
GUI 端到端冒烟测试专用 fixture（tests/e2e/ 独立目录，零生产代码改动）
提供隔离数据库注入；qapp/迁移 guard/MonitorManager 单例重置从根 conftest 继承。
"""
import pytest

from data.database import Database


@pytest.fixture
def e2e_db(tmp_path):
    """e2e 冒烟测试专用隔离数据库（不碰项目真实 data\\monitor.db）"""
    return Database(str(tmp_path / "e2e_smoke.db"))


@pytest.fixture(autouse=True)
def _restore_event_loop_after_real_quit():
    """冒烟用例会走真实退出路径（MainWindow 收尾后调用 QApplication.quit()），而测试进程
    从不运行主事件循环，Qt 会一直记着"已退出"：之后同进程里所有局部 QEventLoop.exec_()
    都立即返回，qtbot.waitSignal 等不到后台线程的信号（如更新下载用例）。每个用例收尾后
    清除这一状态，不让它漏给后面的用例。"""
    from tests.helpers import reset_qt_quit_state

    yield
    reset_qt_quit_state()
