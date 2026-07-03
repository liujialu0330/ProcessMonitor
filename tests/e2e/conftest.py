"""
GUI 端到端冒烟测试专用 fixture（tests/e2e/ 独立目录，零生产代码改动）
提供隔离数据库注入 + MonitorManager 单例重置；qapp/迁移 guard 从根 conftest 继承。
"""
import pytest

from core.monitor_manager import MonitorManager
from data.database import Database


@pytest.fixture
def e2e_db(tmp_path):
    """e2e 冒烟测试专用隔离数据库（不碰项目真实 data\\monitor.db）"""
    return Database(str(tmp_path / "e2e_smoke.db"))


@pytest.fixture(autouse=True)
def _reset_monitor_manager_singleton():
    """
    重置 MonitorManager 单例。

    MonitorManager 是进程级单例（_instance 类属性，仅首次构造时传入的 db 生效）。
    e2e 用例通过 MainWindow(db=e2e_db) 间接构造 MonitorManager，若不在用例间重置，
    后续用例会复用上一个用例残留的旧实例（及其绑定的旧 db、旧任务字典），造成互相
    污染。_initialized 是实例级属性，随旧实例一起被丢弃，无需单独重置。

    teardown 顺序固定：先对旧实例调用 stop_all_tasks() 收尾残留 QThread（避免残留
    线程跨用例干扰或收尾中的数据未落库就被强行丢弃），再置 _instance = None。
    setup 侧也重置一次作防御：若未来有其他测试在 e2e 之前构造了单例（当前没有），
    保证本用例仍从干净单例开始。
    """
    MonitorManager._instance = None
    yield
    old_instance = MonitorManager._instance
    if old_instance is not None:
        old_instance.stop_all_tasks()
    MonitorManager._instance = None
