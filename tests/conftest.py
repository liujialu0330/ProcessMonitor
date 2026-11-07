"""
pytest 全局 fixture
统一 sys.path（把项目根加进 sys.path）、数据目录隔离（只设环境变量，不覆写任何
路径常量）、tmp_path 数据库注入、QApplication 会话级实例、迁移记录与应用配置复位
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
from app.paths import AppPaths  # noqa: E402
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


# 释放泄漏控件前等待其子线程结束的上限（毫秒）
_LEAKED_THREAD_WAIT_MS = 3000
# 只释放本项目的控件类（界面层与测试替身）：pyqtgraph 等第三方库自己持有的无父菜单/面板
# 由其 Python 对象管理生命周期，从 C++ 侧提前删掉会在宿主析构时直接崩溃（abort）
_OWN_WIDGET_MODULES = ('ui.', 'tests.')


def _widget_key(widget) -> int:
    from PyQt5 import sip
    return int(sip.unwrapinstance(widget))


def _is_own_widget(widget) -> bool:
    return type(widget).__module__.startswith(_OWN_WIDGET_MODULES)


def _stop_child_threads(widget) -> bool:
    """控件下仍在运行的 QThread：请求结束并等待。等不到返回 False（此时不能释放控件，
    否则线程对象随父对象析构会 "QThread: Destroyed while thread is still running" 直接崩溃）。"""
    from PyQt5.QtCore import QThread
    for thread in widget.findChildren(QThread):
        if thread.isRunning():
            thread.requestInterruption()
            thread.quit()
            if not thread.wait(_LEAKED_THREAD_WAIT_MS):
                return False
    return True


def leaked_top_level_widgets(app, before) -> list:
    """用例开始后新出现、仍存活的本项目无父顶层控件。"""
    from PyQt5 import sip
    return [w for w in app.topLevelWidgets()
            if not sip.isdeleted(w) and w.parentWidget() is None
            and _is_own_widget(w) and _widget_key(w) not in before]


@pytest.fixture(autouse=True)
def _dispose_leaked_widgets():
    """
    每个用例结束后释放它留下的、仍存活的无父顶层控件（页面、主窗口、卡片、图表等）。

    无父的顶层控件不会随 Python 引用消失而析构：留下的设置页把 qconfig.themeChanged 连到
    setTheme，之后每次切主题都多发一次 themeChanged（还会把主题模式改写成具体主题）；
    qfluentwidgets 的样式表管理器给每个残留控件重刷样式表，主题切换越来越慢；残留控件的
    定时器越积越多。用例开始前已存在的顶层控件不动；有父对象的弹出窗口随父对象一起释放。
    """
    from PyQt5 import sip
    from PyQt5.QtWidgets import QApplication
    from tests.helpers import dispose_widget

    app = QApplication.instance()
    before = {_widget_key(w) for w in app.topLevelWidgets()} if app is not None else set()
    yield
    app = QApplication.instance()
    if app is None:
        return
    for widget in leaked_top_level_widgets(app, before):
        if sip.isdeleted(widget):
            continue
        if not _stop_child_threads(widget):
            import warnings
            warnings.warn(f"用例留下的控件仍有子线程在运行，未释放：{type(widget).__name__}")
            continue
        dispose_widget(widget)


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path_factory, monkeypatch):
    """
    把数据目录隔离到临时目录，任何用例都不得触碰开发机的 data 目录。

    只设置环境变量 PROCESS_MONITOR_DATA_DIR：路径统一由 app.paths.AppPaths.resolve()
    在使用时解析（config 的兼容路径常量、app_config.CONFIG_PATH、日志目录都是访问时
    现算），不再逐个覆写模块常量。返回本用例的 AppPaths，需要显式注入的用例可直接用。
    """
    data_dir = str(tmp_path_factory.mktemp("appdata"))
    monkeypatch.setenv(config.DATA_DIR_ENV, data_dir)
    return AppPaths.resolve()


@pytest.fixture
def app_paths(_isolate_data_dir):
    """本用例隔离后的应用路径（数据目录为临时目录，尚未创建日志目录）"""
    return _isolate_data_dir


@pytest.fixture
def db_path(tmp_path):
    """临时数据库文件路径（不落项目 data 目录）"""
    return str(tmp_path / "test_monitor.db")


@pytest.fixture
def db(db_path):
    """注入临时库路径的 Database 实例（新库，直接建当前版本结构）"""
    return Database(db_path)


@pytest.fixture(autouse=True)
def _no_real_update_check(monkeypatch):
    """
    任何用例里建出来的主窗口都不会真的联网检查更新。

    MainWindow 构造时登记了"3 秒后静默检查更新"的定时器；用例若没有自己把
    about_group.check_update 换成空函数，窗口随用例结束被释放后，定时器会在后面某个用例的
    事件循环里触发，对已释放的控件抛 RuntimeError（随机让 1–3 个无关用例失败）。
    在构造函数末尾统一换掉，工厂 make_main_window 与直接 MainWindow(...) 都覆盖；
    需要真实检查流程的用例直接用 AboutUpdateGroup（make_about_update_group），不经主窗口。
    """
    try:
        from ui.main_window import MainWindow
    except ImportError:     # 极少数不带界面依赖的环境：没有主窗口也就没有这个定时器
        return
    original = MainWindow.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.about_group.check_update = lambda *a, **k: None

    monkeypatch.setattr(MainWindow, '__init__', init)


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
        item_cfg.skipped_version.value = item_cfg.skipped_version.defaultValue
        item_cfg.keep_awake_while_monitoring.value = (
            item_cfg.keep_awake_while_monitoring.defaultValue)
        item_cfg.automation_enabled.value = item_cfg.automation_enabled.defaultValue
        item_cfg.report_color_scheme.value = item_cfg.report_color_scheme.defaultValue
        # 主题复位为本应用语义上的默认值 AUTO（跟随系统），而非 qfluentwidgets
        # 库自带的类默认值 Theme.LIGHT——与 load_app_config() 首启行为保持一致
        item_cfg.themeMode.value = Theme.AUTO
        item_cfg._theme = Theme.AUTO
        qconfig._cfg = qconfig
        qconfig._theme = Theme.AUTO

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _no_real_exit_probe(request, monkeypatch):
    """
    默认不让任何任务线程对真实进程取退出码句柄。

    MonitorWorker 默认用 open_exit_probe（ctypes OpenProcess）：用例里的假 PID（例如 5000）
    恰好被本机某个真实进程占用时，句柄的创建时间与预期不符，任务会被误判为 PID 已复用，
    让全量里偶发失败、单独跑却通过。默认换成"不可用"的空探针；确实要对真实子进程取退出码
    的用例用 @pytest.mark.real_exit_probe 保留真实实现。
    """
    if request.node.get_closest_marker('real_exit_probe'):
        return
    from core import monitor_task
    from core.exit_forensics import NullExitCodeProbe
    monkeypatch.setattr(monitor_task, 'open_exit_probe', lambda pid: NullExitCodeProbe())
