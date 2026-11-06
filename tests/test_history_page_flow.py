"""
历史页：默认范围与空状态文案、缓存失效、运行中任务自动刷新、自适应范围、统计条、
打点竖线、表格提示、任务下拉文案、经 manager 删除、删除确认框、错误提示停留时长。
"""
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pyqtgraph as pg
import pytest
from pyqtgraph import exporters as pg_exporters
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from core.contracts import TaskStatus
from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_history_page, make_services
from ui.pages.history_page import AUTO_REFRESH_INTERVAL_MS
from ui.services import ManagerTaskStatus, QtDialogs
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 20, 0, 0)


def _seed_task(db, samples=10, note="", status="stopped", markers=(), name="demo.exe",
               start=BASE, metric=MetricType.MEMORY_PRIVATE):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name, metric_types=[metric],
        interval=1.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(seconds=samples),
        status=status, note=note)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i), metric)
        for i in range(samples)])
    for offset, label in markers:
        db.add_marker(task.task_id, start + timedelta(seconds=offset), label)
    return task


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    """全替身服务：时钟定在样本数据之后，任务默认都不在运行。"""
    return make_services(clock=FakeClock(BASE + timedelta(hours=1)))


@pytest.fixture
def page(qapp, db, manager, services):
    widget = make_history_page(db, manager, services)
    widget.resize(1200, 800)
    yield widget
    widget._auto_refresh_timer.stop()
    widget.close()


# ---------- 默认范围与空状态 ----------

def test_default_range_is_all_and_initial_empty_state(page):
    assert page.current_range_key == 'all'
    assert page.empty_title_label.text() == "暂无历史数据"


def test_empty_state_wording_depends_on_selected_range(page, db, monkeypatch):
    task = _seed_task(db, samples=3)
    page.select_task(task.task_id)
    monkeypatch.setattr(db.points, 'fetch', lambda *a, **k: [])

    page.current_range_key = 'all'
    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE)
    assert page.empty_title_label.text() == "该任务暂无数据"

    page.current_range_key = '1h'
    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE)
    assert page.empty_title_label.text() == "所选时间范围内暂无数据"


def test_refresh_invalidates_last_timestamp_cache(page, db):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    page.current_range_key = '1h'

    first_anchor = page._get_cached_last_dt(task.task_id)
    assert first_anchor == BASE + timedelta(seconds=4)

    # 新数据写入后，不刷新时锚点仍是缓存值；点刷新后锚点跟上
    db.save_data_points([DataPoint(
        task.task_id, BASE + timedelta(seconds=60), 1.0, MetricType.MEMORY_PRIVATE)])
    assert page._get_cached_last_dt(task.task_id) == first_anchor

    page._on_refresh_clicked()

    assert page._get_cached_last_dt(task.task_id) == BASE + timedelta(seconds=60)


def test_cache_is_reused_between_range_switches(page, db, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    calls = []
    real = db.analytics.last_point_timestamp
    monkeypatch.setattr(
        db.analytics, 'last_point_timestamp', lambda tid: calls.append(tid) or real(tid))

    page._on_time_range_changed('1h')
    page._on_time_range_changed('6h')

    assert len(calls) <= 1


# ---------- 自动刷新 ----------

def test_auto_refresh_timer_follows_visibility(page):
    assert AUTO_REFRESH_INTERVAL_MS == 5000
    assert page._auto_refresh_timer.interval() == 5000
    assert not page._auto_refresh_timer.isActive()

    page.show()
    assert page._auto_refresh_timer.isActive()
    page.hide()
    assert not page._auto_refresh_timer.isActive()


def test_is_task_running_reads_manager_snapshot(qapp, db, manager, monkeypatch):
    """生产装配（ManagerTaskStatus）下"是否运行中"只经 manager 的只读快照判断，
    不取任务线程对象；停止收尾中仍算运行（收尾写完结束时间后才转已停止）"""
    monkeypatch.setattr(manager, 'get_task', lambda tid: pytest.fail("界面不应取任务线程"))
    snapshots = {
        "run": SimpleNamespace(status=TaskStatus.RUNNING, is_stopping=False),
        "stopping": SimpleNamespace(status=TaskStatus.RUNNING, is_stopping=True),
        "pending": SimpleNamespace(status=TaskStatus.PENDING, is_stopping=False),
        "done": SimpleNamespace(status=TaskStatus.STOPPED, is_stopping=False),
    }
    monkeypatch.setattr(manager, 'get_task_info', snapshots.get)
    page = make_history_page(
        db, manager, make_services(task_status=ManagerTaskStatus(manager)))
    try:
        assert page._is_task_running("run") is True
        assert page._is_task_running("stopping") is True
        assert page._is_task_running("pending") is False
        assert page._is_task_running("done") is False
        assert page._is_task_running("unknown") is False
    finally:
        page._auto_refresh_timer.stop()
        page.close()


def test_auto_refresh_reloads_only_when_selected_task_is_running(
        page, db, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.show()
    page.select_task(task.task_id)

    loads = []
    monkeypatch.setattr(
        page, '_start_auto_refresh_worker', lambda *a, **k: loads.append((a, k)))

    # 任务未在运行：不刷新
    services.task_status.set_running(task.task_id, False)
    page._on_auto_refresh()
    assert loads == []

    # 运行中：交给后台 worker 刷新（keep_view 语义在应用结果时保证）
    services.task_status.set_running(task.task_id, True)
    page._on_auto_refresh()
    assert len(loads) == 1

    # 刚结束：整体重载一次任务列表，之后不再重复
    reloads = []
    monkeypatch.setattr(page, '_load_tasks', lambda: reloads.append(1))
    services.task_status.set_running(task.task_id, False)
    page._on_auto_refresh()
    page._on_auto_refresh()
    assert reloads == [1]


def test_auto_refresh_stops_when_page_not_visible(page):
    page.show()
    assert page._auto_refresh_timer.isActive()
    page.hide()
    page._auto_refresh_timer.start()   # 模拟隐藏后残留的一次触发
    page._on_auto_refresh()
    assert not page._auto_refresh_timer.isActive()


# ---------- 自适应范围 ----------

def test_reload_enables_auto_range_unless_keep_view(page, db, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    calls = []
    monkeypatch.setattr(page.chart_widget, 'enableAutoRange', lambda *a, **k: calls.append(1))

    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE)
    assert calls == [1]

    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE, keep_view=True)
    assert calls == [1]


# ---------- 统计条 ----------

def test_stats_strip_shows_six_cells_from_metric_stats(page, db):
    task = _seed_task(db, samples=10)   # 100 MB -> 109 MB，每秒涨 1 MB
    page.select_task(task.task_id)

    texts = {key: label.text() for key, label in page.stat_value_labels.items()}
    assert tuple(texts) == ("起点", "当前", "变化", "增速", "最小", "最大")
    assert texts["起点"] == "100 MB"
    assert texts["当前"] == "109 MB"
    assert texts["变化"].startswith("+9 MB")
    assert texts["增速"].endswith("/小时") and texts["增速"].startswith("+")
    assert texts["最小"] == "100 MB" and texts["最大"] == "109 MB"


def test_stats_strip_is_cleared_without_data(page):
    page._clear_display()
    assert {label.text() for label in page.stat_value_labels.values()} == {"--"}


@pytest.mark.parametrize("width, columns", [(1400, 6), (800, 3), (400, 2)])
def test_stats_strip_wraps_responsively(page, width, columns):
    assert page._stats_column_count(width) == columns
    page._update_stats_layout(width)
    used_columns = {page.stats_layout.getItemPosition(i)[1]
                    for i in range(page.stats_layout.count())}
    assert max(used_columns) + 1 == columns


# ---------- 打点竖线 ----------

def test_markers_are_drawn_as_vertical_lines(page, db):
    task = _seed_task(db, samples=10, markers=[(2, "第1轮"), (6, "第2轮")])
    page.select_task(task.task_id)

    lines = [item for item in page.chart_widget.getPlotItem().items
             if isinstance(item, pg.InfiniteLine) and item is not page.crosshair_line]
    assert len(lines) == 2
    assert sorted(line.value() for line in lines) == [
        (BASE + timedelta(seconds=2)).timestamp(),
        (BASE + timedelta(seconds=6)).timestamp()]


def test_no_marker_lines_for_task_without_markers(page, db):
    task = _seed_task(db, samples=10)
    page.select_task(task.task_id)
    lines = [item for item in page.chart_widget.getPlotItem().items
             if isinstance(item, pg.InfiniteLine) and item is not page.crosshair_line]
    assert lines == []


# ---------- 表格提示 ----------

def test_table_hint_shows_shown_over_total(page, db, monkeypatch):
    task = _seed_task(db, samples=10)
    monkeypatch.setattr('ui.pages.history_page.TABLE_POINT_LIMIT', 4)
    page.select_task(task.task_id)

    assert page.table_hint_label.text() == "最新数据优先 · 显示最近 4 条 / 共 10 条"
    assert page.data_table.rowCount() == 4


def test_table_hint_uses_thousands_separator(page):
    points = [DataPoint("t", BASE + timedelta(seconds=i), 1.0, MetricType.NUM_THREADS)
              for i in range(3)]
    page._update_table(points, MetricType.NUM_THREADS, total=12345)
    assert "共 12,345 条" in page.table_hint_label.text()


# ---------- 任务下拉 ----------

def test_task_combo_text_and_single_query(page, db, manager, monkeypatch):
    long_note = "建模 200 次回归测试，连续运行过夜观察内存增长"
    _seed_task(db, samples=30, note=long_note, name="MPrintExp.exe")
    _seed_task(db, samples=0, name="nodata.exe")   # 没有数据的任务不出现

    counted = []
    monkeypatch.setattr(
        db, 'get_data_point_count', lambda *a, **k: counted.append(1) or 0)
    page._load_tasks()

    assert counted == [], "任务下拉不应再逐任务统计数据点"
    assert page.task_combo.count() == 1
    text = page.task_combo.itemText(0)
    assert "MPrintExp.exe" in text and "PID 4321" in text and "07-16 20:00" in text
    assert "…" in text and long_note not in text
    assert long_note in page.task_combo.toolTip()


def test_running_task_is_marked_in_combo(page, db, services):
    task = _seed_task(db, samples=5, status="running")
    services.task_status.set_running(task.task_id)
    page._load_tasks()
    assert "运行中" in page.task_combo.itemText(0)


def test_select_task_picks_given_task(page, db):
    first = _seed_task(db, samples=3, name="a.exe", start=BASE)
    second = _seed_task(db, samples=3, name="b.exe", start=BASE + timedelta(hours=1))

    page.select_task(first.task_id)
    assert page.current_task_id == first.task_id
    page.select_task(second.task_id)
    assert page.current_task_id == second.task_id
    assert "b.exe" in page.task_combo.currentText()

    page.select_task("missing-task")
    # 不在列表中（已删除或尚无数据）：清空选择并显示"请选择监控任务"，
    # 不静默回落到首项；列表本身保持完整
    assert page.current_task_id is None
    assert page.current_metric is None
    assert page.task_combo.currentIndex() == -1
    assert page.task_combo.count() == 2
    assert page.empty_title_label.text() == "请选择监控任务"


# ---------- 删除经 manager ----------

def test_delete_goes_through_manager(page, db, manager, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)

    services.dialogs.push('confirm', True)
    deleted = []
    real = manager.delete_task_data
    monkeypatch.setattr(
        manager, 'delete_task_data', lambda tid: deleted.append(tid) or real(tid))

    page._on_delete_task_clicked()

    # 确认框挂在主窗口上、按钮文案为"删除/取消"
    confirm = services.dialogs.last_call('confirm')
    assert confirm['parent'] is page.window()
    assert confirm['title'] == "确认删除"
    assert "demo.exe (PID: 4321)" in confirm['content']
    assert (confirm['confirm_text'], confirm['cancel_text']) == ("删除", "取消")
    assert deleted == [task.task_id]
    assert db.get_task(task.task_id) is None
    assert page.current_task_id is None
    assert page.task_combo.count() == 0
    assert services.notifier.last().level == "success"


def test_delete_cancelled_keeps_task(page, db, manager, services, monkeypatch):
    """确认框里选了取消：不经 manager 删除，也不弹任何提示。"""
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    services.dialogs.push('confirm', False)
    monkeypatch.setattr(
        manager, 'delete_task_data', lambda tid: pytest.fail("取消后不应删除"))

    page._on_delete_task_clicked()

    assert db.get_task(task.task_id) is not None
    assert services.notifier.notices == []


def test_delete_failure_keeps_task(page, db, manager, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)

    services.dialogs.push('confirm', True)
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: False)

    page._on_delete_task_clicked()

    errors = services.notifier.messages('error')
    assert len(errors) == 1
    # 错误提示不能一闪即逝：不自动消失，由用户手动关闭
    assert errors[0].effective_duration == -1
    assert db.get_task(task.task_id) is not None


def test_delete_confirm_is_chinese_and_enter_does_not_delete(qapp, db, manager, monkeypatch):
    """真实确认框（生产 QtDialogs 构造的对话框）：按钮为"删除/取消"，显示后焦点在
    "取消"，回车不会删除"""
    task = _seed_task(db, samples=5)
    seen = {}

    def _probe(box):
        # 不进模态循环：显示后记录状态，再向焦点控件按回车，模拟用户顺手回车
        box.show()
        QTest.qWait(300)  # 等淡入动画结束，避免动画回调落到后续用例里
        seen.update(yes=box.yesButton.text(), cancel=box.cancelButton.text(),
                    focus=box.focusWidget())
        accepted = []
        box.yesSignal.connect(lambda: accepted.append(True))
        QTest.keyClick(box.focusWidget(), Qt.Key_Return)
        for _ in range(50):  # 等 100ms 淡出动画结束、对话框真正关闭
            if not box.isVisible():
                break
            QTest.qWait(20)
        seen['cancel_button'] = box.cancelButton
        return bool(accepted)

    page = make_history_page(
        db, manager, make_services(dialogs=QtDialogs(run_dialog=_probe)))
    deleted = []
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: deleted.append(tid) or True)
    try:
        page.show()
        page.select_task(task.task_id)

        page._on_delete_task_clicked()

        assert seen['yes'] == "删除"
        assert seen['cancel'] == "取消"
        assert seen['focus'] is seen['cancel_button']
        assert deleted == []
        assert db.get_task(task.task_id) is not None
    finally:
        page._auto_refresh_timer.stop()
        page.close()


# ---------- 错误提示停留时长 ----------

def test_load_task_failure_error_stays_until_closed(page, services):
    page._load_task_data("不存在的任务")

    errors = services.notifier.messages('error')
    assert len(errors) == 1
    assert errors[0].effective_duration == -1


def test_save_chart_failure_error_stays_until_closed(page, db, services, monkeypatch, tmp_path):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    assert page._chart_x
    services.dialogs.push('ask_save_path', str(tmp_path / "chart.png"))

    def _boom(*args, **kwargs):
        raise RuntimeError("导出失败")

    monkeypatch.setattr(pg_exporters, 'ImageExporter', _boom)

    page._save_chart_as_png()

    errors = services.notifier.messages('error')
    assert len(errors) == 1
    assert errors[0].effective_duration == -1


def test_save_chart_default_name_uses_clock_and_cancel_is_silent(page, db, services):
    """PNG 默认文件名的时间戳取自注入的时钟；保存框里取消则什么都不做、不提示。"""
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    services.clock.set(datetime(2026, 8, 1, 9, 30, 15))
    services.dialogs.push('ask_save_path', None)

    page._save_chart_as_png()

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

    page._save_chart_as_png()

    assert target.exists() and target.stat().st_size > 0
    notice = services.notifier.last()
    assert notice.level == "success" and str(target) in notice.content


def test_copy_chart_puts_pixmap_on_clipboard(page, db, services):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)

    page._copy_chart_image()

    pixmap = services.dialogs.last_call('copy_pixmap')['pixmap']
    assert not pixmap.isNull()
    assert services.notifier.last().level == "success"


def test_chart_actions_without_data_only_warn(page, services):
    """没有图表数据时保存/复制只给提示，不弹保存框、不碰剪贴板。"""
    page._save_chart_as_png()
    page._copy_chart_image()

    assert services.notifier.levels() == ["warning", "warning"]
    assert services.dialogs.calls == []


def test_copy_chart_failure_error_stays_until_closed(page, db, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    assert page._chart_x

    def _boom(*args, **kwargs):
        raise RuntimeError("截图失败")

    monkeypatch.setattr(page.chart_widget, 'grab', _boom)

    page._copy_chart_image()

    errors = services.notifier.messages('error')
    assert len(errors) == 1
    assert errors[0].effective_duration == -1


def test_small_window_scrolls_instead_of_clipping_chart(qapp, db, manager):
    """默认窗口尺寸下图表不能被压出卡片：内容放不下时页面应出现滚动条。"""
    task = _seed_task(db, samples=20)
    page = make_history_page(db, manager, make_services())
    page.resize(930, 640)
    page.show()
    try:
        page.select_task(task.task_id)
        qapp.processEvents()
        chart = page.chart_widget
        card = chart.parentWidget()
        assert chart.geometry().bottom() <= card.rect().bottom()
        content = page.widget()
        if content.minimumSizeHint().height() > page.viewport().height():
            assert page.verticalScrollBar().maximum() > 0
    finally:
        page._auto_refresh_timer.stop()
        page.close()


def test_auto_selection_follows_newest_task_but_user_choice_is_kept(page, db):
    """启动时默认选中的旧任务：出现更新的任务后切到最新；用户主动选过的任务保持不变。"""
    old = _seed_task(db, samples=5, name="old.exe", start=BASE)
    page._load_tasks()
    assert page.current_task_id == old.task_id

    newer = _seed_task(db, samples=5, name="newer.exe", start=BASE + timedelta(hours=1))
    page._load_tasks()
    assert page.current_task_id == newer.task_id

    # 用户主动选回旧任务后，再出现新任务也不切走
    old_index = next(i for i in range(page.task_combo.count())
                     if page.task_combo.itemData(i) == old.task_id)
    page.task_combo.setCurrentIndex(old_index)
    assert page.current_task_id == old.task_id
    _seed_task(db, samples=5, name="newest.exe", start=BASE + timedelta(hours=2))
    page._load_tasks()
    assert page.current_task_id == old.task_id


def test_select_task_is_an_explicit_choice(page, db):
    """从监控页"查看历史"跳转选中的任务，即便不是最新也不被自动切走。"""
    old = _seed_task(db, samples=5, name="old.exe", start=BASE)
    _seed_task(db, samples=5, name="newer.exe", start=BASE + timedelta(hours=1))
    page._load_tasks()
    page.select_task(old.task_id)
    page._load_tasks()
    assert page.current_task_id == old.task_id


def test_running_task_duration_label_updates_in_place(page, db, services, monkeypatch):
    """页面一直开着时，下拉里运行中任务的时长要跟着走；结束后改为最终时长。
    原地改文字，不重载列表、不打断用户正在看的任务。"""
    task = _seed_task(db, samples=3, status="running")
    other = _seed_task(db, samples=3, start=BASE - timedelta(days=1))
    clock = services.clock
    services.task_status.set_running(task.task_id)
    monkeypatch.setattr(page, '_start_auto_refresh_worker', lambda *a, **k: None)
    clock.set(BASE + timedelta(minutes=5))

    page.show()
    page.select_task(other.task_id)
    index = next(i for i in range(page.task_combo.count())
                 if page.task_combo.itemData(i) == task.task_id)
    assert "运行中 5分钟" in page.task_combo.itemText(index)

    reloads = []
    monkeypatch.setattr(page, '_load_tasks', lambda: reloads.append(1))
    clock.set(BASE + timedelta(minutes=15))
    page._on_auto_refresh()
    assert "运行中 15分钟" in page.task_combo.itemText(index)

    db.update_task_status(task.task_id, "stopped", BASE + timedelta(minutes=20))
    services.task_status.set_running(task.task_id, False)
    clock.set(BASE + timedelta(hours=2))
    page._on_auto_refresh()
    text = page.task_combo.itemText(index)
    assert "运行中" not in text and "20分钟" in text
    assert reloads == []
    assert page.current_task_id == other.task_id


# ---------- 读库失败（StorageError）与"暂无数据"区分 ----------

def _raise_storage(*a, **k):
    from data.errors import StorageError
    raise StorageError("读取数据点失败: database disk image is malformed")


@pytest.mark.parametrize("repo_attr, method", [
    ("points", "fetch"),
    ("analytics", "legacy_stats"),
    ("analytics", "legacy_buckets"),
    ("markers", "list"),
    ("tasks", "get"),
])
def test_load_failure_shows_read_error_not_empty(page, db, services, monkeypatch,
                                                 repo_attr, method):
    task = _seed_task(db, samples=5, markers=[(1, "m")])
    page.select_task(task.task_id)
    assert page.content_stack.currentWidget() is page.analysis_page
    notices_before = len(services.notifier.notices)

    monkeypatch.setattr(getattr(db, repo_attr), method, _raise_storage)
    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE)

    assert page.content_stack.currentWidget() is page.empty_state_page
    assert page.empty_title_label.text() == "读取数据失败"
    new = services.notifier.notices[notices_before:]
    assert [(n.level, n.title) for n in new] == [("error", "读取数据失败")]
    assert "malformed" in new[0].content


def test_task_list_failure_shows_read_error(page, db, services, monkeypatch):
    _seed_task(db, samples=3)
    monkeypatch.setattr(db.tasks, "list_with_data", _raise_storage)

    page._load_tasks()

    assert page.empty_title_label.text() == "读取数据失败"
    assert services.notifier.last().level == "error"
    assert page.task_combo.count() == 0 and page.current_task_id is None


def test_empty_range_still_shows_no_data_wording(page, db, services):
    """真正没有数据时仍是"暂无数据"文案，且不弹错误"""
    task = _seed_task(db, samples=3)
    page.select_task(task.task_id)
    page.current_range_key = 'all'
    page._load_task_data(task.task_id, MetricType.NUM_THREADS)   # 该任务没有这个指标的数据
    assert page.empty_title_label.text() == "该任务暂无数据"
    assert services.notifier.messages("error") == []
