"""
pytest 全局 fixture
统一 sys.path（把项目根加进 sys.path）、数据目录隔离、tmp_path 数据库注入、
QApplication 会话级实例、MonitorManager 单例重置
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

import config  # noqa: E402
from core.monitor_manager import MonitorManager  # noqa: E402
from data.database import Database  # noqa: E402
from data.db.migrations.runner import reset_guard_for_tests  # noqa: E402
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


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path_factory, monkeypatch):
    """
    把数据目录隔离到临时目录，任何用例都不得触碰开发机的 data 目录。

    各模块访问路径的方式：Database 与 update_checker 在调用时才读取
    config.DB_PATH / config.UPDATE_PREFS_PATH；setting_page 读取 config.DATA_DIR；
    app_config.CONFIG_PATH 是导入时从 config.DATA_DIR 拷贝出的常量，必须单独覆写。
    这些入口都在此统一指向临时目录。
    """
    data_dir = str(tmp_path_factory.mktemp("appdata"))
    monkeypatch.setattr(config, 'DATA_DIR', data_dir)
    monkeypatch.setattr(config, 'DB_PATH', os.path.join(data_dir, 'monitor.db'))
    monkeypatch.setattr(
        config, 'UPDATE_PREFS_PATH', os.path.join(data_dir, 'update_prefs.json'))
    monkeypatch.setattr(
        app_config, 'CONFIG_PATH', os.path.join(data_dir, 'config.json'))


@pytest.fixture(autouse=True)
def _reset_monitor_manager_singleton():
    """
    重置 MonitorManager 单例（所有用例前后）。

    MonitorManager 是进程级单例（_instance 类属性，仅首次构造时传入的 db 生效），
    不重置会让后续用例复用上一个用例残留的旧实例（及其绑定的旧 db、旧任务字典）。
    teardown 顺序固定：先对旧实例 stop_all_tasks() 收尾残留 QThread，再置
    _instance = None。_initialized 是实例级属性，随旧实例一起丢弃。
    """
    MonitorManager._instance = None
    yield
    old_instance = MonitorManager._instance
    if old_instance is not None:
        old_instance.stop_all_tasks()
    MonitorManager._instance = None


@pytest.fixture
def db_path(tmp_path):
    """临时数据库文件路径（不落项目 data 目录）"""
    return str(tmp_path / "test_monitor.db")


@pytest.fixture
def db(db_path):
    """注入临时库路径的 Database 实例（新库，直接建当前版本结构）"""
    return Database(db_path)


@pytest.fixture(autouse=True)
def _reset_migration_guard():
    """
    重置进程级迁移记录（MigrationRunner 按库路径记住本进程内的迁移结果）。

    该记录用于防止 backup_aborted 场景下每个新 Database() 实例重复触发备份；
    测试里 tmp_path 每个用例不同，但为避免同名路径或异常残留互相污染，用例前后都清空。
    """
    reset_guard_for_tests()
    yield
    reset_guard_for_tests()


@pytest.fixture(autouse=True)
def _reset_app_config():
    """
    重置 app_config 模块级单例 cfg 的状态。

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
