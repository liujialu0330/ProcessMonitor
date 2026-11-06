"""
导出页：默认保存路径随任务重置、覆盖确认、千分位计数、工作线程释放时机、
导出文件带「打点」列、"运行中"以任务状态服务为准。

页面经 tests/factories.make_export_page 构造：提示、对话框、时钟、任务运行状态
都是替身（RecordingNotifier / ScriptedDialogs / FakeClock / FakeTaskStatus），
不替换页面模块里的任何名字。操作尽量走公开接口：reload_tasks / refresh_running_tasks、
按钮 click()、TaskSelector（用户点选经 _user_select）；只有导出线程信号槽的时序
用例直接调用槽函数。
"""
import csv
import inspect
import os
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock, FakeTaskStatus, ScriptedDialogs
from tests.factories import make_export_page, make_services
from tests.helpers import poll_until
from ui.pages.export_page import ExportPage
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 20, 54, 51)

SAVE_TITLE = "选择保存位置"
CSV_FILTER = "CSV 文件 (*.csv)"


def _seed_task(db, samples=5, name="demo.exe", pid=4321, start=BASE, note="",
               markers=(), status="stopped"):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=pid, process_name=name,
        metric_types=[MetricType.NUM_THREADS], interval=1.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(seconds=samples),
        status=status, note=note)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), float(10 + i),
                  MetricType.NUM_THREADS) for i in range(samples)])
    for offset, label in markers:
        db.add_marker(task.task_id, start + timedelta(seconds=offset), label)
    return task


def _user_select(page, index):
    """模拟用户在任务下拉里点选第 index 项（对内部 ComboBox 设当前项与点选等价）。"""
    page.task_selector.combo.setCurrentIndex(index)


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


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(
        dialogs=_HookedDialogs(), clock=FakeClock(BASE), task_status=FakeTaskStatus())


@pytest.fixture
def notifier(services):
    return services.notifier


@pytest.fixture
def dialogs(services):
    return services.dialogs


@pytest.fixture
def clock(services):
    return services.clock


@pytest.fixture
def task_status(services):
    return services.task_status


@pytest.fixture
def page(qapp, db, manager, services, tmp_path):
    widget = make_export_page(db, manager, services)
    widget.last_dir = str(tmp_path)
    widget.resize(900, 700)
    yield widget
    worker = widget.export_worker
    if worker is not None:
        worker.cancel()
        worker.wait(5000)
    widget.running_timer.stop()
    widget.close()


# ---------- 构造 ----------

def test_constructor_requires_injected_dependencies():
    """db / manager / services 必填，不再兜底自建 Database 或省略 manager 登记。"""
    params = inspect.signature(ExportPage.__init__).parameters
    for name in ("db", "manager", "services"):
        assert params[name].default is inspect.Parameter.empty, name
    assert params["parent"].default is None


# ---------- 默认路径 ----------

def test_default_save_path_is_last_dir_plus_task_filename(page, db, tmp_path):
    task = _seed_task(db)
    page.reload_tasks()

    assert page.current_task_id == task.task_id
    assert page.path_edit.text() == str(tmp_path / "demo_4321_20260716_205451.csv")


def test_save_path_resets_when_task_switches(page, db, tmp_path):
    first = _seed_task(db, name="a.exe", pid=1, start=BASE)
    second = _seed_task(db, name="b.exe", pid=2, start=BASE + timedelta(hours=1))
    page.reload_tasks()
    ids = [page.task_selector.task_id_at(i) for i in range(page.task_selector.count())]
    assert set(ids) == {first.task_id, second.task_id}

    _user_select(page, ids.index(first.task_id))
    assert os.path.basename(page.path_edit.text()).startswith("a_1_")
    _user_select(page, ids.index(second.task_id))
    assert os.path.basename(page.path_edit.text()).startswith("b_2_")


def test_manual_path_is_kept_when_list_reloads_same_task(page, db, tmp_path):
    _seed_task(db)
    page.reload_tasks()
    custom = str(tmp_path / "我的导出.csv")
    page.path_edit.setText(custom)

    page.reload_tasks()   # 页面再次显示 -> 重新载入列表，任务没变

    assert page.path_edit.text() == custom


def test_browse_remembers_directory_for_next_task(page, db, dialogs, tmp_path):
    first = _seed_task(db, name="a.exe", pid=1, start=BASE)
    _seed_task(db, name="b.exe", pid=2, start=BASE + timedelta(hours=1))
    page.reload_tasks()
    chosen_dir = tmp_path / "reports"
    chosen_dir.mkdir()
    default_before = page.path_edit.text()
    dialogs.push('ask_save_path', str(chosen_dir / "x.csv"))

    page.browse_button.click()
    assert page.path_edit.text() == str(chosen_dir / "x.csv")
    call = dialogs.last_call('ask_save_path')
    assert call["parent"] is page
    assert call["title"] == SAVE_TITLE
    assert call["default_path"] == default_before
    assert call["name_filter"] == CSV_FILTER

    other = [i for i in range(page.task_selector.count())
             if page.task_selector.task_id_at(i) != page.current_task_id][0]
    _user_select(page, other)
    assert os.path.dirname(page.path_edit.text()) == str(chosen_dir)
    assert first is not None


def test_browse_cancel_keeps_path(page, db, dialogs):
    _seed_task(db)
    page.reload_tasks()
    before = page.path_edit.text()
    dialogs.push('ask_save_path', None)
    page.browse_button.click()
    assert page.path_edit.text() == before
    assert len(dialogs.calls_of('ask_save_path')) == 1


def test_overwrite_requires_confirmation(page, db, dialogs, tmp_path):
    _seed_task(db)
    page.reload_tasks()
    existing = tmp_path / "exists.csv"
    existing.write_text("原有内容", encoding='utf-8')
    page.path_edit.setText(str(existing))

    dialogs.push('confirm', False)
    page.export_button.click()
    assert page.export_worker is None
    assert existing.read_text(encoding='utf-8') == "原有内容"

    dialogs.push('confirm', True)
    page.export_button.click()
    asked = dialogs.calls_of('confirm')
    assert len(asked) == 2
    assert all(str(existing) in call["content"] for call in asked)
    assert asked[-1]["title"] == "文件已存在"
    assert asked[-1]["confirm_text"] == "覆盖"
    assert asked[-1]["parent"] is page.window()
    assert page.export_worker is not None


def test_no_overwrite_prompt_for_new_file(page, db, dialogs):
    _seed_task(db)
    page.reload_tasks()
    page.export_button.click()
    assert 'confirm' not in dialogs.methods_called()
    assert page.export_worker is not None


def test_export_with_empty_path_opens_save_dialog_first(page, db, dialogs, tmp_path):
    """路径被清空时点导出：直接弹保存对话框，选好后继续导出"""
    _seed_task(db)
    page.reload_tasks()
    page.path_edit.setText("")
    dialogs.push('ask_save_path', str(tmp_path / "chosen.csv"))

    page.export_button.click()

    assert page.path_edit.text() == str(tmp_path / "chosen.csv")
    assert page.export_worker is not None


def test_export_worker_reuses_page_db(page, db):
    """导出线程复用页面持有的 Database 实例，不按路径另建"""
    _seed_task(db)
    page.reload_tasks()
    page.export_button.click()
    assert page.export_worker is not None
    assert page.export_worker.db is page.db


# ---------- 错误提示 ----------

def test_error_infobars_stay_until_closed(page, db, notifier, monkeypatch):
    """错误类提示不自动消失（duration=-1），需用户手动关闭"""
    page._on_export_error("导出失败：磁盘已满")
    notice = notifier.last()
    assert notice.level == "error"
    assert notice.effective_duration == -1
    assert notice.parent is page

    # 列表里有、但读取任务详情失败（如刚被别处删除）：载入摘要时报错
    _seed_task(db)
    monkeypatch.setattr(db, 'get_task', lambda task_id: None)
    page.reload_tasks()
    notice = notifier.last()
    assert (notice.level, notice.title) == ("error", "任务加载失败")
    assert notice.effective_duration == -1
    assert page.process_name_label.text() == "未选择任务"


# ---------- 千分位与任务来源 ----------

def test_task_info_uses_thousands_separators(page, db):
    task = _seed_task(db, samples=3)
    page.reload_tasks()
    assert page.current_task_id == task.task_id

    page._on_export_progress(1234567)
    assert "1,234,567" in page.export_status_label.text()


def test_task_list_comes_from_single_query(page, db, monkeypatch):
    _seed_task(db, samples=3)
    _seed_task(db, samples=3, name="other.exe", pid=9, start=BASE + timedelta(hours=1))
    counted = []
    monkeypatch.setattr(
        db, 'get_data_point_count', lambda *a, **k: counted.append(1) or 0)
    page.reload_tasks()
    # 只有选中任务的信息面板会统计一次，下拉列表本身不逐任务 COUNT
    assert len(counted) <= 1
    assert page.task_selector.count() == 2
    texts = [page.task_selector.item_text(i) for i in range(page.task_selector.count())]
    assert any("2026-07-16 20:54" in text for text in texts)


def test_export_finished_message_uses_thousands(page, notifier, tmp_path):
    page._on_export_finished(str(tmp_path / "x.csv"), 1200, 3600)
    notice = notifier.last()
    assert notice.level == "success"
    assert notice.duration == 8000
    assert "1,200" in notice.content and "3,600" in notice.content


def test_open_folder_button_opens_export_dir_and_tolerates_failure(
        page, notifier, dialogs, tmp_path):
    """导出成功提示上的"打开文件夹"按钮经 Dialogs 打开所在目录；打开失败不再打扰"""
    page._on_export_finished(str(tmp_path / "x.csv"), 1, 1)
    bar = notifier.last("success").bar
    assert page.open_folder_button in bar.widgets

    page.open_folder_button.click()
    assert dialogs.last_call('open_folder') == {"path": str(tmp_path)}

    dialogs.push('open_folder', OSError("目录已被删除"))
    page.open_folder_button.click()   # 不抛出、不弹提示
    assert len(dialogs.calls_of('open_folder')) == 2
    assert notifier.levels() == ["success"]


# ---------- 工作线程释放时机 ----------

def test_result_slots_do_not_release_worker(page, db, tmp_path):
    page._export_worker = sentinel = object()
    page._on_export_finished(str(tmp_path / "x.csv"), 1, 1)
    assert page.export_worker is sentinel
    page._on_export_error("失败")
    assert page.export_worker is sentinel
    page._export_worker = None


def test_export_end_to_end_releases_worker_on_thread_finished(
        page, db, notifier, tmp_path):
    _seed_task(db, samples=6, markers=[(2, "第1轮")])
    page.reload_tasks()
    target = tmp_path / "out.csv"
    page.path_edit.setText(str(target))

    page.export_button.click()
    worker = page.export_worker
    assert worker is not None
    assert not page.export_button.isEnabled()
    # 登记到线程登记表：退出时先取消（删除半成品临时文件）再等 2 秒
    entries = page.services.threads.entries()
    assert [(e.name, e.thread, e.cancel, e.timeout_ms) for e in entries] == [
        ("导出数据", worker, worker.cancel, 2000)]

    from PyQt5.QtWidgets import QApplication
    assert poll_until(lambda: (QApplication.processEvents() or True)
                      and page.export_worker is None, timeout=15)
    assert page.export_button.isEnabled()
    assert notifier.last().level == "success"
    # 线程结束后由登记表自动注销
    assert page.services.threads.entries() == []

    # 导出文件末列为「打点」
    with open(target, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))
    assert rows[0][-1] == "打点"
    assert [row[-1] for row in rows[1:]] == ["", "", "第1轮", "", ""] or \
        [row[-1] for row in rows[1:]].count("第1轮") == 1


def test_export_finished_refreshes_task_summary(page, db, tmp_path):
    """运行中任务导出期间仍在采集：导出完成后摘要的采集次数要同步更新。"""
    task = _seed_task(db, samples=3)
    page.reload_tasks()
    assert "3 次采集" in page.data_count_label.text()

    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(seconds=10 + i), 1.0,
                  MetricType.NUM_THREADS)
        for i in range(2)])
    page._on_export_finished(str(tmp_path / "x.csv"), 5, 5)
    assert "5 次采集" in page.data_count_label.text()


# ---------- 运行中任务 ----------

def test_running_task_label_and_summary_refresh_while_visible(
        page, db, clock, task_status):
    """页面一直开着时，运行中任务的下拉时长与摘要要定时刷新；任务结束后显示最终状态，
    且不重置用户已选的保存路径。"""
    task = _seed_task(db, samples=3, status="running")
    task_status.set_running(task.task_id)
    clock.set(BASE + timedelta(minutes=5))

    page.show()
    assert page.running_timer.is_active()
    assert "运行中 5分钟" in page.task_selector.item_text(0)
    assert page.status_label.text() == "运行中"
    assert page.end_time_label.text() == "进行中"
    page.path_edit.setText(r"D:\导出\a.csv")

    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(seconds=10 + i), 1.0,
                  MetricType.NUM_THREADS)
        for i in range(2)])
    clock.set(BASE + timedelta(minutes=15))
    page.refresh_running_tasks()
    assert "运行中 15分钟" in page.task_selector.item_text(0)
    assert "5 次采集" in page.data_count_label.text()

    # 任务结束：采集线程退出、库里写入结束时间
    task_status.set_running(task.task_id, False)
    db.update_task_status(task.task_id, "stopped", BASE + timedelta(minutes=20))
    page.refresh_running_tasks()
    text = page.task_selector.item_text(0)
    assert "运行中" not in text and "20分钟" in text
    assert page.status_label.text() == "已停止"
    assert page.path_edit.text() == r"D:\导出\a.csv"

    # 结束后再推进时钟：最终时长不再变化
    clock.advance(timedelta(hours=1))
    page.refresh_running_tasks()
    assert page.task_selector.item_text(0) == text

    page.hide()
    assert not page.running_timer.is_active()


def test_running_state_follows_task_status_not_db_column(page, db, clock, task_status):
    """"运行中"以任务状态服务为准：库里仍标 running、但采集线程已不存在的孤儿任务
    显示为已停止，时长与结束时间退回最后一次采样，不随时钟增长。"""
    orphan = _seed_task(db, samples=3, status="running")
    clock.set(BASE + timedelta(hours=2))

    page.reload_tasks()

    assert page.current_task_id == orphan.task_id
    text = page.task_selector.item_text(0)
    assert "运行中" not in text and text.endswith("2秒")
    assert page.status_label.text() == "已停止"
    assert page.end_time_label.text() == (
        BASE + timedelta(seconds=2)).strftime('%Y-%m-%d %H:%M:%S')

    clock.advance(timedelta(hours=1))
    page.refresh_running_tasks()
    assert page.task_selector.item_text(0) == text


def test_stopping_task_still_counts_as_running(page, db, clock, task_status):
    """库里已标 stopped 但任务状态服务仍报运行（停止收尾中）：按运行中显示。"""
    task = _seed_task(db, samples=3, status="stopped")
    task_status.set_running(task.task_id)
    clock.set(BASE + timedelta(minutes=7))

    page.reload_tasks()

    assert "运行中 7分钟" in page.task_selector.item_text(0)
    assert page.status_label.text() == "运行中"


# ---------- 空状态 ----------

def test_empty_state_card_replaces_export_card(page, db):
    """没有可导出任务：空状态卡代替整张导出卡；有了任务后点页头"刷新"恢复。"""
    page.reload_tasks()
    assert page.empty_state_card.isVisibleTo(page)
    assert not page.export_card.isVisibleTo(page)
    assert page.task_selector.count() == 0 and page.current_task_id is None
    assert page.path_edit.text() == ""

    task = _seed_task(db)
    page.refresh_button.click()
    assert page.export_card.isVisibleTo(page)
    assert not page.empty_state_card.isVisibleTo(page)
    assert page.current_task_id == task.task_id


def test_current_option_tooltip_follows_selection(page, db, clock):
    """下拉框提示是当前任务的完整信息（与历史页同口径）。"""
    from ui.formatting import format_task_tooltip
    first = _seed_task(db, name="a.exe", pid=1, start=BASE, note="完整备注")
    _seed_task(db, name="b.exe", pid=2, start=BASE + timedelta(hours=1))
    page.reload_tasks()
    ids = [option.task_id for option in page.task_selector.options()]
    _user_select(page, ids.index(first.task_id))
    assert page.task_selector.tooltip() == format_task_tooltip(
        db.get_task(first.task_id), False, clock.now())
    assert "完整备注" in page.task_selector.tooltip()


# ---------- 与清理数据库互斥 ----------

def test_export_registers_with_manager_until_thread_finished(
        page, manager, db, tmp_path):
    """开始导出即登记（此时不能开始清理数据库），线程结束后注销"""
    _seed_task(db, samples=6)
    page.reload_tasks()
    page.path_edit.setText(str(tmp_path / "out.csv"))

    page.export_button.click()
    assert page.export_worker is not None
    assert manager.has_running_exports() is True
    assert manager.begin_maintenance() is False

    from PyQt5.QtWidgets import QApplication
    assert poll_until(lambda: (QApplication.processEvents() or True)
                      and page.export_worker is None, timeout=15)
    assert manager.has_running_exports() is False
    assert manager.begin_maintenance() is True
    manager.end_maintenance()


def test_export_unregisters_even_if_worker_reference_replaced(page, manager):
    """旧 worker 的 finished 晚于新 worker 启动到达时也要注销自己那一次登记"""
    assert manager.begin_export() is True
    old_worker = object()
    page._export_worker = newer = SimpleNamespace(isRunning=lambda: True)
    try:
        page._on_export_thread_finished(old_worker)

        assert manager.has_running_exports() is False
        assert page.export_worker is newer
    finally:
        page._export_worker = None


def test_export_refused_during_maintenance(page, manager, db, notifier, dialogs):
    """清理数据库进行中：不弹保存对话框、不启动导出线程，并提示稍后再导出"""
    _seed_task(db)
    page.reload_tasks()
    page.path_edit.setText("")
    assert manager.begin_maintenance() is True

    page.export_button.click()

    assert dialogs.calls_of('ask_save_path') == []
    assert page.export_worker is None
    assert manager.has_running_exports() is False
    notice = notifier.last()
    assert notice.level == "warning"
    assert notice.content == "正在清理数据库，请稍后再导出"
    manager.end_maintenance()


def test_export_refused_if_maintenance_starts_during_overwrite_confirm(
        page, manager, db, notifier, dialogs, tmp_path):
    """覆盖确认框期间清理已开始：以登记结果为准，不启动导出线程"""
    _seed_task(db)
    page.reload_tasks()
    existing = tmp_path / "exists.csv"
    existing.write_text("原有内容", encoding='utf-8')
    page.path_edit.setText(str(existing))
    dialogs.push('confirm', True)
    dialogs.on_confirm = manager.begin_maintenance

    page.export_button.click()

    assert manager.is_maintenance_running() is True
    assert page.export_worker is None
    assert manager.has_running_exports() is False
    assert notifier.last().content == "正在清理数据库，请稍后再导出"
    assert page.export_button.isEnabled()
    manager.end_maintenance()


def test_no_task_selected_warns_without_dialogs(page, notifier, dialogs):
    """尚未选择任务时点浏览/导出：只提示先选任务，不弹保存对话框"""
    page.browse_button.click()
    page.export_button.click()

    assert [n.title for n in notifier.messages("warning")] == ["请选择任务"] * 2
    assert dialogs.methods_called() == []
