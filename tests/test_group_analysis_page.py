"""
进程组任务（进程树 / 同名进程）的分析页：成员下拉、成员结论、成员视图与结论条。

数据由真实的进程组采集线程（假时钟 + 假进程树，见 tests/test_group_analysis.py 的夹具）
写进测试库，页面与视图模型注入同步执行器，用户操作后结果当场可见。覆盖：
- 工具条的"成员"下拉：合计（默认）排第一，其后按显示名排序、括号里是出现过的实例数；pid 任务
  不显示；
- 成员视图读的是 指标@成员键 的序列（视图模型的快照、摘要表行、焦点图、区间表标题都落在
  成员序列上），切换不再查库；
- 合计视图下摘要表下方的"成员结论"折叠区：每个成员一行，点击一行整页切到该成员；
- 结论条按 core.verdict.member_overall：合计平稳而某成员持续上涨时整体判持续上涨，并点名成员；
- 上限占比：进程组合计写"—"（tooltip 说明），单实例成员保留单进程口径，多实例成员写"—"；
- 成员消失 / 范围内没有数据 / 成员加载失败 / 换任务时的回落；
- 1280×820 主窗口里首屏能看到结论条与摘要表。
"""
import dataclasses
import uuid
from datetime import datetime

import pytest
from PyQt5.QtCore import QPoint

from core.monitor_manager import MonitorManager
from core.workers.member_snapshots_worker import MemberBundle, load_member_bundle
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_analysis_page, make_main_window, make_services
from tests.group_doubles import ROOT_PID
from tests.helpers import SyncHistoryRunner, dispose_widget, process_events_until
from tests.group_doubles import inst
from tests.test_group_analysis import (  # noqa: F401 夹具
    leaking_children, rebuilt_children, root, seesaw_members)
from tests.test_group_worker import run_group
from tests.test_monitor_worker_clock import started  # noqa: F401
from ui.formatting import (
    GROUP_USAGE_NOTE, MEMBER_TOTAL_OPTION, MEMBER_USAGE_NOTE, SUMMARY_COLUMNS)
from ui.viewmodels.analysis_vm import EMPTY_MEMBER_NO_DATA, MemberOption
from utils.metrics import MetricType, split_member_key

MEM = MetricType.MEMORY_PRIVATE
GDI = MetricType.NUM_GDI_OBJECTS
COUNT = 'process_count'
COLUMNS = list(SUMMARY_COLUMNS)
NOW = datetime(2030, 1, 1)


class SpyMemberRunner:
    """成员加载执行器替身：同步执行真实的 load_member_bundle，并记下请求；
    drop 里的成员从结果里去掉，fail 为真时回一个失败的结果。"""

    def __init__(self, db):
        self.db = db
        self.requests = []
        self.drop = set()
        self.fail = False

    def __call__(self, request, on_done):
        self.requests.append(request)
        if self.fail:
            on_done(MemberBundle(request, error="读库失败"))
            return
        bundle = load_member_bundle(self.db, request)
        if self.drop:
            bundle = dataclasses.replace(bundle, snapshots={
                k: v for k, v in bundle.snapshots.items()
                if split_member_key(k)[1] not in self.drop})
        on_done(bundle)


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(clock=FakeClock(NOW))


@pytest.fixture
def runner(db):
    return SyncHistoryRunner(db)


@pytest.fixture
def member_runner(db):
    return SpyMemberRunner(db)


@pytest.fixture
def make_page(qapp, db, manager, services, runner, member_runner):
    pages = []

    def build():
        page = make_analysis_page(db, manager, services, worker_runner=runner,
                                  defer=lambda fn: fn(), member_runner=member_runner)
        page.resize(1280, 1600)
        pages.append(page)
        return page

    yield build
    for page in pages:
        page.auto_refresh.stop()
        process_events_until(lambda: services.threads.entries() == [], timeout=10)
        page.close()


def open_group(page, worker):
    page.show()
    page.select_task(worker.task_id)
    assert page.vm.last_analysis is not None
    return page.vm


# ---------------------------------------------------------------- 成员下拉


def test_selector_lists_total_then_members_sorted_by_name(make_page, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    selector = page.member_selector
    assert page.toolbar.members_visible()
    assert selector.item_texts() == [MEMBER_TOTAL_OPTION, "a.exe（1）", "app.exe（1）", "b.exe（1）"]
    assert selector.member_keys() == ["a.exe", "app.exe", "b.exe"]
    assert selector.current_member() is None and vm.current_member is None
    assert "全部成员的合计" in selector.item_tooltip(0)
    assert "出现过 1 个实例" in selector.item_tooltip(1)


def test_selector_choice_drives_view_model(make_page, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    page.member_selector.combo.setCurrentIndex(1)           # 用户在下拉里选 a.exe
    assert vm.current_member == "a.exe"
    assert page.member_selector.current_member() == "a.exe"
    page.member_selector.combo.setCurrentIndex(0)
    assert vm.current_member is None


def test_instance_count_in_option_is_distinct_instances_ever_seen(
        make_page, db, started):  # noqa: F811
    """同名进程每周期整体换 PID：下拉括号里是出现过的不同实例数，不是当前实例数。"""
    def frames(t, n):
        return [root(memory_private=100.0),
                inst('worker.exe', 6000 + n * 2, memory_private=50.0, ppid=ROOT_PID),
                inst('worker.exe', 6001 + n * 2, memory_private=50.0, ppid=ROOT_PID)]
    worker = run_group(db, started, frames, cycles=30, interval=5.0)
    page = make_page()
    vm = open_group(page, worker)
    option = next(o for o in vm.member_options if o.key == 'worker.exe')
    assert option.instances == 60
    assert page.member_selector.item_texts()[-1] == "worker.exe（60）"


def test_pid_task_has_no_member_view(make_page, db, manager, member_runner):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
                       metric_types=[MEM], interval=1.0, start_time=NOW,
                       end_time=NOW, status="stopped")
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, NOW.replace(second=i), 100.0 + i, MEM)
                         for i in range(30)])
    page = make_page()
    page.show()
    page.select_task(task.task_id)
    assert page.vm.last_analysis is not None
    assert not page.vm.is_group_task
    assert not page.toolbar.members_visible() and not page.member_panel.isVisible()
    assert member_runner.requests == []                     # pid 任务不读成员曲线
    assert page.vm.member_options == [] and page.vm.view_analysis() is page.vm.last_analysis


# ---------------------------------------------------------------- 成员视图读 @ 序列


def test_member_view_uses_member_series_keys_without_requery(
        make_page, runner, member_runner, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    queries, member_loads = len(runner.requests), len(member_runner.requests)
    assert member_loads >= 1
    shown, analyses = [], []
    vm.snapshot_ready.connect(shown.append)
    vm.analysis_ready.connect(analyses.append)

    assert vm.select_member("a.exe")

    # 整页改用 指标@成员键 的序列，且是已加载结果的重画：不再查库
    assert len(runner.requests) == queries and len(member_runner.requests) == member_loads
    assert list(analyses[-1].snapshots) == [f"{MEM}@a.exe", f"{COUNT}@a.exe"]
    assert shown[-1].request.metric == f"{MEM}@a.exe"
    assert page.summary_table.metrics() == [f"{MEM}@a.exe", f"{COUNT}@a.exe"]
    assert page.summary_table.selected_metric() == f"{MEM}@a.exe"
    assert page.trend_panel.focus_metric() == f"{MEM}@a.exe"
    assert list(page.trend_panel.charts()) == [f"{MEM}@a.exe"]
    assert page.trend_panel.folded_metrics() == [f"{COUNT}@a.exe"]
    assert "私有字节（a.exe）" in page.interval_table.title_label.text()
    assert page.sample_table.row_texts()
    rows = dict(zip(page.summary_table.metrics(), page.summary_table.row_texts()))
    assert rows[f"{MEM}@a.exe"][COLUMNS.index("verdict")] == "持续上涨"
    assert rows[f"{MEM}@a.exe"][COLUMNS.index("metric")] == "私有字节"   # 行里不重复成员名
    # 合计 / 报告用的结果不受成员选择影响
    assert list(vm.last_analysis.snapshots) == [MEM, COUNT]
    assert vm.last_snapshot.request.metric in (MEM, COUNT)

    # 成员视图里切指标：认序列键，也不查库
    page.summary_table.table.cellClicked.emit(1, 0)
    assert vm.current_metric == COUNT
    assert shown[-1].request.metric == f"{COUNT}@a.exe"
    assert page.trend_panel.focus_metric() == f"{COUNT}@a.exe"
    assert len(runner.requests) == queries

    vm.select_member(None)
    assert list(analyses[-1].snapshots) == [MEM, COUNT]
    assert shown[-1].request.metric == COUNT
    assert page.summary_table.metrics() == [MEM, COUNT]
    assert len(runner.requests) == queries


def test_member_view_survives_auto_refresh(make_page, runner, member_runner, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    vm.select_member("b.exe")
    before = len(member_runner.requests)
    vm._start_query(auto_refresh=True)                       # 定时刷新
    assert len(member_runner.requests) == before + 1
    assert vm.current_member == "b.exe"
    assert page.summary_table.metrics() == [f"{MEM}@b.exe", f"{COUNT}@b.exe"]


def test_unknown_member_is_ignored(make_page, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    assert vm.select_member("ghost.exe") is False
    assert vm.current_member is None


def test_member_view_banner_names_the_member(make_page, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    vm.select_member("a.exe")
    banner = page.verdict_banner
    assert banner.verdict() == "持续上涨"
    assert banner.sentence().startswith("成员 a.exe：1 / 2 项持续上涨：私有字节 +")
    vm.select_member("b.exe")                                 # 持续下降：不算持续上涨
    assert banner.verdict() != "持续上涨"
    assert banner.sentence().startswith("成员 b.exe：")


# ---------------------------------------------------------------- 成员结论


def test_member_conclusion_panel_lists_members_and_click_switches(make_page, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    panel = page.member_panel
    assert panel.isVisible() and panel.is_expanded()
    assert panel.title() == "成员结论 · 3 个成员，1 个持续上涨"
    assert panel.member_keys() == ["a.exe", "app.exe", "b.exe"]      # 持续上涨的在前
    first = panel.row_texts()[0]
    assert first[0] == "a.exe" and first[1] == "1 个实例" and first[2] == "持续上涨"
    assert first[3].startswith("私有字节 +")
    assert panel.row_texts()[1][2:] == ("平稳", "—")

    panel.click_member("a.exe")                                      # 点击一行，整页切到该成员
    assert vm.current_member == "a.exe"
    assert page.member_selector.current_member() == "a.exe"
    assert page.summary_table.metrics()[0] == f"{MEM}@a.exe"
    assert not panel.isVisible()                                     # 成员视图不显示成员结论

    page.member_selector.combo.setCurrentIndex(0)
    assert panel.isVisible() and panel.member_keys() == ["a.exe", "app.exe", "b.exe"]


def test_member_conclusion_panel_can_be_collapsed(make_page, seesaw_members):
    page = make_page()
    open_group(page, seesaw_members)
    panel = page.member_panel
    panel.toggle_button.click()
    assert not panel.is_expanded() and not panel.scroll.isVisible()
    assert panel.toggle_button.text() == "展开" and panel.isVisible()
    panel.toggle_button.click()
    assert panel.scroll.isVisible()


def test_member_conclusion_panel_scrolls_when_many_members(make_page, db, started):  # noqa: F811
    def frames(t, n):
        return [root(memory_private=100.0)] + [
            inst(f"m{i:02d}.exe", 7000 + i, memory_private=10.0, ppid=ROOT_PID)
            for i in range(8)]
    worker = run_group(db, started, frames, cycles=20, interval=5.0)
    page = make_page()
    open_group(page, worker)
    panel = page.member_panel
    assert len(panel.member_keys()) == 9
    assert panel.scroll.height() == 5 * 36                           # 只显示 5 行，其余滚动


# ---------------------------------------------------------------- 结论条按 member_overall


def test_banner_counts_rising_member_when_totals_are_stable(make_page, seesaw_members):
    """总量持平、成员此消彼长：合计两项都平稳，但 a.exe 持续上涨 → 整体判持续上涨并点名。"""
    page = make_page()
    vm = open_group(page, seesaw_members)
    totals = [r[COLUMNS.index("verdict")] for r in page.summary_table.row_texts()]
    assert totals == ["平稳", "平稳"]
    banner = page.verdict_banner
    assert banner.verdict() == "持续上涨"
    assert banner.sentence().startswith("合计未见持续上涨，但 1 个成员持续上涨：a.exe（私有字节 +")
    assert "a.exe" in banner.pill.toolTip()
    del vm


def test_banner_appends_members_when_totals_also_rise(make_page, leaking_children):
    page = make_page()
    open_group(page, leaking_children)
    banner = page.verdict_banner
    assert banner.verdict() == "持续上涨"
    assert banner.sentence().startswith("2 / 2 项持续上涨：")
    assert "；成员 " in banner.sentence() and "也在持续上涨" in banner.sentence()


def test_banner_stays_calm_when_nothing_rises(make_page, rebuilt_children):
    page = make_page()
    open_group(page, rebuilt_children)
    assert page.verdict_banner.verdict() != "持续上涨"
    assert page.member_panel.title().endswith("没有成员持续上涨")


def test_banner_driven_by_member_overall(make_page, seesaw_members, monkeypatch):
    """去掉成员判定则失败：把 member_overall 换成"全平稳"，结论条就只剩合计的结论。"""
    import ui.formatting as formatting
    monkeypatch.setattr(formatting, 'member_overall',
                        lambda analysis: {m: 'stable' for m in analysis.member_keys})
    page = make_page()
    open_group(page, seesaw_members)
    assert page.verdict_banner.verdict() == "平稳"
    assert "成员" not in page.verdict_banner.sentence()


# ---------------------------------------------------------------- 上限占比


@pytest.fixture
def gdi_group(db, started):  # noqa: F811
    """app.exe 单实例；worker.exe 两个实例；全部持平。"""
    def frames(t, n):
        return [root(**{GDI: 300.0}),
                inst('worker.exe', 6001, ppid=ROOT_PID, **{GDI: 200.0}),
                inst('worker.exe', 6002, ppid=ROOT_PID, **{GDI: 200.0})]
    return run_group(db, started, frames, metrics=(GDI, COUNT), cycles=140, interval=5.0)


def _usage(page):
    return {split_member_key(m)[0]: row[COLUMNS.index("usage")]
            for m, row in zip(page.summary_table.metrics(), page.summary_table.row_texts())}


def _usage_tip(page, metric):
    row = page.summary_table.row_of(metric)
    return page.summary_table.table.item(row, COLUMNS.index("usage")).toolTip()


def test_usage_is_dash_for_group_totals(make_page, gdi_group):
    page = make_page()
    open_group(page, gdi_group)
    assert _usage(page)[GDI] == "—"
    assert _usage_tip(page, GDI) == GROUP_USAGE_NOTE


def test_usage_keeps_single_process_basis_for_single_instance_member(make_page, gdi_group):
    page = make_page()
    vm = open_group(page, gdi_group)
    vm.select_member("app.exe")
    assert _usage(page)[GDI] == "3%"                                 # 300 / 10000


def test_usage_is_dash_for_multi_instance_member(make_page, gdi_group):
    page = make_page()
    vm = open_group(page, gdi_group)
    vm.select_member("worker.exe")
    assert _usage(page)[GDI] == "—"
    assert _usage_tip(page, f"{GDI}@worker.exe") == MEMBER_USAGE_NOTE.format(count=2)


# ---------------------------------------------------------------- 回落与失败


def _gone_member_frames(t, n):
    items = [root(memory_private=100.0)]
    if n < 10:                                    # gone.exe 只在任务开头的 50 秒里出现过
        items.append(inst('gone.exe', 7001, memory_private=40.0, ppid=ROOT_PID))
    return items


def test_member_without_data_in_range_shows_empty_state_and_can_switch_back(
        make_page, db, started):  # noqa: F811
    """只采私有字节时：选"最近 10 分钟"后 gone.exe 没有任何数据，页面切到空状态，切回合计恢复。"""
    worker = run_group(db, started, _gone_member_frames, metrics=(MEM,), cycles=140,
                       interval=5.0)
    page = make_page()
    vm = open_group(page, worker)
    assert vm.select_member("gone.exe")
    assert page.content_stack.currentWidget() is page.body
    vm.set_range('10m')
    assert page.content_stack.currentWidget() is page.empty_state_card
    assert page.empty_state_card.title_label.text() == EMPTY_MEMBER_NO_DATA[0]
    assert vm.current_member == "gone.exe" and vm.last_analysis is not None
    vm.select_member(None)
    assert page.content_stack.currentWidget() is page.body
    assert page.summary_table.metrics() == [MEM]


def test_member_view_only_lists_metrics_the_member_has_data_for(make_page, db, started):  # noqa: F811
    """带进程总数时 gone.exe 退出后仍有"0 个"的记录，私有字节在范围内没有数据：成员视图只列
    有数据的指标，焦点指标跟到它们上面。"""
    worker = run_group(db, started, _gone_member_frames, cycles=140, interval=5.0)
    page = make_page()
    vm = open_group(page, worker)
    vm.select_member("gone.exe")
    assert vm.current_metric == MEM
    vm.set_range('10m')
    assert page.summary_table.metrics() == [f"{COUNT}@gone.exe"]
    assert vm.current_metric == COUNT
    assert page.trend_panel.focus_metric() == f"{COUNT}@gone.exe"
    vm.select_metric(f"{MEM}@gone.exe")                        # 没有数据的指标不能选
    assert vm.current_metric == COUNT


def test_member_that_disappears_falls_back_to_totals(make_page, member_runner, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    vm.select_member("b.exe")
    member_runner.drop = {"b.exe"}
    vm.refresh()                                                      # 重载：b.exe 不在了
    assert vm.current_member is None
    assert [o.key for o in vm.member_options] == ["a.exe", "app.exe"]
    assert page.member_selector.current_member() is None
    assert page.summary_table.metrics() == [MEM, COUNT]


def test_member_load_failure_keeps_previous_members(make_page, member_runner, seesaw_members):
    page = make_page()
    vm = open_group(page, seesaw_members)
    vm.select_member("a.exe")
    member_runner.fail = True
    vm._start_query(auto_refresh=True)
    assert vm.current_member == "a.exe"
    assert [o.key for o in vm.member_options] == ["a.exe", "app.exe", "b.exe"]
    assert page.summary_table.metrics()[0] == f"{MEM}@a.exe"


def test_changing_task_resets_member_view(make_page, db, seesaw_members, started):  # noqa: F811
    """换到另一个也有 a.exe 的组任务：回到合计视图，不沿用上一个任务选的成员。"""
    def frames(t, n):
        return [root(memory_private=100.0),
                inst('a.exe', 7001, memory_private=2000.0, ppid=ROOT_PID)]
    other = run_group(db, started, frames, cycles=20, interval=5.0)
    page = make_page()
    vm = open_group(page, seesaw_members)
    vm.select_member("a.exe")
    emitted = []
    vm.members_changed.connect(emitted.append)
    page.select_task(other.task_id)
    assert emitted[0] == []                                  # 先清空旧任务的成员选项
    assert vm.current_member is None
    assert page.member_selector.current_member() is None
    assert [o.key for o in vm.member_options] == ["a.exe", "app.exe"]
    assert page.summary_table.metrics() == [MEM, COUNT]


def test_member_options_are_dataclass_values():
    assert MemberOption("a.exe", "A.exe", 2) == MemberOption("a.exe", "A.exe", 2)


# ---------------------------------------------------------------- 首屏


def test_first_screen_shows_banner_and_summary_in_1280x820_window(
        qapp, db, manager, services, seesaw_members):
    window = make_main_window(db, manager, services)
    window.about_group.check_update = lambda *args, **kwargs: None
    try:
        window.resize(1280, 820)
        window.show()
        window.switchTo(window.analysis_page)
        page = window.analysis_page
        page.select_task(seesaw_members.task_id)
        assert process_events_until(lambda: page.vm.last_analysis is not None, timeout=15)
        process_events_until(lambda: False, timeout=0.3)
        viewport = page.viewport()
        for widget in (page.verdict_banner, page.summary_table):
            assert widget.isVisible()
            top = widget.mapTo(viewport, QPoint(0, 0)).y()
            assert top >= 0 and top + widget.height() <= viewport.height(), widget
        assert page.toolbar.members_visible() and page.member_panel.isVisible()
    finally:
        page.auto_refresh.stop()
        process_events_until(lambda: not services.threads.entries(), timeout=10)
        # 不能用 close()：主窗口的真退出分支会调 QApplication.quit()，让之后某个用例里嵌套的
        # 事件循环（pytest-qt 的 waitSignal）一进去就退出而超时
        dispose_widget(window)
