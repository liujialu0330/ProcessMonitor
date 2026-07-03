"""
pytest 全局 fixture
统一 sys.path（把项目根加进 sys.path）、tmp_path 数据库注入、QApplication 会话级实例
"""
import os
import sys

import pytest

# 把项目根目录加入 sys.path，便于以 `from data.database import Database` 等方式导入
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 无窗口环境跑 PyQt5：涉及 QThread/QObject 的用例需要一个 QApplication 实例存在，
# offscreen 平台不弹出任何窗口；若外部已设置 QT_QPA_PLATFORM 则尊重外部设置
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from data.database import Database  # noqa: E402


@pytest.fixture(scope='session')
def qapp():
    """会话级 QApplication 实例：QThread 子类（MonitorTask/UpdateDownloader等）的信号槽
    机制依赖它存在，多个测试模块共用同一实例，避免重复创建报错"""
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def db_path(tmp_path):
    """临时数据库文件路径（不落项目 data 目录）"""
    return str(tmp_path / "test_monitor.db")


@pytest.fixture
def db(db_path):
    """注入临时库路径的 Database 实例（新库，直接建 v1 结构）"""
    return Database(db_path)


@pytest.fixture(autouse=True)
def _reset_migration_guard():
    """
    重置 Database 类级迁移 guard（_migration_attempted/_migration_state）。

    该 guard 设计为进程级（防止 backup_aborted 场景下每个新 Database() 实例重复
    触发备份），但测试进程内会连续实例化多个指向不同 tmp db_path 的 Database，
    若不在用例间重置会互相污染（前一个用例触发迁移后置位，后续用例直接被跳过）。
    """
    Database._migration_attempted = False
    Database._migration_state = {
        'migration_failed': False,
        'data_reset': False,
        'backup_aborted': False,
    }
    yield
    Database._migration_attempted = False
    Database._migration_state = {
        'migration_failed': False,
        'data_reset': False,
        'backup_aborted': False,
    }
