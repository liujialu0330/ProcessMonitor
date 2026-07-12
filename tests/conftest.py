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
import app_config  # noqa: E402
from qfluentwidgets import qconfig, Theme  # noqa: E402


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


@pytest.fixture(autouse=True)
def _reset_app_config():
    """
    重置 app_config 模块级单例 cfg 的状态（v1.3.0 批1，评审修订 M2）。

    cfg 的三个自定义配置项（default_interval/retention_days/close_to_tray）以及
    继承自 QConfig 基类的 themeMode，都是类级 ConfigItem 对象：进程内所有
    AppConfig 实例（含用例中临时构造用于持久化 roundtrip 测试的实例）都引用
    同一批对象；qconfig.load() 还会把 qfluentwidgets 模块级单例 qconfig 的
    _cfg 属性重新指向被加载的 cfg 实例、并把 file 指向被加载的（可能是 tmp_path
    下的）配置文件路径。这些都是跨用例持久存在的全局可变状态，若不在每个
    用例前后重置，任一用例里的 load/set 都会泄漏给后续用例（包括与 app_config
    完全无关的用例，因为 themeMode 与 qfluentwidgets 内部组件共用同一对象）——
    这是"既有全量用例保持全绿"的前置条件。

    直接操作 ConfigItem.value（而非调用 qconfig.set）：qconfig.set 对 themeMode
    有特殊分支（联动更新已解析的主题值、emit themeChanged、save() 落盘），直接
    赋值可以拿到同样的"回落默认值"效果，同时避免 teardown 阶段触发额外的磁盘
    写入与主题联动级联。
    """
    def _reset():
        item_cfg = app_config.cfg
        item_cfg.default_interval.value = item_cfg.default_interval.defaultValue
        item_cfg.retention_days.value = item_cfg.retention_days.defaultValue
        item_cfg.close_to_tray.value = item_cfg.close_to_tray.defaultValue
        # 主题复位为本应用语义上的默认值 AUTO（跟随系统），而非 qfluentwidgets
        # 库自带的类默认值 Theme.LIGHT——与 load_app_config() 首启行为保持一致
        item_cfg.themeMode.value = Theme.AUTO
        item_cfg._theme = Theme.AUTO
        qconfig._cfg = qconfig
        qconfig._theme = Theme.AUTO

    _reset()
    yield
    _reset()
