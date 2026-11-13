"""
响应式布局（按内容区宽度分 compact / regular / wide 三档）在各控件上的行为：

- 摘要表各档位的可见列（wide 全部、regular 去掉"增速 · R²"与"末段趋势"、compact 再合并
  "基线 → 当前"并去掉"上限占比"），"指标""判定"始终可见，用户勾选优先于档位默认，
  勾选经窗口状态记忆往返持久化；
- 小倍数图列数 3 / 2 / 1；工具条在 compact 用范围下拉；结论条在 compact 把依据放第二行；
- 主窗口里：1280 宽分析页没有横向滚动、800 宽摘要表没有被截字的列、compact 时侧栏收成图标栏；
- QT_SCALE_FACTOR=1.25 / 1.5 下构造主窗口与分析页不抛错、没有横向溢出（子进程）。
截图见 tests/visual/test_responsive_layout.py。
"""
import os
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtWidgets import QApplication

from app_config import cfg
from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock, FakePreferences, FakeWindowMemory, RecordingNotifier, ScriptedDialogs
from tests.factories import make_analysis_page, make_main_window, make_services
from tests.helpers import SyncHistoryRunner, dispose_widget, process_events_until, stop_and_join
from ui.formatting import SUMMARY_COLUMNS, SUMMARY_HEADERS
from ui.layout_breakpoints import COMPACT, REGULAR, WIDE
from ui.services import ManagerTaskStatus
from ui.widgets.analysis.summary_table import (
    ALWAYS_COLUMNS, MERGED_BASELINE_HEADER, SummaryTable, normalize_user_columns, plan_columns)
from ui.window_state import AnalysisMemory, ConfigWindowMemory, WindowMemory
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
GDI = MetricType.NUM_GDI_OBJECTS
THREADS = MetricType.NUM_THREADS
METRICS = [MEM, HANDLES, GDI, THREADS]

ALL = list(SUMMARY_COLUMNS)
REGULAR_COLUMNS = ["metric", "baseline", "current", "delta", "per_round", "verdict", "usage"]
COMPACT_COLUMNS = ["metric", "baseline", "delta", "per_round", "verdict"]


def _seed(db, manager, samples=1200):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
                       metric_types=list(METRICS), interval=1.0, start_time=BASE,
                       end_time=BASE + timedelta(seconds=samples), status="stopped")
    db.save_task(task)
    values = {MEM: lambda i: 102400.0 + i * 10, HANDLES: lambda i: 500 + i // 50,
              GDI: lambda i: 300.0, THREADS: lambda i: 24.0}
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=i), values[m](i), m)
                         for i in range(samples) for m in METRICS])
    manager.add_marker(task.task_id, "基线", BASE + timedelta(seconds=100))
    for offset, label in ((400, "第 1 轮"), (800, "第 2 轮")):
        db.add_marker(task.task_id, BASE + timedelta(seconds=offset), label)
    return task


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=2)))


@pytest.fixture
def make_page(qapp, db, manager, services):
    pages = []

    def build(width=1280):
        page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                                  defer=lambda fn: fn())
        page.resize(width, 1600)
        pages.append(page)
        return page

    yield build
    for page in pages:
        page.auto_refresh.stop()
        process_events_until(lambda: services.threads.entries() == [], timeout=10)
        page.close()


@pytest.fixture
def loaded(make_page, db, manager):
    task = _seed(db, manager)

    def load(width):
        page = make_page(width)
        page.show()
        page.select_task(task.task_id)
        QApplication.processEvents()
        return page
    return load


# ---------------------------------------------------------------- 摘要表的列


def test_plan_columns_per_tier():
    assert list(plan_columns(WIDE).visible) == ALL
    assert list(plan_columns(REGULAR).visible) == REGULAR_COLUMNS
    compact = plan_columns(COMPACT)
    assert list(compact.visible) == COMPACT_COLUMNS and compact.merged
    for tier in (WIDE, REGULAR, COMPACT):
        assert set(ALWAYS_COLUMNS) <= set(plan_columns(tier).visible)


def test_user_columns_override_the_tier_default():
    chosen = ("tail", "rate")
    for tier in (WIDE, REGULAR, COMPACT):
        plan = plan_columns(tier, chosen)
        assert list(plan.visible) == ["metric", "rate", "tail", "verdict"]
        assert not plan.merged


def test_normalize_user_columns():
    assert normalize_user_columns([]) == ()
    assert normalize_user_columns(["bogus"]) == ()
    assert normalize_user_columns(["tail", "bogus"]) == ("metric", "tail", "verdict")
    # 可选列全取消是有效勾选：只剩始终可见的两列
    assert normalize_user_columns(["verdict"]) == ("metric", "verdict")


@pytest.fixture
def table(qapp, loaded):
    page = loaded(1280)
    return page.summary_table


def _headers(table):
    return [table.table.horizontalHeaderItem(i).text() for i in range(len(SUMMARY_COLUMNS))]


@pytest.mark.parametrize("tier, expected", [
    (WIDE, ALL), (REGULAR, REGULAR_COLUMNS), (COMPACT, COMPACT_COLUMNS)])
def test_visible_columns_per_tier(table, tier, expected):
    table.set_tier(tier)
    assert table.visible_columns() == expected


def test_compact_merges_baseline_and_current(table):
    table.set_tier(COMPACT)
    baseline_col = SUMMARY_COLUMNS.index("baseline")
    row = table.rows()[0]
    assert table.merged_baseline()
    assert _headers(table)[baseline_col] == MERGED_BASELINE_HEADER
    assert table.table.item(0, baseline_col).text() == f"{row.text('baseline')} → {row.text('current')}"
    tip = table.table.item(0, baseline_col).toolTip()
    assert row.tooltips["baseline"] in tip
    table.set_tier(REGULAR)
    assert not table.merged_baseline()
    assert _headers(table)[baseline_col] == SUMMARY_HEADERS["baseline"]
    assert table.table.item(0, baseline_col).text() == row.text("baseline")


def test_hidden_rate_column_moves_into_hover_tips(table):
    row = table.rows()[0]
    rate = row.text("rate")
    assert rate not in ("--", "—")
    table.set_tier(WIDE)
    assert "增速 · R²" not in table.table.item(0, 0).toolTip()
    for tier in (REGULAR, COMPACT):
        table.set_tier(tier)
        expected = f"增速 · R²：{rate}"
        assert expected in table.table.item(0, 0).toolTip()
        assert expected in table.table.cellWidget(0, SUMMARY_COLUMNS.index("verdict")).toolTip()


def test_verdict_and_metric_columns_stay_visible_when_user_unchecks_everything(table):
    table.set_user_columns(("verdict",))
    for tier in (WIDE, REGULAR, COMPACT):
        table.set_tier(tier)
        assert table.visible_columns() == ["metric", "verdict"]


def test_user_choice_beats_tier_in_the_widget(table):
    table.set_tier(COMPACT)
    table.set_user_columns(("tail", "current"))
    assert table.visible_columns() == ["metric", "current", "tail", "verdict"]
    assert not table.merged_baseline()
    table.set_tier(WIDE)
    assert table.visible_columns() == ["metric", "current", "tail", "verdict"]


def test_usage_column_stays_hidden_without_limit_data_even_when_chosen(make_page, db, manager):
    task = _seed(db, manager)
    page = make_page(1280)
    page.show()
    page.select_task(task.task_id)
    page.summary_table.set_tier(WIDE)
    assert "usage" in page.summary_table.visible_columns()       # 夹具里有 GDI 对象数
    page.summary_table.set_rows(
        [r for r in page.summary_table.rows() if r.metric == MEM], "x")
    page.summary_table.set_user_columns(("usage", "delta"))
    assert page.summary_table.visible_columns() == ["metric", "delta", "verdict"]


def test_header_menu_toggles_columns_and_resets(table):
    changes = []
    table.columns_changed.connect(changes.append)
    table.set_tier(REGULAR)
    menu = table.column_menu()
    assert menu is table.last_menu
    actions = table.column_actions
    assert [c for c in SUMMARY_COLUMNS if actions[c].isChecked()] == REGULAR_COLUMNS
    assert not actions["metric"].isEnabled() and not actions["verdict"].isEnabled()
    assert not table.reset_action.isEnabled()

    actions["tail"].trigger()
    assert table.visible_columns() == ["metric", "baseline", "current", "delta", "per_round",
                                       "tail", "verdict", "usage"]
    assert changes[-1] == table.user_columns()
    assert "tail" in changes[-1]

    table.column_menu()
    assert table.column_actions["tail"].isChecked()
    assert table.reset_action.isEnabled()
    table.column_actions["delta"].trigger()
    assert "delta" not in table.visible_columns()

    table.column_menu()
    table.reset_action.trigger()
    assert table.user_columns() == () and changes[-1] == ()
    assert table.visible_columns() == REGULAR_COLUMNS


def test_menu_in_compact_counts_merged_columns_as_checked(table):
    table.set_tier(COMPACT)
    table.column_menu()
    checked = [c for c in SUMMARY_COLUMNS if table.column_actions[c].isChecked()]
    assert checked == ["metric", "baseline", "current", "delta", "per_round", "verdict"]
    table.column_actions["current"].trigger()           # 取消"当前"：只剩基线，不再合并
    assert table.visible_columns() == ["metric", "baseline", "delta", "per_round", "verdict"]
    assert not table.merged_baseline()
    baseline_col = SUMMARY_COLUMNS.index("baseline")
    assert table.table.item(0, baseline_col).text() == table.rows()[0].text("baseline")


def test_right_click_on_header_pops_up_the_menu(table, monkeypatch):
    from qfluentwidgets import RoundMenu
    shown = []
    monkeypatch.setattr(RoundMenu, "exec", lambda self, *a, **k: shown.append(self))
    header = table.table.horizontalHeader()
    header.customContextMenuRequested.emit(header.rect().center())
    assert shown == [table.last_menu]


def test_set_tier_keeps_selection_and_rows(table):
    table.select_metric(HANDLES)
    for tier in (COMPACT, WIDE, REGULAR):
        table.set_tier(tier)
        assert table.selected_metric() == HANDLES
        assert len(table.rows()) == len(METRICS)


# ---------------------------------------------------------------- 记忆往返


@pytest.fixture
def restore_cfg():
    saved = cfg.analysis_summary_columns.value
    yield
    cfg.analysis_summary_columns.value = saved


def test_memory_roundtrip_through_the_page(loaded):
    page = loaded(1000)             # regular 档默认不显示"末段趋势"，勾选它才是一次有效改动
    assert page.memory_snapshot().summary_columns == ()
    page.summary_table.column_menu()
    page.summary_table.column_actions["tail"].trigger()
    snapshot = page.memory_snapshot()
    assert "tail" in snapshot.summary_columns

    again = loaded(800)
    again.restore_memory(snapshot)
    assert again.summary_table.user_columns() == snapshot.summary_columns
    assert "tail" in again.summary_table.visible_columns()
    # 窄档位下用户勾选仍然生效
    assert again.summary_table.tier() == COMPACT
    assert not again.summary_table.merged_baseline()


def test_memory_roundtrip_through_config(restore_cfg):
    store = ConfigWindowMemory(screens=lambda: [(0, 0, 1920, 1040)])
    columns = ("metric", "delta", "tail", "verdict")
    memory = WindowMemory(analysis=AnalysisMemory(summary_columns=columns))
    assert store.save(memory) is True
    assert store.load().analysis.summary_columns == columns
    assert cfg.analysis_summary_columns.value == "metric,delta,tail,verdict"
    assert store.save(memory) is False
    store.save(WindowMemory())
    assert store.load().analysis.summary_columns == ()


@pytest.mark.parametrize("raw", [None, 5, ["x"], "x" * 200])
def test_corrupted_columns_in_config_mean_default(restore_cfg, raw):
    cfg.analysis_summary_columns.value = raw
    store = ConfigWindowMemory(screens=lambda: [(0, 0, 1920, 1040)])
    columns = store.load().analysis.summary_columns
    table = SummaryTable()
    table.set_user_columns(columns)
    assert table.user_columns() == () or set(table.user_columns()) <= set(SUMMARY_COLUMNS)


def test_main_window_schedules_a_save_when_columns_change(tmp_path, qapp, db):
    manager = MonitorManager(db=db)
    fake = FakeWindowMemory()
    services = make_services(
        notifier=RecordingNotifier(), dialogs=ScriptedDialogs(), preferences=FakePreferences(),
        task_status=ManagerTaskStatus(manager), window_memory=fake)
    win = make_main_window(db, manager, services)
    win.about_group.check_update = lambda *args, **kwargs: None
    try:
        win._state_timer.stop()
        table = win.analysis_page.summary_table
        table.column_menu()
        table.column_actions["tail"].trigger()
        assert win._state_timer.isActive()
        win._save_window_state()
        assert "tail" in fake.memory.analysis.summary_columns
    finally:
        win._state_timer.stop()
        stop_and_join(manager)
        dispose_widget(win)


# ---------------------------------------------------------------- 小倍数图列数


@pytest.mark.parametrize("width, columns", [(1920, 3), (1400, 3), (1280, 3), (1099, 2), (1000, 2),
                                            (800, 1)])
def test_small_multiple_columns_follow_the_tier(loaded, width, columns):
    page = loaded(width)
    page.trend_panel.expand_all()
    QApplication.processEvents()
    assert len(page.trend_panel.small_charts()) >= 3
    assert page.trend_panel.columns() == columns


def test_columns_never_exceed_the_chart_count(loaded):
    page = loaded(1920)
    page.trend_panel.expand_all()
    page.trend_panel.set_analysis(page.vm.last_analysis, order=[MEM, HANDLES], focus=MEM)
    assert len(page.trend_panel.small_charts()) == 1
    assert page.trend_panel.columns() == 1


def test_columns_follow_a_tier_change_after_the_window_resizes(loaded):
    page = loaded(1920)
    page.trend_panel.expand_all()
    QApplication.processEvents()
    assert page.trend_panel.columns() == 3
    page.resize(1000, 1600)
    QApplication.processEvents()
    assert page.trend_panel.columns() == 2
    page.resize(760, 1600)
    QApplication.processEvents()
    assert page.trend_panel.columns() == 1


def _x_range(chart):
    return tuple(chart.chart_widget.getPlotItem().vb.viewRange()[0])


def test_constant_value_small_charts_do_not_stretch_the_focus_time_axis(loaded):
    """全部展开且有恒定值指标（线程数、GDI 对象数）时，焦点大图的横轴仍是它自己的数据范围，
    各小图与焦点图显示同一段时间。曾经用 pyqtgraph 的 setXLink：小图按屏幕位置对齐、
    按自己的数据自适应，每张小图的时间轴互相错开，还把焦点图的横轴拉到数据之外。"""
    page = loaded(1280)
    panel = page.trend_panel
    focus = panel.focus_chart
    before = _x_range(focus)
    data_low, data_high = min(focus.x_values), max(focus.x_values)
    panel.expand_all()
    QApplication.processEvents()
    after = _x_range(focus)
    assert after == before
    assert after[1] - after[0] <= (data_high - data_low) * 1.2
    constant = {GDI, THREADS} & set(panel.small_charts())
    assert constant, "夹具里应有恒定值的小图"
    for metric, chart in panel.small_charts().items():
        assert _x_range(chart) == after, metric


def test_small_charts_follow_zoom_and_pan_in_both_directions(loaded):
    page = loaded(1280)
    panel = page.trend_panel
    panel.expand_all()
    QApplication.processEvents()
    low, high = _x_range(panel.focus_chart)
    quarter = (high - low) / 4
    panel.zoom_to(low + quarter, high - quarter)
    expected = _x_range(panel.focus_chart)
    for chart in panel.small_charts().values():
        assert _x_range(chart) == expected
    # 在某张小图上平移，焦点图与其他小图跟着走
    charts = list(panel.small_charts().values())
    charts[0].chart_widget.getPlotItem().vb.setXRange(low, low + quarter, padding=0)
    QApplication.processEvents()
    assert _x_range(panel.focus_chart) == (low, low + quarter)
    assert all(_x_range(c) == (low, low + quarter) for c in charts)


def test_a_small_chart_created_later_starts_on_the_shared_range(loaded):
    """展开 / 换焦点时新建的小图，横轴一开始就是焦点图当前的范围。"""
    page = loaded(1280)
    panel = page.trend_panel
    panel.expand_all()
    QApplication.processEvents()
    low, high = _x_range(panel.focus_chart)
    panel.zoom_to(low + 100, low + 400)
    shared = _x_range(panel.focus_chart)
    chart = panel._make_small()
    assert _x_range(chart) == shared
    chart.deleteLater()


# ---------------------------------------------------------------- 工具条与结论条


def test_toolbar_uses_a_range_combo_in_compact(loaded):
    page = loaded(760)
    bar = page.toolbar
    assert page.breakpoints.tier == COMPACT and bar.range_as_combo()
    assert bar.range_combo.isVisible() and not bar.range_segmented.isVisible()
    assert bar.views_inline()
    picked = []
    bar.range_changed.connect(picked.append)
    bar.range_combo.setCurrentIndex(1)                     # 10 分钟
    assert picked == ["10m"] and bar.current_range() == "10m"
    bar.set_range("all")                                    # 程序设置不发信号，两个控件同步
    assert picked == ["10m"]
    assert bar.range_combo.currentText() == "全部"

    page.resize(1280, 1600)
    QApplication.processEvents()
    assert not bar.range_as_combo()
    assert bar.range_segmented.isVisible() and not bar.range_combo.isVisible()
    assert bar.current_range() == "all"


def test_range_combo_disables_unavailable_items(loaded):
    bar = loaded(760).toolbar
    index = {key: i for i, key in enumerate(("baseline", "10m", "1h", "6h", "24h", "all", "custom"))}
    bar.set_custom_range_text(None)
    assert bar.range_combo.itemData(index["custom"]) == "custom"
    # 刷选出范围后"自定义"可选，并在选中时写明起止
    bar.set_custom_range_text("08:00 – 08:10")
    bar.set_range("custom")
    assert bar.range_combo.toolTip() == "08:00 – 08:10"


def test_banner_moves_basis_to_a_second_row_in_compact(loaded):
    page = loaded(760)
    banner = page.verdict_banner
    assert banner.details_on_second_row() and banner.basis()
    assert banner.basis_label.isVisible()
    assert banner.basis_label.geometry().top() > banner.sentence_label.geometry().bottom() - 4
    assert banner.copy_button.geometry().top() > banner.sentence_label.geometry().bottom() - 4
    assert banner.sentence_label.fontMetrics().horizontalAdvance(banner.sentence()) \
        <= banner.sentence_label.width()                   # 主句没被省略
    page.resize(1280, 1600)
    QApplication.processEvents()
    assert not banner.details_on_second_row()
    for widget in (banner.basis_label, banner.copy_button):
        assert abs(widget.geometry().center().y()
                   - banner.sentence_label.geometry().center().y()) < 8


# ---------------------------------------------------------------- 主窗口：宽度档位下的布局


@pytest.fixture
def main_window(tmp_path, qapp, db):
    made = []

    def build(width, height=820):
        manager = MonitorManager(db=db)
        services = make_services(
            notifier=RecordingNotifier(), dialogs=ScriptedDialogs(),
            preferences=FakePreferences(), task_status=ManagerTaskStatus(manager))
        win = make_main_window(db, manager, services)
        win.about_group.check_update = lambda *args, **kwargs: None
        made.append((win, manager, services))
        win.resize(width, height)
        win.show()
        win.switchTo(win.analysis_page)
        return win, services

    yield build
    for win, manager, services in made:
        process_events_until(lambda: not services.threads.entries(), timeout=10)
        stop_and_join(manager)
        dispose_widget(win)


def _load(win, services, task):
    page = win.analysis_page
    page.select_task(task.task_id)
    assert process_events_until(lambda: page.vm.last_analysis is not None
                                and page.vm.current_task_id == task.task_id, timeout=15)
    assert process_events_until(lambda: not services.threads.entries(), timeout=10)
    process_events_until(lambda: False, timeout=0.2)
    return page


def _assert_no_clipped_columns(table):
    header = table.table.horizontalHeader()
    assert table.columns_fit()
    for c, column in enumerate(SUMMARY_COLUMNS):
        if table.table.isColumnHidden(c):
            continue
        assert header.sectionSize(c) >= table.table.sizeHintForColumn(c), column
        assert header.sectionSize(c) >= header.sectionSizeHint(c), column


def test_1280_has_no_horizontal_scroll(main_window, db, manager):
    task = _seed(db, manager)
    win, services = main_window(1280)
    page = _load(win, services, task)
    # 默认窗口的内容区约 1130，属 wide：9 列全部显示、不截字、没有横向滚动
    assert page.breakpoints.tier == WIDE
    assert page.horizontalScrollBar().maximum() == 0
    assert page.widget().width() <= page.viewport().width()
    _assert_no_clipped_columns(page.summary_table)
    assert page.summary_table.visible_columns() == ALL


def test_1000_is_regular_and_does_not_clip(main_window, db, manager):
    task = _seed(db, manager)
    win, services = main_window(1000, 820)
    page = _load(win, services, task)
    assert page.breakpoints.tier == REGULAR
    assert page.horizontalScrollBar().maximum() == 0
    _assert_no_clipped_columns(page.summary_table)
    assert page.summary_table.visible_columns() == REGULAR_COLUMNS


def test_800_does_not_clip_summary_columns_or_overflow(main_window, db, manager):
    task = _seed(db, manager)
    win, services = main_window(800, 700)
    page = _load(win, services, task)
    assert page.breakpoints.tier == COMPACT
    assert page.horizontalScrollBar().maximum() == 0
    assert page.widget().width() <= page.viewport().width()
    table = page.summary_table
    assert table.visible_columns() == COMPACT_COLUMNS and table.merged_baseline()
    _assert_no_clipped_columns(table)
    # 表里的每个单元格文字都放得下（用字体度量判断，不靠 sizeHint 自证）
    metrics = table.table.fontMetrics()
    for r in range(table.table.rowCount()):
        for c, column in enumerate(SUMMARY_COLUMNS):
            if table.table.isColumnHidden(c) or column == "verdict":
                continue
            text = table.table.item(r, c).text()
            assert metrics.horizontalAdvance(text) <= table.table.columnWidth(c), (r, column, text)
    # 结论条主句与工具条各控件都在页面可见宽度之内
    viewport_width = page.viewport().width()
    for widget in (page.toolbar.more_button, page.toolbar.range_combo,
                   page.verdict_banner.copy_button, page.toolbar.views):
        right = widget.mapTo(page.viewport(), widget.rect().topRight()).x()
        assert right <= viewport_width, widget


def test_navigation_collapses_to_icons_when_compact(main_window, db, manager):
    from qfluentwidgets import NavigationDisplayMode
    win, _services = main_window(1500)
    panel = win.navigationInterface.panel
    assert process_events_until(lambda: win.breakpoints.tier == WIDE)
    panel.expand(useAni=False)
    assert panel.displayMode == NavigationDisplayMode.EXPAND
    win.resize(800, 700)
    assert process_events_until(lambda: win.breakpoints.tier == COMPACT)
    assert process_events_until(lambda: panel.isCollapsed(), timeout=3)


def test_compact_tier_collapses_an_expanded_sidebar(main_window):
    """窗口还不窄于 qfluentwidgets 自己的收起阈值时，进入 compact 也要把挤占内容区的侧栏收起。"""
    from qfluentwidgets import NavigationDisplayMode
    win, _services = main_window(1500)
    panel = win.navigationInterface.panel
    panel.expand(useAni=False)
    assert panel.displayMode == NavigationDisplayMode.EXPAND
    win._on_layout_tier_changed(COMPACT)
    assert process_events_until(lambda: panel.isCollapsed(), timeout=3)
    win._on_layout_tier_changed(WIDE)                      # 回到宽档位不会自动再展开
    assert panel.isCollapsed()


# ---------------------------------------------------------------- 高 DPI 冒烟（子进程）


_SCALED_SCRIPT = textwrap.dedent(r"""
    import sys, uuid
    from datetime import datetime, timedelta
    sys.path.insert(0, {root!r})
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    app = QApplication([])
    from core.monitor_manager import MonitorManager
    from data.database import Database
    from data.models import DataPoint, MonitorTask
    from tests.factories import make_main_window, make_services
    from tests.helpers import process_events_until
    from utils.metrics import MetricType
    base = datetime(2026, 7, 16, 16, 0, 0)
    metrics = [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES, MetricType.NUM_GDI_OBJECTS]
    db = Database({db!r})
    manager = MonitorManager(db=db)
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=1, process_name="demo.exe",
                       metric_types=metrics, interval=1.0, start_time=base,
                       end_time=base + timedelta(seconds=1200), status="stopped")
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, base + timedelta(seconds=i), v, m)
                         for i in range(1200)
                         for m, v in zip(metrics, (102400.0 + i * 10, 500.0 + i // 50, 300.0))])
    manager.add_marker(task.task_id, "基线", base + timedelta(seconds=100))
    services = make_services()
    win = make_main_window(db, manager, services)
    win.about_group.check_update = lambda *a, **k: None
    ratio = app.primaryScreen().devicePixelRatio()
    for width, height in ((1280, 820), (800, 700)):
        win.resize(width, height)
        win.show()
        win.switchTo(win.analysis_page)
        page = win.analysis_page
        page.select_task(task.task_id)
        assert process_events_until(lambda: page.vm.last_analysis is not None, timeout=15)
        assert process_events_until(lambda: not services.threads.entries(), timeout=10)
        process_events_until(lambda: False, timeout=0.3)
        overflow = page.horizontalScrollBar().maximum()
        fits = page.summary_table.columns_fit()
        win.switchTo(win.monitor_page)
        process_events_until(lambda: False, timeout=0.2)
        monitor_overflow = win.monitor_page.horizontalScrollBar().maximum()
        win.switchTo(win.analysis_page)
        print("RESULT", ratio, width, overflow, fits, monitor_overflow, page.breakpoints.tier)
    # 最后一次切页仍可能发起后台查询，必须像正式入口一样收完登记线程再销毁窗口。
    shutdown = []
    real_shutdown = win._run_shutdown
    win._run_shutdown = lambda: shutdown.append(real_shutdown()) or shutdown[-1]
    win.request_quit()
    assert shutdown == [True]
    assert not services.threads.entries()
    from tests.helpers import dispose_widget
    dispose_widget(win)
    sys.stdout.flush()
""")


@pytest.mark.parametrize("scale", ["1.25", "1.5"])
def test_high_dpi_smoke(tmp_path, scale):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = _SCALED_SCRIPT.format(root=root, db=str(tmp_path / "scaled.db"))
    env = dict(os.environ, QT_SCALE_FACTOR=scale, QT_QPA_PLATFORM="offscreen",
               PYTHONIOENCODING="utf-8", PROCESS_MONITOR_DATA_DIR=str(tmp_path / "appdata"))
    proc = subprocess.run([sys.executable, "-u", "-X", "faulthandler", "-c", code], cwd=root, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    lines = [line.split() for line in proc.stdout.splitlines() if line.startswith("RESULT")]
    assert proc.returncode == 0 and len(lines) == 2, (
        f"退出码 {proc.returncode}\nstdout: {proc.stdout[-1500:]}\nstderr: {proc.stderr[-3000:]}")
    for (_tag, ratio, width, overflow, fits, monitor_overflow, tier), expected in zip(
            lines, (("1280", "wide"), ("800", "compact"))):
        # 离屏屏幕的逻辑 DPI 不是 96，比例会比 QT_SCALE_FACTOR 略大；关键是确实不是 1
        assert float(ratio) >= float(scale) - 1e-6
        assert (width, tier) == expected
        assert int(overflow) == 0 and fits == "True" and int(monitor_overflow) == 0

