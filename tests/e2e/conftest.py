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
