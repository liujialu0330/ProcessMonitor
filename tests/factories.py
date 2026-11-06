"""
测试用的服务集合与页面工厂

make_services(**overrides) 组装一份全替身的 UiServices，可按名字替换其中任意一项；
线程登记表（threads）用真实的 ThreadRegistry（QObject，离屏可用），测试可从
services.threads.entries() 断言页面登记了哪些后台线程。

页面工厂按"页面构造函数必填注入"之后的签名编写：
    MonitorPage / AnalysisPage / SettingPage(db, manager, services, parent=None)
    AboutUpdateGroup(services, parent=None)
    MainWindow(db, manager, services)

页面工厂未给 services 时用 make_services() 的替身，但任务状态绑定到传入的真实
manager（ManagerTaskStatus），使页面看到的"是否在运行"与 manager 一致；需要断言
提示、预设对话框选择时，请自行构造 services 传入并保留引用。
"""
from dataclasses import replace
from typing import Any, Optional

from core.shutdown import ThreadRegistry
from tests.doubles import (
    FakeClock, FakePreferences, FakeProcessQuery, FakeTaskStatus,
    RecordingNotifier, ScriptedDialogs,
)
from ui.services import ManagerTaskStatus, UiServices


def make_services(**overrides: Any) -> UiServices:
    """全替身的服务集合；关键字参数按字段名替换对应服务（字段名写错会报 TypeError）。"""
    services = UiServices(
        notifier=RecordingNotifier(),
        dialogs=ScriptedDialogs(),
        clock=FakeClock(),
        process_query=FakeProcessQuery(),
        task_status=FakeTaskStatus(),
        preferences=FakePreferences(),
        threads=ThreadRegistry(),
    )
    return replace(services, **overrides) if overrides else services


def _services_for(manager, services: Optional[UiServices]) -> UiServices:
    if services is not None:
        return services
    if manager is None:
        return make_services()
    return make_services(task_status=ManagerTaskStatus(manager))


def make_monitor_page(db, manager, services: Optional[UiServices] = None, parent=None):
    """构造监控页。"""
    from ui.pages.monitor_page import MonitorPage
    return MonitorPage(db, manager, _services_for(manager, services), parent=parent)


def make_analysis_page(db, manager, services: Optional[UiServices] = None, parent=None,
                       worker_runner=None, defer=None, **vm_options):
    """构造分析页。worker_runner / defer / vm_options（table_limit、max_buckets 等）透传给
    AnalysisViewModel：测试注入同步 runner 与立即执行的 defer 即可同步观察结果；不给时
    与生产一致（后台线程查询、下一轮事件循环发出）。"""
    from ui.pages.analysis_page import AnalysisPage
    return AnalysisPage(db, manager, _services_for(manager, services), parent=parent,
                        worker_runner=worker_runner, defer=defer, **vm_options)


def make_setting_page(db, manager, services: Optional[UiServices] = None, parent=None):
    """构造设置页。"""
    from ui.pages.setting_page import SettingPage
    return SettingPage(db, manager, _services_for(manager, services), parent=parent)


def make_about_update_group(services: Optional[UiServices] = None, parent=None):
    """构造"关于与更新"分组（设置页的一部分，可脱离设置页单独测更新流程）。"""
    from ui.pages.about_update import AboutUpdateGroup
    return AboutUpdateGroup(services if services is not None else make_services(), parent=parent)


def make_main_window(db, manager, services: Optional[UiServices] = None):
    """构造主窗口。"""
    from ui.main_window import MainWindow
    return MainWindow(db, manager, _services_for(manager, services))
