"""
分析页组装：控件信号 → 视图模型命令的连线，以及视图模型信号驱动的关键可见状态
（判定摘要、小倍数/叠加、相对时间、刷选自定义范围、区间缩放、右键打点编辑、删除、
一键报告导出与复制、任务对比、空状态），以及原历史页/导出页沿用到分析页的页面行为：
任务下拉文案与运行中时长刷新、加载态与读取失败、自动刷新跳过、删除确认、导出与清理
数据库互斥、导出失败提示、保存目录记忆。业务规则见 tests/test_analysis_vm.py。

视图模型注入同步查询执行器（SyncHistoryRunner）与立即执行的 defer，用户操作后结果
当场可见；导出走真实后台线程（ThreadRegistry 登记），用 process_events_until 等完成。
"""
import dataclasses
import os
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from core.history_query import CUSTOM_RANGE_KEY
from core.monitor_manager import MonitorManager
from data.errors import StorageError
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock, ScriptedDialogs
from tests.factories import make_analysis_page, make_services
from tests.helpers import (
    ManualDefer, ManualHistoryRunner, SyncHistoryRunner, process_events_until)
from ui.pages.analysis_page import TASK_HAS_NO_DATA
from ui.services import QtDialogs
from ui.viewmodels.analysis_vm import TIME_RELATIVE, VIEW_OVERLAY, VIEW_SMALL
from ui.widgets.analysis.export_flow import MAINTENANCE, NO_DATA
from ui.widgets.analysis.interval_table import VIEW_SAMPLES
from ui.widgets.analysis.marker_menu import ADD_TEXT, BASELINE_TEXT, DELETE_TEXT, RENAME_TEXT
from ui.widgets.analysis.toolbar import EXPORT_COPY, EXPORT_CSV, EXPORT_REPORT
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
GDI = MetricType.NUM_GDI_OBJECTS
IO = MetricType.IO_READ_BYTES
METRICS = [MEM, HANDLES, GDI, IO]
SAMPLES = 600


def _seed(db, manager, *, name="demo.exe", start=BASE, metrics=METRICS, samples=SAMPLES,
          slope=10.0, markers=((200, "第 1 轮"), (400, "第 2 轮")), baseline=100,
          status="stopped", note=""):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name=name,
                       metric_types=list(metrics), interval=1.0, start_time=start,
                       end_time=None if status == "running" else start + timedelta(seconds=samples),
                       status=status, note=note)
    db.save_task(task)
    values = {MEM: lambda i: 102400.0 + i * slope, HANDLES: lambda i: 500 + i // 50,
              GDI: lambda i: 300.0, IO: lambda i: 1000.0 * i}
    db.save_data_points([DataPoint(task.task_id, start + timedelta(seconds=i), values[m](i), m)
                         for i in range(samples) for m in metrics])
    if baseline is not None:
        manager.add_marker(task.task_id, "基线", start + timedelta(seconds=baseline))
    for offset, label in markers:
        db.add_marker(task.task_id, start + timedelta(seconds=offset), label)
    return task


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=2)))


@pytest.fixture
def runner(db):
    return SyncHistoryRunner(db)


@pytest.fixture
def make_page(qapp, db, manager, services, runner):
    pages = []

    def build():
        page = make_analysis_page(db, manager, services, worker_runner=runner,
                                  defer=lambda fn: fn())
        page.resize(1280, 1600)
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
    page = make_page()
    page.show()
    page.select_task(task.task_id)
    return page, task


# ---------------------------------------------------------------- 载入与焦点


def test_loads_summary_small_multiples_and_intervals(loaded):
    page, task = loaded
    assert page.current_task_id == task.task_id and page.current_metric == MEM
    assert page.content_stack.currentWidget() is page.body
    assert [r[0] for r in page.summary_table.row_texts()][:2] == ["私有字节", "句柄数"]
    assert len(page.summary_table.row_texts()) == 4
    assert page.summary_table.selected_metric() == MEM
    assert page.summary_table.basis().startswith("依据：")
    charts = page.trend_panel.charts()
    assert list(charts) == METRICS
    assert all(chart.has_data() for chart in charts.values())
    assert charts[MEM].band_items()                     # min/max 带
    assert page.trend_panel.columns() == 2
    assert [r[0] for r in page.interval_table.row_texts()] == [
        "开始 → 基线", "基线 → 第 1 轮", "第 1 轮 → 第 2 轮", "第 2 轮 → 结束"]
    assert page.trend_panel.overview.has_data()
    assert page.toolbar.current_range() == 'all'


def test_focus_switch_reuses_batch(loaded, runner):
    page, _task = loaded
    queries = len(runner.requests)
    page.summary_table.table.cellClicked.emit(1, 0)
    assert page.current_metric == HANDLES
    assert len(runner.requests) == queries            # 复用已取回的快照，不重查
    assert "句柄数" in page.interval_table.title_label.text()
    page.lower_views.set_view(VIEW_SAMPLES)
    assert page.lower_views.stack.currentWidget() is page.sample_table
    assert page.sample_table.row_texts()


def test_empty_state_and_task_without_data(make_page, db, services):
    page = make_page()
    page.show()
    assert page.content_stack.currentWidget() is page.empty_state_card
    empty = MonitorTask(task_id=str(uuid.uuid4()), pid=1, process_name="x.exe",
                        metric_types=[MEM], interval=1.0, start_time=BASE, end_time=None,
                        status="stopped")
    db.save_task(empty)
    page.select_task(empty.task_id)
    assert services.notifier.last('info').title == TASK_HAS_NO_DATA[0]
    assert page.current_task_id is None


# ---------------------------------------------------------------- 视图与范围


def test_view_mode_switch_and_relative_time(loaded):
    page, _task = loaded
    page.toolbar.view_segmented.setCurrentItem(VIEW_OVERLAY)
    assert page.vm.view_mode == VIEW_OVERLAY
    assert page.trend_panel.stack.currentIndex() == 1
    # 累计量（I/O 读取字节）不叠加，列在说明里
    assert page.trend_panel.overlay_chart.legend_names() == ["私有字节", "句柄数", "GDI对象数"]
    assert page.trend_panel.overlay_note.text().startswith("未叠加（累计量或参考值为 0）：")
    page.toolbar.view_segmented.setCurrentItem(VIEW_SMALL)
    assert page.trend_panel.stack.currentIndex() == 0

    page.toolbar.time_segmented.setCurrentItem(TIME_RELATIVE)
    origin = (BASE + timedelta(seconds=100)).timestamp()       # 基线时刻
    assert all(chart.relative_origin == origin for chart in page.trend_panel.charts().values())
    assert page.trend_panel.charts()[MEM].hover_text(0).startswith("T-")


def test_overview_brush_sets_custom_range_and_all_clears(loaded):
    page, _task = loaded
    start, end = BASE + timedelta(seconds=150), BASE + timedelta(seconds=450)
    page.trend_panel.overview.select(start.timestamp(), end.timestamp())
    assert page.vm.range_key == CUSTOM_RANGE_KEY
    assert page.toolbar.current_range() == CUSTOM_RANGE_KEY and page.toolbar.custom_available()
    xs = page.trend_panel.charts()[MEM].x_values
    assert start.timestamp() - 1 <= xs[0] and xs[-1] <= end.timestamp() + 1
    assert page.trend_panel.overview.x_bounds()[0] == pytest.approx(BASE.timestamp())

    page.toolbar.range_segmented.setCurrentItem('all')
    assert page.vm.range_key == 'all'
    assert not page.toolbar.custom_available()
    assert page.trend_panel.charts()[MEM].x_values[0] == pytest.approx(BASE.timestamp())


def test_interval_row_click_zooms_trend(loaded):
    page, _task = loaded
    page.interval_table.activate_row(2)                 # 第 1 轮 → 第 2 轮：[200, 400)
    low, high = page.trend_panel.visible_x_range()
    assert low == pytest.approx((BASE + timedelta(seconds=196)).timestamp(), abs=2)
    assert high == pytest.approx((BASE + timedelta(seconds=404)).timestamp(), abs=2)


# ---------------------------------------------------------------- 打点编辑与删除


def test_marker_menu_items_and_edits(loaded, db, services):
    page, task = loaded
    flow, dialogs = page.marker_flow, services.dialogs
    x = (BASE + timedelta(seconds=500)).timestamp()
    assert [(text, enabled) for text, _fn, enabled in flow.items(x, None)] == [
        (ADD_TEXT, True), (BASELINE_TEXT, False), (RENAME_TEXT, False), (DELETE_TEXT, False)]

    dialogs.push('ask_marker_label', "第 3 轮")
    assert flow.add_at(x)
    assert dialogs.last_call('ask_marker_label')['default_label'] == "第 3 轮"
    assert len(page.interval_table.row_texts()) == 5    # 打点后静默重查，区间表随之更新

    marker = next(m for m in db.markers.list(task.task_id) if m.label == "第 3 轮")
    dialogs.push('ask_marker_label', "收尾")
    assert flow.rename(marker)
    marker = next(m for m in db.markers.list(task.task_id) if m.label == "收尾")
    assert page.interval_table.row_texts()[-1][0] == "收尾 → 结束"

    first = next(m for m in db.markers.list(task.task_id) if m.label == "第 1 轮")
    assert [enabled for _t, _f, enabled in flow.items(x, first)] == [True, True, True, True]
    assert flow.set_baseline(first)
    kinds = {m.label: m.kind for m in db.markers.list(task.task_id)}
    assert kinds.get("第 1 轮") == 'baseline'

    dialogs.push('confirm', True)
    assert flow.delete(marker)
    assert dialogs.last_call('confirm')['title'] == "删除打点"
    assert "收尾" not in [m.label for m in db.markers.list(task.task_id)]


def test_context_menu_signal_opens_menu(loaded):
    page, _task = loaded
    chart = page.trend_panel.charts()[MEM]
    marker = next(m for m in chart.markers if m.label == "第 1 轮")
    chart.context_menu_requested.emit(marker.timestamp.timestamp(), marker,
                                      page.mapToGlobal(page.rect().center()))
    menu = page.marker_flow.last_menu
    assert [a.text() for a in menu.actions() if a.text()] == [
        ADD_TEXT, BASELINE_TEXT, RENAME_TEXT, DELETE_TEXT]
    assert all(a.isEnabled() for a in menu.actions() if a.text())
    menu.close()


def test_delete_task_data(loaded, db, services):
    page, task = loaded
    services.dialogs.push('confirm', True)
    page.toolbar.delete_clicked.emit()
    assert services.notifier.last('success').title == "删除成功"
    assert db.get_task(task.task_id) is None
    assert page.content_stack.currentWidget() is page.empty_state_card


# ---------------------------------------------------------------- 导出与复制


def test_report_export_writes_three_files(loaded, services, tmp_path):
    page, task = loaded
    out = tmp_path / "out"
    out.mkdir()
    chosen = str(out / "demo_报告.png")
    services.dialogs.push('ask_save_path', chosen)
    page.toolbar.export_requested.emit(EXPORT_REPORT)
    assert page.export_status_label.text() == "正在导出…"
    assert not page.toolbar.export_button.isEnabled()
    assert process_events_until(
        lambda: services.threads.entries() == [] and services.notifier.messages('success'),
        timeout=20)
    assert sorted(os.listdir(out)) == ["demo.csv", "demo_报告.png", "demo_摘要.csv"]
    notice = services.notifier.last('success')
    assert notice.title == "导出成功" and "demo_报告.png" in notice.content
    assert not page.export_status_label.isVisible()
    assert page.toolbar.export_button.isEnabled()
    page.export_flow.open_folder_button.click()
    assert services.dialogs.last_call('open_folder')['path'] == str(out)

    # 再次导出到同一位置：确认覆盖时取消 → 不启动
    services.dialogs.push('ask_save_path', chosen)
    services.dialogs.push('confirm', False)
    page.toolbar.export_requested.emit(EXPORT_REPORT)
    assert services.dialogs.last_call('confirm')['confirm_text'] == "覆盖"
    assert services.threads.entries() == []


def test_csv_export_and_copy_image(loaded, services, tmp_path):
    page, _task = loaded
    path = str(tmp_path / "only.csv")
    services.dialogs.push('ask_save_path', path)
    page.toolbar.export_requested.emit(EXPORT_CSV)
    assert process_events_until(lambda: services.threads.entries() == [] and os.path.exists(path),
                                timeout=20)
    with open(path, encoding="utf-8-sig") as f:
        assert f.readline().startswith("# ")

    page.toolbar.export_requested.emit(EXPORT_COPY)
    pixmap = services.dialogs.last_call('copy_pixmap')['pixmap']
    assert not pixmap.isNull() and pixmap.width() >= 1000
    assert services.notifier.last('success').title == "复制成功"


def test_export_without_data_warns(make_page, services):
    page = make_page()
    page.show()
    page.toolbar.export_requested.emit(EXPORT_REPORT)
    assert services.notifier.last('warning').title == NO_DATA[0]
    assert services.dialogs.calls_of('ask_save_path') == []


# ---------------------------------------------------------------- 任务对比


def test_compare_panel_overlays_two_tasks(loaded, db, manager):
    page, _task = loaded
    old = _seed(db, manager, name="old.exe", start=BASE - timedelta(days=1), slope=4.0)
    page.vm.reload_tasks()
    page.toolbar.compare_button.setChecked(True)
    assert page.compare_panel.isVisible()
    page.compare_panel.task_selected.emit(old.task_id)
    assert page.vm.compare_task_id == old.task_id
    assert page.compare_panel.chart.legend_names() == ["当前任务", "对比任务"]
    assert page.compare_panel.primary_rounds.texts()
    assert page.compare_panel.other_rounds.texts()

    page.summary_table.table.cellClicked.emit(1, 0)     # 切焦点：对比跟随
    assert "句柄数" in page.compare_panel.message_label.text()

    page.compare_panel.close_requested.emit()
    assert not page.compare_panel.isVisible() and page.vm.compare_task_id is None
    assert not page.toolbar.compare_button.isChecked()


# ================================================================ 沿用自原历史页/导出页的页面行为


def _raise_storage(*_args, **_kwargs):
    raise StorageError("读取数据点失败: database disk image is malformed")


def _empty_title(page):
    return page.empty_state_card.title_label.text()


def _option_index(page, task_id):
    return [o.task_id for o in page.task_selector.options()].index(task_id)


# ---------------------------------------------------------------- 任务下拉


def test_navigation_select_task_loads_once(qapp, db, manager, services):
    """主窗口跳转 = 页面显示（重载）+ select_task：同一轮里只发一批查询，且查的是目标任务。"""
    target = _seed(db, manager, name="old.exe", start=BASE - timedelta(days=1), samples=60)
    _seed(db, manager, name="new.exe", samples=60)
    runner, defer = SyncHistoryRunner(db), ManualDefer()
    page = make_analysis_page(db, manager, services, worker_runner=runner, defer=defer)
    try:
        page.show()
        page.select_task(target.task_id)
        defer.run()
        assert [r.task_id for r in runner.requests] == [target.task_id] * len(METRICS)
        assert sorted(r.metric for r in runner.requests) == sorted(METRICS)
        assert page.current_task_id == target.task_id
        assert page.vm.selection_is_auto is False
    finally:
        page.auto_refresh.stop()
        page.close()


def test_task_combo_text_tooltip_and_running_mark(make_page, db, manager, services):
    """下拉项：进程名 · PID · 开始时间 · 运行中时长 · 截断备注；完整备注在 tooltip；
    没有数据的任务不出现。"""
    long_note = "建模 200 次回归测试，连续运行过夜观察内存增长"
    task = _seed(db, manager, name="MPrintExp.exe", status="running", note=long_note,
                 metrics=[MEM], samples=30, markers=(), baseline=None)
    empty = MonitorTask(task_id=str(uuid.uuid4()), pid=1, process_name="nodata.exe",
                        metric_types=[MEM], interval=1.0, start_time=BASE, end_time=None,
                        status="stopped")
    db.save_task(empty)
    services.task_status.set_running(task.task_id)
    page = make_page()
    page.vm.reload_tasks()

    assert page.task_selector.count() == 1
    text = page.task_selector.item_text(0)
    assert "MPrintExp.exe" in text and "PID 4321" in text and "07-16 16:00" in text
    assert "运行中" in text
    assert "…" in text and long_note not in text
    assert long_note in page.task_selector.tooltip()


def test_select_missing_task_clears_without_fallback(make_page, db, manager, services):
    """导航到尚无数据的任务：清空选择并提示，不静默回落到首项；列表本身保持完整。"""
    first = _seed(db, manager, name="a.exe", start=BASE - timedelta(days=1), metrics=[MEM],
                  markers=(), baseline=None)
    second = _seed(db, manager, name="b.exe", metrics=[MEM], markers=(), baseline=None)
    empty = MonitorTask(task_id=str(uuid.uuid4()), pid=1, process_name="nodata.exe",
                        metric_types=[MEM], interval=1.0, start_time=BASE, end_time=None,
                        status="stopped")
    db.save_task(empty)
    page = make_page()
    page.select_task(first.task_id)
    page.select_task(second.task_id)
    assert "b.exe" in page.task_selector.combo.currentText()
    assert services.notifier.notices == []

    page.select_task(empty.task_id)
    assert page.current_task_id is None and page.current_metric is None
    assert page.task_selector.current_index() == -1
    assert page.task_selector.count() == 2
    assert _empty_title(page) == "请选择监控任务"
    hint, = services.notifier.messages()
    assert hint.level == "info" and "暂无采样数据" in hint.content


def test_running_task_duration_label_updates_in_place(make_page, db, manager, services):
    """页面开着时运行中任务的下拉时长随定时刷新原地更新；结束后改为最终时长且不再变化，
    不打断当前选择。"""
    task = _seed(db, manager, name="run.exe", status="running", metrics=[MEM], samples=3,
                 markers=(), baseline=None)
    other = _seed(db, manager, name="other.exe", start=BASE - timedelta(days=1), metrics=[MEM],
                  samples=3, markers=(), baseline=None)
    services.task_status.set_running(task.task_id)
    services.clock.set(BASE + timedelta(minutes=5))
    page = make_page()
    page.select_task(other.task_id)
    index = _option_index(page, task.task_id)
    assert "运行中 5分钟" in page.task_selector.item_text(index)

    services.clock.set(BASE + timedelta(minutes=15))
    page.auto_refresh.tick()
    assert "运行中 15分钟" in page.task_selector.item_text(index)

    db.update_task_status(task.task_id, "stopped", BASE + timedelta(minutes=20))
    services.task_status.set_running(task.task_id, False)
    services.clock.set(BASE + timedelta(hours=2))
    page.auto_refresh.tick()
    text = page.task_selector.item_text(index)
    assert "运行中" not in text and "20分钟" in text
    services.clock.set(BASE + timedelta(hours=5))
    page.auto_refresh.tick()
    assert page.task_selector.item_text(index) == text
    assert page.current_task_id == other.task_id


def test_running_state_follows_task_status_not_db_column(make_page, db, manager, services):
    """"运行中"以任务状态服务为准：库里仍标 running 但采集线程已不在的孤儿任务按已结束
    显示（时长退回最后一次采样）；库里已标 stopped 但仍在停止收尾的任务按运行中显示。"""
    orphan = _seed(db, manager, name="orphan.exe", status="running", metrics=[MEM], samples=3,
                   markers=(), baseline=None)
    stopping = _seed(db, manager, name="stopping.exe", start=BASE + timedelta(minutes=1),
                     metrics=[MEM], samples=3, markers=(), baseline=None)
    services.task_status.set_running(stopping.task_id)
    services.clock.set(BASE + timedelta(minutes=8))
    page = make_page()
    page.vm.reload_tasks()

    orphan_text = page.task_selector.item_text(_option_index(page, orphan.task_id))
    assert "运行中" not in orphan_text and orphan_text.endswith("2秒")
    assert "运行中 7分钟" in page.task_selector.item_text(_option_index(page, stopping.task_id))
    assert page.vm.can_delete is False          # 当前选中的是最新任务（仍在收尾）

    services.clock.advance(timedelta(hours=1))
    page.auto_refresh.tick()
    assert page.task_selector.item_text(_option_index(page, orphan.task_id)) == orphan_text


# ---------------------------------------------------------------- 加载态、失败与自动刷新


def test_busy_disables_refresh_and_shows_bar_after_delay(qapp, db, manager, services):
    task = _seed(db, manager, metrics=[MEM], samples=30, markers=(), baseline=None)
    runner = ManualHistoryRunner(db)
    page = make_analysis_page(db, manager, services, worker_runner=runner, defer=lambda fn: fn())
    page.resize(1000, 700)
    try:
        page.show()
        assert page.vm.is_busy
        assert not page.toolbar.refresh_button.isEnabled()
        assert not page.busy_bar.isVisible()          # 快速查询不闪进度条
        QTest.qWait(400)
        assert page.busy_bar.isVisible()

        runner.complete_all()
        assert page.current_task_id == task.task_id
        assert page.toolbar.refresh_button.isEnabled()
        assert not page.busy_bar.isVisible()
    finally:
        page.auto_refresh.stop()
        page.close()


def test_load_failure_shows_read_error_and_sticky_notice(loaded, db, services, monkeypatch):
    page, _task = loaded
    monkeypatch.setattr(db.points, 'fetch', _raise_storage)

    page.toolbar.refresh_button.click()

    assert page.content_stack.currentWidget() is page.empty_state_card
    assert _empty_title(page) == "读取数据失败"
    error, = services.notifier.messages('error')
    assert error.title == "读取数据失败" and "malformed" in error.content
    assert error.effective_duration == -1   # 错误常驻，由用户关闭


def test_task_list_failure_shows_read_error(make_page, db, manager, services, monkeypatch):
    _seed(db, manager, metrics=[MEM], samples=3, markers=(), baseline=None)
    page = make_page()
    monkeypatch.setattr(db.tasks, "list_with_data", _raise_storage)

    page.vm.reload_tasks()

    assert _empty_title(page) == "读取数据失败"
    assert services.notifier.last().level == "error"
    assert page.task_selector.count() == 0 and not page.task_selector.isEnabled()


def test_snapshot_updates_auto_refresh_interval(make_page, db, manager, monkeypatch):
    """数据量越大自动刷新越慢：30 万点时间隔 15 秒。"""
    task = _seed(db, manager, metrics=[MEM], samples=30, markers=(), baseline=None)
    real = db.analytics.aggregates
    monkeypatch.setattr(
        db.analytics, 'aggregates',
        lambda *a, **k: dataclasses.replace(real(*a, **k), count=300_000))
    page = make_page()
    page.select_task(task.task_id)
    assert page.auto_refresh.interval == 15000


def test_auto_refresh_skipped_while_viewing_chart_or_minimized(
        make_page, db, manager, services, runner, monkeypatch):
    """用户正在看图（悬停十字线）或主窗口最小化：本次定时刷新跳过，定时器继续。"""
    task = _seed(db, manager, status="running", metrics=[MEM], samples=30, markers=(),
                 baseline=None)
    services.task_status.set_running(task.task_id)
    page = make_page()
    page.show()
    page.select_task(task.task_id)

    def auto_requests():
        return [r for r in runner.requests if r.auto_refresh]

    chart = page.trend_panel.charts()[MEM]
    chart.crosshair_line.setVisible(True)
    page.auto_refresh.tick()
    assert auto_requests() == []
    assert page.auto_refresh.is_active
    chart.crosshair_line.setVisible(False)
    page.auto_refresh.tick()
    assert len(auto_requests()) == 1

    monkeypatch.setattr(page.window(), 'isMinimized', lambda: True)
    page.auto_refresh.tick()
    assert len(auto_requests()) == 1
    assert page.auto_refresh.is_active


# ---------------------------------------------------------------- 删除


def test_delete_confirm_texts_and_goes_through_manager(loaded, db, manager, services,
                                                       monkeypatch):
    page, task = loaded
    deleted = []
    real = manager.delete_task_data
    monkeypatch.setattr(manager, 'delete_task_data',
                        lambda tid: deleted.append(tid) or real(tid))
    services.dialogs.push('confirm', True)

    page.toolbar.delete_action.trigger()

    confirm = services.dialogs.last_call('confirm')
    assert confirm['parent'] is page.window()
    assert confirm['title'] == "确认删除"
    assert "demo.exe (PID: 4321)" in confirm['content']
    assert (confirm['confirm_text'], confirm['cancel_text']) == ("删除", "取消")
    assert deleted == [task.task_id]


def test_delete_cancelled_or_failed_keeps_task(loaded, db, manager, services, monkeypatch):
    page, task = loaded
    services.dialogs.push('confirm', False)
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: pytest.fail("取消后不应删除"))
    page.toolbar.delete_action.trigger()
    assert db.get_task(task.task_id) is not None
    assert services.notifier.notices == []

    services.dialogs.push('confirm', True)
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: False)
    page.toolbar.delete_action.trigger()
    error, = services.notifier.messages('error')
    assert error.title == "删除失败" and error.effective_duration == -1
    assert db.get_task(task.task_id) is not None


def test_delete_disabled_for_running_task(make_page, db, manager, services):
    task = _seed(db, manager, status="running", metrics=[MEM], samples=10, markers=(),
                 baseline=None)
    services.task_status.set_running(task.task_id)
    page = make_page()
    page.select_task(task.task_id)

    assert not page.toolbar.delete_action.isEnabled()
    assert page.toolbar.delete_action.toolTip() == "任务正在运行中，请先停止任务再删除"
    page.toolbar.delete_action.trigger()   # 禁用时 trigger 不得转发删除
    assert services.dialogs.calls == []


def test_delete_confirm_focus_on_cancel_and_enter_does_not_delete(qapp, db, manager,
                                                                   monkeypatch):
    """真实确认框（生产 QtDialogs）：按钮"删除/取消"，焦点在"取消"，回车不会删除。"""
    task = _seed(db, manager, metrics=[MEM], samples=10, markers=(), baseline=None)
    seen = {}

    def _probe(box):
        box.show()
        QTest.qWait(300)  # 等淡入动画结束，避免动画回调落到后续用例里
        seen.update(yes=box.yesButton.text(), cancel=box.cancelButton.text(),
                    focus=box.focusWidget(), cancel_button=box.cancelButton)
        accepted = []
        box.yesSignal.connect(lambda: accepted.append(True))
        QTest.keyClick(box.focusWidget(), Qt.Key_Return)
        for _ in range(50):  # 等淡出动画结束、对话框真正关闭
            if not box.isVisible():
                break
            QTest.qWait(20)
        return bool(accepted)

    page = make_analysis_page(db, manager, make_services(dialogs=QtDialogs(run_dialog=_probe)),
                              worker_runner=SyncHistoryRunner(db), defer=lambda fn: fn())
    deleted = []
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: deleted.append(tid) or True)
    try:
        page.show()
        page.select_task(task.task_id)
        page.toolbar.delete_action.trigger()
        assert (seen['yes'], seen['cancel']) == ("删除", "取消")
        assert seen['focus'] is seen['cancel_button']
        assert deleted == []
    finally:
        page.auto_refresh.stop()
        page.close()


# ---------------------------------------------------------------- 导出：登记、互斥、失败与目录


def _export_report_to(page, services, folder, name="demo_报告.png"):
    services.dialogs.push('ask_save_path', str(folder / name))
    page.toolbar.export_requested.emit(EXPORT_REPORT)


def test_export_registers_thread_and_blocks_maintenance_until_finished(
        loaded, manager, services, tmp_path):
    """开始导出即登记（线程登记表 + manager 导出计数，此时不能开始清理数据库），
    线程结束后两处都注销。"""
    page, _task = loaded
    _export_report_to(page, services, tmp_path)
    worker = page.vm.export_worker
    assert worker is not None
    assert [(e.name, e.thread, e.cancel, e.timeout_ms) for e in services.threads.entries()] == [
        ("导出报告", worker, worker.cancel, 2000)]
    assert manager.has_running_exports() is True
    assert manager.begin_maintenance() is False

    assert process_events_until(lambda: page.vm.export_worker is None, timeout=20)
    assert services.threads.entries() == []
    assert manager.has_running_exports() is False
    assert manager.begin_maintenance() is True
    manager.end_maintenance()


def test_export_refused_during_maintenance_without_dialog(loaded, manager, services):
    """清理数据库进行中：不弹保存对话框、不启动导出线程，提示稍后再导出。"""
    page, _task = loaded
    assert manager.begin_maintenance() is True
    try:
        page.toolbar.export_requested.emit(EXPORT_REPORT)
        assert services.dialogs.calls_of('ask_save_path') == []
        assert page.vm.export_worker is None
        assert manager.has_running_exports() is False
        notice = services.notifier.last()
        assert (notice.level, notice.content) == ("warning", MAINTENANCE[1])
    finally:
        manager.end_maintenance()


class _HookedDialogs(ScriptedDialogs):
    """确认框弹出期间可插入一个动作（模拟"确认框开着时别处开始了清理数据库"）。"""

    def __init__(self):
        super().__init__()
        self.on_confirm = None

    def confirm(self, parent, title, content, confirm_text, cancel_text="取消"):
        result = super().confirm(parent, title, content, confirm_text, cancel_text)
        if self.on_confirm is not None:
            self.on_confirm()
        return result


def test_export_refused_if_maintenance_starts_during_overwrite_confirm(
        qapp, db, manager, tmp_path):
    """覆盖确认框期间清理已开始：以 manager 登记结果为准，不启动导出线程。"""
    dialogs = _HookedDialogs()
    services = make_services(clock=FakeClock(BASE + timedelta(hours=2)), dialogs=dialogs)
    task = _seed(db, manager, metrics=[MEM], samples=60, markers=(), baseline=None)
    page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                              defer=lambda fn: fn())
    try:
        page.select_task(task.task_id)
        (tmp_path / "demo.csv").write_text("原有内容", encoding="utf-8")
        dialogs.push('ask_save_path', str(tmp_path / "demo.csv"))
        dialogs.push('confirm', True)
        dialogs.on_confirm = manager.begin_maintenance

        page.toolbar.export_requested.emit(EXPORT_CSV)

        assert dialogs.last_call('confirm')['title'] == "文件已存在"
        assert manager.is_maintenance_running() is True
        assert page.vm.export_worker is None
        assert manager.has_running_exports() is False
        assert services.notifier.last().content == MAINTENANCE[1]
        assert page.toolbar.export_button.isEnabled()
        assert (tmp_path / "demo.csv").read_text(encoding="utf-8") == "原有内容"
    finally:
        manager.end_maintenance()
        page.auto_refresh.stop()
        page.close()


def test_export_failure_notice_is_sticky_and_clears_status(loaded, services):
    page, _task = loaded
    page.export_flow.status_changed.emit("正在导出…")
    page.vm.export_failed.emit("导出失败：磁盘已满")
    error = services.notifier.last()
    assert (error.level, error.title, error.content) == ("error", "导出失败", "导出失败：磁盘已满")
    assert error.effective_duration == -1
    assert error.parent is page
    assert not page.export_status_label.isVisible()


def test_save_directory_is_remembered_across_tasks_and_reload(loaded, db, manager, services,
                                                             tmp_path):
    """导出成功后记住所选目录：换任务、重新载入列表都不重置；下次默认路径 = 该目录 +
    新任务的文件名。取消保存对话框不改变记住的目录。"""
    page, _task = loaded
    out = tmp_path / "reports"
    out.mkdir()
    _export_report_to(page, services, out)
    assert process_events_until(lambda: page.vm.export_worker is None, timeout=20)
    assert page.export_flow.last_dir == str(out)

    other = _seed(db, manager, name="other.exe", start=BASE - timedelta(days=1),
                  metrics=[MEM], markers=(), baseline=None)
    page.vm.reload_tasks()
    page.select_task(other.task_id)
    services.dialogs.push('ask_save_path', None)          # 用户取消
    page.toolbar.export_requested.emit(EXPORT_CSV)
    call = services.dialogs.last_call('ask_save_path')
    assert os.path.dirname(call['default_path']) == str(out)
    assert os.path.basename(call['default_path']).startswith("other")
    assert page.export_flow.last_dir == str(out)


def test_open_folder_failure_is_silent(loaded, services, tmp_path):
    """导出成功提示上的"打开文件夹"：打开失败不抛出、不再弹提示。"""
    page, _task = loaded
    _export_report_to(page, services, tmp_path)
    assert process_events_until(
        lambda: services.threads.entries() == [] and services.notifier.messages('success'),
        timeout=20)
    services.dialogs.push('open_folder', OSError("目录已被删除"))
    page.export_flow.open_folder_button.click()
    assert len(services.dialogs.calls_of('open_folder')) == 1
    assert services.notifier.levels() == ["success"]


# ---------------------------------------------------------------- 结构约束


def test_page_holds_no_query_logic():
    """页面只做组装：不直接查库、不屏蔽信号、不持有选择状态。"""
    import inspect
    import ui.pages.analysis_page as module
    source = inspect.getsource(module)
    for forbidden in ("blockSignals", "_selection_is_auto", "db.tasks", "db.points",
                      "db.analytics", "db.markers", "get_task(", "run_history_query"):
        assert forbidden not in source, forbidden
