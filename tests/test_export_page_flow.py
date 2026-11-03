"""
导出页：默认保存路径随任务重置、覆盖确认、千分位计数、工作线程释放时机、
导出文件带「打点」列。
"""
import csv
import os
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import ui.pages.export_page as export_module
from data.models import DataPoint, MonitorTask
from tests.helpers import poll_until
from ui.pages.export_page import ExportPage
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 20, 54, 51)


def _seed_task(db, samples=5, name="demo.exe", pid=4321, start=BASE, note="",
               markers=()):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=pid, process_name=name,
        metric_types=[MetricType.NUM_THREADS], interval=1.0, start_time=start,
        end_time=start + timedelta(seconds=samples), status="stopped", note=note)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), float(10 + i),
                  MetricType.NUM_THREADS) for i in range(samples)])
    for offset, label in markers:
        db.add_marker(task.task_id, start + timedelta(seconds=offset), label)
    return task


@pytest.fixture
def silent_infobar(monkeypatch):
    calls = []
    recorder = SimpleNamespace(**{
        level: (lambda level=level, **kwargs: calls.append((level, kwargs)) or
                SimpleNamespace(addWidget=lambda *a, **k: None))
        for level in ("info", "success", "warning", "error")})
    monkeypatch.setattr(export_module, 'InfoBar', recorder)
    return calls


@pytest.fixture
def page(qapp, db, tmp_path):
    widget = ExportPage(db=db)
    widget._last_dir = str(tmp_path)
    widget.resize(900, 700)
    yield widget
    worker = widget._export_worker
    if worker is not None:
        worker.cancel()
        worker.wait(5000)
    widget.close()


# ---------- 默认路径 ----------

def test_default_save_path_is_last_dir_plus_task_filename(page, db, tmp_path):
    task = _seed_task(db)
    page._load_tasks()

    assert page.current_task_id == task.task_id
    assert page.path_edit.text() == str(tmp_path / "demo_4321_20260716_205451.csv")


def test_save_path_resets_when_task_switches(page, db, tmp_path):
    first = _seed_task(db, name="a.exe", pid=1, start=BASE)
    second = _seed_task(db, name="b.exe", pid=2, start=BASE + timedelta(hours=1))
    page._load_tasks()
    ids = [page.task_combo.itemData(i) for i in range(page.task_combo.count())]
    assert set(ids) == {first.task_id, second.task_id}

    page.task_combo.setCurrentIndex(ids.index(first.task_id))
    assert os.path.basename(page.path_edit.text()).startswith("a_1_")
    page.task_combo.setCurrentIndex(ids.index(second.task_id))
    assert os.path.basename(page.path_edit.text()).startswith("b_2_")


def test_manual_path_is_kept_when_list_reloads_same_task(page, db, tmp_path):
    _seed_task(db)
    page._load_tasks()
    custom = str(tmp_path / "我的导出.csv")
    page.path_edit.setText(custom)

    page._load_tasks()   # 页面再次显示 -> 重新载入列表，任务没变

    assert page.path_edit.text() == custom


def test_browse_remembers_directory_for_next_task(page, db, tmp_path):
    first = _seed_task(db, name="a.exe", pid=1, start=BASE)
    _seed_task(db, name="b.exe", pid=2, start=BASE + timedelta(hours=1))
    page._load_tasks()
    chosen_dir = tmp_path / "reports"
    chosen_dir.mkdir()
    page._ask_save_path = lambda default: str(chosen_dir / "x.csv")

    page._browse_save_path()
    assert page.path_edit.text() == str(chosen_dir / "x.csv")

    other = [i for i in range(page.task_combo.count())
             if page.task_combo.itemData(i) != page.current_task_id][0]
    page.task_combo.setCurrentIndex(other)
    assert os.path.dirname(page.path_edit.text()) == str(chosen_dir)
    assert first is not None


def test_browse_cancel_keeps_path(page, db):
    _seed_task(db)
    page._load_tasks()
    before = page.path_edit.text()
    page._ask_save_path = lambda default: ""
    page._browse_save_path()
    assert page.path_edit.text() == before


def test_overwrite_requires_confirmation(page, db, tmp_path, monkeypatch, silent_infobar):
    _seed_task(db)
    page._load_tasks()
    existing = tmp_path / "exists.csv"
    existing.write_text("原有内容", encoding='utf-8')
    page.path_edit.setText(str(existing))

    monkeypatch.setattr(page, '_confirm_overwrite', lambda path: False)
    page._export_data()
    assert page._export_worker is None
    assert existing.read_text(encoding='utf-8') == "原有内容"

    asked = []
    monkeypatch.setattr(
        page, '_confirm_overwrite', lambda path: asked.append(path) or True)
    page._export_data()
    assert asked == [str(existing)]
    assert page._export_worker is not None


def test_no_overwrite_prompt_for_new_file(page, db, tmp_path, monkeypatch, silent_infobar):
    _seed_task(db)
    page._load_tasks()
    monkeypatch.setattr(
        page, '_confirm_overwrite', lambda path: pytest.fail("新文件不应询问覆盖"))
    page._export_data()
    assert page._export_worker is not None


# ---------- 千分位与任务来源 ----------

def test_task_info_uses_thousands_separators(page, db):
    task = _seed_task(db, samples=3)
    page._load_tasks()
    assert page.current_task_id == task.task_id

    page._on_export_progress(1234567)
    assert "1,234,567" in page.export_status_label.text()


def test_task_list_comes_from_single_query(page, db, monkeypatch):
    _seed_task(db, samples=3)
    _seed_task(db, samples=3, name="other.exe", pid=9, start=BASE + timedelta(hours=1))
    counted = []
    monkeypatch.setattr(
        db, 'get_data_point_count', lambda *a, **k: counted.append(1) or 0)
    page._load_tasks()
    # 只有选中任务的信息面板会统计一次，下拉列表本身不逐任务 COUNT
    assert len(counted) <= 1
    assert page.task_combo.count() == 2
    texts = [page.task_combo.itemText(i) for i in range(page.task_combo.count())]
    assert any("2026-07-16 20:54" in text for text in texts)


def test_export_finished_message_uses_thousands(page, silent_infobar, tmp_path):
    page._on_export_finished(str(tmp_path / "x.csv"), 1200, 3600)
    level, kwargs = silent_infobar[-1]
    assert level == "success"
    assert "1,200" in kwargs["content"] and "3,600" in kwargs["content"]


# ---------- 工作线程释放时机 ----------

def test_result_slots_do_not_release_worker(page, db, tmp_path, silent_infobar):
    page._export_worker = sentinel = object()
    page._on_export_finished(str(tmp_path / "x.csv"), 1, 1)
    assert page._export_worker is sentinel
    page._on_export_error("失败")
    assert page._export_worker is sentinel
    page._export_worker = None


def test_export_end_to_end_releases_worker_on_thread_finished(
        page, db, tmp_path, silent_infobar):
    _seed_task(db, samples=6, markers=[(2, "第1轮")])
    page._load_tasks()
    target = tmp_path / "out.csv"
    page.path_edit.setText(str(target))

    page._export_data()
    assert page._export_worker is not None
    assert not page.export_button.isEnabled()

    from PyQt5.QtWidgets import QApplication
    assert poll_until(lambda: (QApplication.processEvents() or True)
                      and page._export_worker is None, timeout=15)
    assert page.export_button.isEnabled()
    assert silent_infobar[-1][0] == "success"

    # 导出文件末列为「打点」
    with open(target, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))
    assert rows[0][-1] == "打点"
    assert [row[-1] for row in rows[1:]] == ["", "", "第1轮", "", ""] or \
        [row[-1] for row in rows[1:]].count("第1轮") == 1
