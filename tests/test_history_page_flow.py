"""
历史页组装：控件信号 → 视图模型命令的连线，以及视图模型信号驱动的关键可见状态
（快照渲染、空状态、加载态、错误提示、任务下拉文案、删除确认、图表保存/复制、
小窗口滚动）。选择语义、锚点、过期丢弃等业务规则见 tests/test_history_vm.py。

视图模型注入同步查询执行器（SyncHistoryRunner）与立即执行的 defer，用户操作后
结果当场可见；真实后台线程见 tests/test_history_page_async.py。
"""
import dataclasses
import uuid
from datetime import datetime, timedelta

import pyqtgraph as pg
import pytest
from pyqtgraph import exporters as pg_exporters
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from core.monitor_manager import MonitorManager
from data.errors import StorageError
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_history_page, make_services
from tests.helpers import ManualDefer, ManualHistoryRunner, SyncHistoryRunner
from ui.services import QtDialogs
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 20, 0, 0)
MEM = MetricType.MEMORY_PRIVATE


def _seed_task(db, samples=10, note="", status="stopped", markers=(), name="demo.exe",
               start=BASE, metric=MEM, metrics=None):
    metrics = list(metrics or [metric])
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name, metric_types=metrics,
        interval=1.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(seconds=samples),
        status=status, note=note)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i), m)
        for i in range(samples) for m in metrics])
    for offset, label in markers:
        db.add_marker(task.task_id, start + timedelta(seconds=offset), label)
    return task


def run_now(fn):
    """defer 替身：加载请求立即发出（不等下一轮事件循环）。"""
    fn()


def _raise_storage(*a, **k):
    raise StorageError("读取数据点失败: database disk image is malformed")


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    """全替身服务：时钟定在样本数据之后，任务默认都不在运行。"""
    return make_services(clock=FakeClock(BASE + timedelta(hours=1)))


@pytest.fixture
def runner(db):
    return SyncHistoryRunner(db)


@pytest.fixture
def page(qapp, db, manager, services, runner):
    widget = make_history_page(db, manager, services, worker_runner=runner, defer=run_now)
    widget.resize(1200, 800)
    yield widget
    widget.auto_refresh.stop()
    widget.close()


def _empty_title(page):
    return page.empty_state_card.title_label.text()


def _marker_lines(page):
    return [item for item in page.chart_view.chart_widget.getPlotItem().items
            if isinstance(item, pg.InfiniteLine) and item is not page.chart_view.crosshair_line]


# ---------- 初始状态与显示/隐藏 ----------

def test_initial_state_is_empty_with_all_range(page):
    assert page.vm.range_key == page.filter_bar.current_range() == 'all'
    assert page.content_stack.currentWidget() is page.empty_state_card
    assert _empty_title(page) == "暂无历史数据"
    assert page.current_task_id is None and page.current_metric is None


def test_show_reloads_and_drives_auto_refresh(page, db):
    task = _seed_task(db, samples=5)
    assert not page.auto_refresh.is_active

    page.show()
    assert page.current_task_id == task.task_id
    assert page.task_selector.current_id() == task.task_id
    assert page.content_stack.currentWidget() is page.analysis_page
    assert page.auto_refresh.is_active

    page.hide()
    assert not page.auto_refresh.is_active


# ---------- 控件信号 → 命令 ----------

def test_user_picks_task_in_selector(page, db, runner):
    old = _seed_task(db, samples=3, name="old.exe", start=BASE)
    _seed_task(db, samples=3, name="new.exe", start=BASE + timedelta(hours=1))
    page.vm.reload_tasks()
    assert page.vm.selection_is_auto is True

    page.task_selector.combo.setCurrentIndex(1)   # 等同用户在下拉里点选

    assert page.current_task_id == old.task_id
    assert page.vm.selection_is_auto is False
    assert runner.requests[-1].task_id == old.task_id
    assert "old.exe" in page.task_selector.combo.currentText()


def test_metric_combo_selects_metric(page, db, runner):
    task = _seed_task(db, samples=3, metrics=[MEM, MetricType.NUM_THREADS])
    page.select_task(task.task_id)
    assert page.filter_bar.current_metric() == MEM

    page.filter_bar.metric_combo.setCurrentIndex(1)

    assert page.current_metric == MetricType.NUM_THREADS
    assert runner.requests[-1].metric == MetricType.NUM_THREADS
    assert page.chart_view.metric_type == MetricType.NUM_THREADS


def test_range_segment_sets_range(page, db, runner):
    task = _seed_task(db, samples=3)
    page.select_task(task.task_id)

    page.filter_bar.range_segmented.setCurrentItem('1h')

    assert page.vm.range_key == '1h'
    assert runner.requests[-1].range_key == '1h'


def test_refresh_button_reloads_task_list(page, db):
    _seed_task(db, samples=3)
    page.vm.reload_tasks()
    assert page.task_selector.count() == 1

    newer = _seed_task(db, samples=3, name="newer.exe", start=BASE + timedelta(hours=1))
    page.filter_bar.refresh_button.click()

    assert page.task_selector.count() == 2
    assert page.current_task_id == newer.task_id   # 自动选中跟随最新


def test_view_switch_and_raw_values(qapp, page):
    assert page.view_stack.currentWidget() is page.chart_view
    page.filter_bar.view_segmented.setCurrentItem('detail')
    assert page.view_stack.currentWidget() is page.sample_table
    page.filter_bar.view_segmented.setCurrentItem('trend')
    assert page.view_stack.currentWidget() is page.chart_view

    assert not page.sample_table.raw_values_visible()
    page.filter_bar.raw_value_action.setChecked(True)
    assert page.sample_table.raw_values_visible()


def test_navigation_select_task_loads_once(qapp, db, manager, services):
    """主窗口跳转 = 页面显示（重载）+ select_task：同一轮里只发一次查询，且查的是目标任务。"""
    target = _seed_task(db, samples=3, name="old.exe", start=BASE)
    _seed_task(db, samples=3, name="new.exe", start=BASE + timedelta(hours=1))
    runner, defer = SyncHistoryRunner(db), ManualDefer()
    page = make_history_page(db, manager, services, worker_runner=runner, defer=defer)
    try:
        page.show()
        page.select_task(target.task_id)
        defer.run()
        assert [r.task_id for r in runner.requests] == [target.task_id]
        assert page.current_task_id == target.task_id
        assert page.vm.selection_is_auto is False
    finally:
        page.auto_refresh.stop()
        page.close()


# ---------- 快照渲染 ----------

def test_snapshot_renders_stats_table_chart_and_markers(page, db):
    """两个用户打点围出一轮：增长格显示"每轮增长"（打点后 5 点中位数 104 → 107.5 MB）；
    只有 9 秒数据，判定为数据不足；打点竖线横排编号。"""
    task = _seed_task(db, samples=10, markers=[(2, "第1轮"), (6, "第2轮")])
    page.select_task(task.task_id)

    texts = page.stats_strip.texts()
    assert texts == {"开始值": "100 MB", "当前值": "109 MB", "变化量": "+9 MB (+9.0%)",
                     "每轮增长": "+3.5 MB", "最小": "100 MB", "最大": "109 MB"}
    assert page.stats_strip.pill_text() == "数据不足"

    rows = page.sample_table.row_texts()
    assert len(rows) == 10
    assert rows[0][0] == (BASE + timedelta(seconds=9)).strftime('%m-%d %H:%M:%S')
    assert rows[0][1] == "109 MB" and rows[0][2] == "+1 MB" and rows[0][3] == "--"
    assert page.chart_view.axis_title() == "私有字节（MB）"
    assert len(page.chart_view.x_values) == 10
    assert sorted(line.value() for line in _marker_lines(page)) == [
        (BASE + timedelta(seconds=2)).timestamp(),
        (BASE + timedelta(seconds=6)).timestamp()]
    assert [badge for _k, badge, _line in page.chart_view.marker_items()] == ["①", "②"]


def test_short_data_rate_cell_is_dimmed(page, db):
    task = _seed_task(db, samples=10)
    page.select_task(task.task_id)
    assert page.stats_strip.texts()["每分钟增长"] == "数据不足（已采 9秒）"
    assert page.stats_strip.dimmed_titles() == ["每分钟增长"]


def test_baseline_enables_range_and_drives_stats(page, db):
    """有基线：筛选条"自基线"可选、统计条首格为基线值、明细"较基线"有值；
    选"自基线"后图表与明细从基线起；基线被删后回到"全部"。"""
    task = _seed_task(db, samples=10)
    marker_id = db.markers.add(task.task_id, BASE + timedelta(seconds=3), "基线", 'baseline')
    page.select_task(task.task_id)

    assert page.filter_bar.baseline_available() is True
    texts = page.stats_strip.texts()
    assert texts["基线值"] == "105 MB" and texts["变化量"] == "+4 MB (+3.8%)"
    assert page.sample_table.row_texts()[0][3] == "+4 MB"
    assert [badge for _k, badge, _line in page.chart_view.marker_items()] == ["基"]

    page.filter_bar.range_segmented.setCurrentItem('baseline')
    assert page.vm.range_key == 'baseline'
    assert len(page.sample_table.row_texts()) == 7
    assert page.chart_view.x_values[0] == (BASE + timedelta(seconds=3)).timestamp()

    db.markers.delete(marker_id)
    page.filter_bar.refresh_button.click()
    assert page.vm.range_key == page.filter_bar.current_range() == 'all'
    assert page.filter_bar.baseline_available() is False
    assert page.stats_strip.texts()["开始值"] == "100 MB"


def test_selecting_task_without_baseline_disables_range(page, db):
    with_baseline = _seed_task(db, samples=5, name="a.exe")
    db.markers.add(with_baseline.task_id, BASE, "基线", 'baseline')
    plain = _seed_task(db, samples=5, name="b.exe", start=BASE - timedelta(days=1))
    page.select_task(with_baseline.task_id)
    assert page.filter_bar.baseline_available() is True
    page.select_task(plain.task_id)
    assert page.filter_bar.baseline_available() is False


def test_cumulative_metric_renders_rate_chart_with_level_table(page, db):
    """累计类：图表画每秒速率，统计条与明细仍是累计值（单位按累计值选）"""
    task = _seed_task(db, samples=40, metric=MetricType.IO_READ_BYTES)
    page.select_task(task.task_id)
    assert page.chart_view.is_rate is True
    assert page.chart_view.axis_title() == "每秒IO读取字节（MB）"
    texts = page.stats_strip.texts()
    assert texts["当前值"] == "139 MB" and texts["平均速率"] == "1 MB/秒"
    assert page.sample_table.row_texts()[0][1] == "139 MB"


def test_table_hint_shows_shown_over_total(qapp, db, manager, services):
    task = _seed_task(db, samples=10)
    page = make_history_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                             defer=run_now, table_limit=4)
    try:
        page.select_task(task.task_id)
        assert page.sample_table.table_hint_label.text() == (
            "共 10 条（倒序） · 仅显示最近 4 条")
        assert page.sample_table.data_table.rowCount() == 4
    finally:
        page.close()


def test_auto_refresh_keeps_view_user_action_resets_it(page, db, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    resets = []
    monkeypatch.setattr(page.chart_view, 'reset_view', lambda: resets.append(1))

    services.task_status.set_running(task.task_id)
    page.auto_refresh.tick()
    assert resets == []

    page.filter_bar.refresh_button.click()
    assert resets == [1]


def test_snapshot_updates_auto_refresh_interval(page, db, monkeypatch):
    task = _seed_task(db, samples=5)
    real = db.analytics.aggregates
    monkeypatch.setattr(
        db.analytics, 'aggregates',
        lambda *a, **k: dataclasses.replace(real(*a, **k), count=300_000))
    page.select_task(task.task_id)
    assert page.auto_refresh.interval == 15000


def test_empty_state_clears_widgets(page, db):
    task = _seed_task(db, samples=3)
    page.select_task(task.task_id)
    assert page.sample_table.row_texts()

    page.vm.empty_state.emit("所选时间范围内暂无数据", "尝试切换到「全部」查看完整历史")

    assert page.content_stack.currentWidget() is page.empty_state_card
    assert _empty_title(page) == "所选时间范围内暂无数据"
    assert page.sample_table.row_texts() == []
    assert set(page.stats_strip.texts().values()) == {"--"}
    assert not page.chart_view.has_data()


# ---------- 加载态与错误 ----------

def test_busy_disables_refresh_and_shows_bar_after_delay(qapp, db, manager, services):
    task = _seed_task(db, samples=3)
    runner = ManualHistoryRunner(db)
    page = make_history_page(db, manager, services, worker_runner=runner, defer=run_now)
    page.resize(1000, 700)
    try:
        page.show()
        assert page.vm.is_busy
        assert not page.filter_bar.refresh_button.isEnabled()
        assert not page.busy_bar.isVisible()          # 快速查询不闪进度条
        QTest.qWait(400)
        assert page.busy_bar.isVisible()

        runner.complete_all()
        assert page.current_task_id == task.task_id
        assert page.filter_bar.refresh_button.isEnabled()
        assert not page.busy_bar.isVisible()
    finally:
        page.auto_refresh.stop()
        page.close()


def test_load_failure_shows_read_error_and_sticky_notice(page, db, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    monkeypatch.setattr(db.points, 'fetch', _raise_storage)

    page.filter_bar.refresh_button.click()

    assert page.content_stack.currentWidget() is page.empty_state_card
    assert _empty_title(page) == "读取数据失败"
    error, = services.notifier.messages('error')
    assert error.title == "读取数据失败" and "malformed" in error.content
    assert error.effective_duration == -1   # 错误常驻，由用户关闭


def test_task_list_failure_shows_read_error(page, db, services, monkeypatch):
    _seed_task(db, samples=3)
    monkeypatch.setattr(db.tasks, "list_with_data", _raise_storage)

    page.vm.reload_tasks()

    assert _empty_title(page) == "读取数据失败"
    assert services.notifier.last().level == "error"
    assert page.task_selector.count() == 0 and not page.task_selector.isEnabled()


# ---------- 任务下拉 ----------

def test_task_combo_text_tooltip_and_running_mark(page, db, services):
    long_note = "建模 200 次回归测试，连续运行过夜观察内存增长"
    task = _seed_task(db, samples=30, note=long_note, name="MPrintExp.exe", status="running")
    _seed_task(db, samples=0, name="nodata.exe")   # 没有数据的任务不出现
    services.task_status.set_running(task.task_id)

    page.vm.reload_tasks()

    assert page.task_selector.count() == 1
    text = page.task_selector.item_text(0)
    assert "MPrintExp.exe" in text and "PID 4321" in text and "07-16 20:00" in text
    assert "运行中" in text
    assert "…" in text and long_note not in text
    assert long_note in page.task_selector.tooltip()


def test_select_task_picks_given_task_and_missing_task_clears(page, db, services):
    first = _seed_task(db, samples=3, name="a.exe", start=BASE)
    second = _seed_task(db, samples=3, name="b.exe", start=BASE + timedelta(hours=1))
    nodata = _seed_task(db, samples=0, name="nodata.exe")

    page.select_task(first.task_id)
    assert page.current_task_id == first.task_id
    page.select_task(second.task_id)
    assert "b.exe" in page.task_selector.combo.currentText()
    assert services.notifier.notices == []

    page.select_task(nodata.task_id)
    # 不在列表中（尚无数据）：清空选择、提示，不静默回落到首项；列表本身保持完整
    assert page.current_task_id is None and page.current_metric is None
    assert page.task_selector.current_index() == -1
    assert page.task_selector.count() == 2
    assert _empty_title(page) == "请选择监控任务"
    hint, = services.notifier.messages()
    assert hint.level == "info" and "暂无采样数据" in hint.content


def test_running_task_duration_label_updates_in_place(page, db, services):
    """页面开着时运行中任务的时长随定时刷新原地更新；结束后改为最终时长，不打断当前选择。"""
    task = _seed_task(db, samples=3, status="running")
    other = _seed_task(db, samples=3, start=BASE - timedelta(days=1))
    services.task_status.set_running(task.task_id)
    services.clock.set(BASE + timedelta(minutes=5))
    page.select_task(other.task_id)
    index = page.task_selector.options().index(
        next(o for o in page.task_selector.options() if o.task_id == task.task_id))
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
    assert page.current_task_id == other.task_id


# ---------- 删除 ----------

def test_delete_goes_through_manager(page, db, manager, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    services.dialogs.push('confirm', True)
    deleted = []
    real = manager.delete_task_data
    monkeypatch.setattr(
        manager, 'delete_task_data', lambda tid: deleted.append(tid) or real(tid))

    page.filter_bar.delete_action.trigger()

    confirm = services.dialogs.last_call('confirm')
    assert confirm['parent'] is page.window()
    assert confirm['title'] == "确认删除"
    assert "demo.exe (PID: 4321)" in confirm['content']
    assert (confirm['confirm_text'], confirm['cancel_text']) == ("删除", "取消")
    assert deleted == [task.task_id]
    assert db.get_task(task.task_id) is None
    assert page.current_task_id is None and page.task_selector.count() == 0
    assert services.notifier.last().level == "success"


def test_delete_cancelled_keeps_task(page, db, manager, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    services.dialogs.push('confirm', False)
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: pytest.fail("取消后不应删除"))

    page.filter_bar.delete_action.trigger()

    assert db.get_task(task.task_id) is not None
    assert services.notifier.notices == []


def test_delete_failure_keeps_task(page, db, manager, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    services.dialogs.push('confirm', True)
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: False)

    page.filter_bar.delete_action.trigger()

    error, = services.notifier.messages('error')
    assert error.effective_duration == -1
    assert db.get_task(task.task_id) is not None


def test_delete_disabled_for_running_task(page, db, services):
    task = _seed_task(db, samples=5, status="running")
    services.task_status.set_running(task.task_id)
    page.select_task(task.task_id)

    assert not page.filter_bar.delete_action.isEnabled()
    assert page.filter_bar.delete_action.toolTip() == "任务正在运行中，请先停止任务再删除"
    page.filter_bar.delete_action.trigger()   # 禁用时 trigger 不得转发删除
    assert services.dialogs.calls == []


def test_delete_confirm_is_chinese_and_enter_does_not_delete(qapp, db, manager, monkeypatch):
    """真实确认框（生产 QtDialogs）：按钮"删除/取消"，焦点在"取消"，回车不会删除"""
    task = _seed_task(db, samples=5)
    seen = {}

    def _probe(box):
        box.show()
        QTest.qWait(300)  # 等淡入动画结束，避免动画回调落到后续用例里
        seen.update(yes=box.yesButton.text(), cancel=box.cancelButton.text(),
                    focus=box.focusWidget())
        accepted = []
        box.yesSignal.connect(lambda: accepted.append(True))
        QTest.keyClick(box.focusWidget(), Qt.Key_Return)
        for _ in range(50):  # 等淡出动画结束、对话框真正关闭
            if not box.isVisible():
                break
            QTest.qWait(20)
        seen['cancel_button'] = box.cancelButton
        return bool(accepted)

    page = make_history_page(db, manager, make_services(dialogs=QtDialogs(run_dialog=_probe)),
                             worker_runner=SyncHistoryRunner(db), defer=run_now)
    deleted = []
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: deleted.append(tid) or True)
    try:
        page.show()
        page.select_task(task.task_id)

        page.filter_bar.delete_action.trigger()

        assert (seen['yes'], seen['cancel']) == ("删除", "取消")
        assert seen['focus'] is seen['cancel_button']
        assert deleted == []
    finally:
        page.auto_refresh.stop()
        page.close()


# ---------- 图表保存 / 复制 ----------

def test_save_chart_default_name_uses_clock_and_cancel_is_silent(page, db, services):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    services.clock.set(datetime(2026, 8, 1, 9, 30, 15))
    services.dialogs.push('ask_save_path', None)

    page.chart_view.save_png_button.click()

    call = services.dialogs.last_call('ask_save_path')
    assert call['parent'] is page
    assert call['default_path'] == "demo_私有字节_20260801093015.png"
    assert call['name_filter'] == "PNG 图片 (*.png)"
    assert services.notifier.notices == []


def test_save_chart_success_writes_png(page, db, services, tmp_path):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    target = tmp_path / "chart.png"
    services.dialogs.push('ask_save_path', str(target))

    page.chart_view.save_png_button.click()

    assert target.exists() and target.stat().st_size > 0
    notice = services.notifier.last()
    assert notice.level == "success" and str(target) in notice.content


def test_save_chart_failure_error_stays(page, db, services, monkeypatch, tmp_path):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    services.dialogs.push('ask_save_path', str(tmp_path / "chart.png"))
    monkeypatch.setattr(pg_exporters, 'ImageExporter',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("导出失败")))

    page.chart_view.save_png_button.click()

    error, = services.notifier.messages('error')
    assert error.effective_duration == -1


def test_copy_chart_puts_pixmap_on_clipboard(page, db, services):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)

    page.chart_view.copy_chart_button.click()

    assert not services.dialogs.last_call('copy_pixmap')['pixmap'].isNull()
    assert services.notifier.last().level == "success"


def test_copy_chart_failure_error_stays(page, db, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    monkeypatch.setattr(page.chart_view, 'grab_pixmap',
                        lambda: (_ for _ in ()).throw(RuntimeError("截图失败")))

    page.chart_view.copy_chart_button.click()

    error, = services.notifier.messages('error')
    assert error.effective_duration == -1


def test_chart_actions_without_data_only_warn(page, services):
    page.chart_view.save_png_button.click()
    page.chart_view.copy_chart_button.click()

    assert services.notifier.levels() == ["warning", "warning"]
    assert services.dialogs.calls == []


# ---------- 版式 ----------

def test_small_window_scrolls_instead_of_clipping_chart(qapp, db, manager):
    """默认窗口尺寸下图表不能被压出卡片：内容放不下时页面应出现滚动条。"""
    _seed_task(db, samples=20)
    page = make_history_page(db, manager, make_services(),
                             worker_runner=SyncHistoryRunner(db), defer=run_now)
    page.resize(930, 640)
    page.show()
    try:
        qapp.processEvents()
        assert page.content_stack.currentWidget() is page.analysis_page
        chart = page.chart_view.chart_widget
        assert chart.geometry().bottom() <= chart.parentWidget().rect().bottom()
        if page.widget().minimumSizeHint().height() > page.viewport().height():
            assert page.verticalScrollBar().maximum() > 0
    finally:
        page.auto_refresh.stop()
        page.close()


@pytest.mark.parametrize("width, compact, columns", [
    (1400, False, 6), (900, False, 3), (700, True, 3), (500, True, 2)])
def test_resize_updates_filter_and_stats_layout(qapp, page, width, compact, columns):
    """页面按视口宽度换算卡片内容宽度：筛选条 < 720 换行；统计条 ≥1100 六列、≥560 三列。"""
    page.resize(width, 700)
    page.show()
    qapp.processEvents()
    assert page.filter_bar.is_compact() is compact
    assert page.stats_strip.columns() == columns
    layout = page.filter_bar.select_layout
    row, column, _rs, column_span = layout.getItemPosition(
        layout.indexOf(page.filter_bar.range_segmented))
    assert (row, column, column_span) == ((2, 1, 3) if compact else (1, 3, 1))


def test_page_holds_no_query_logic():
    """页面只做组装：不直接查库、不屏蔽信号、不持有选择状态。"""
    import inspect
    import ui.pages.history_page as module
    source = inspect.getsource(module)
    for forbidden in ("blockSignals", "_selection_is_auto", "db.tasks", "db.points",
                      "db.analytics", "db.markers", "get_task(", "run_history_query"):
        assert forbidden not in source, forbidden
