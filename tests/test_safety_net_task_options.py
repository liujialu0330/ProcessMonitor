"""
阶段 0 安全网（特性测试）：历史页与导出页的任务下拉同口径。

锁住的现有行为（重构为共用的 TaskOptionsModel + TaskSelector 时任何漂移都应变红）：
- 同一任务集合（运行中、已停止、带长备注、跨年、无数据）下，两页下拉的选项顺序
  与 itemData（task_id）一致：只列有数据的任务，按开始时间倒序；
- 选项文本只差日期格式：历史页 format_task_option_text(full_date=False)，
  导出页 full_date=True（"07-16 20:00" 对 "2026-07-16 20:00"），其余逐字相同；
- 运行中任务的时长原地刷新后两页仍一致；任务结束后两页都改为最终时长；
- 首次载入默认都选最新（第 0 项）；用户手动选中的任务在重新载入后两页都保持。

两页共用同一份服务替身：同一个 FakeClock 决定"现在几点"，同一个 FakeTaskStatus
决定"哪些任务仍在运行"（两页的"运行中"都以任务状态服务为准，与数据库 status 列
无关），这里让它与数据库状态一致后只比较文案口径。
"""
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock, FakeTaskStatus
from tests.factories import make_export_page, make_history_page, make_services
from ui.formatting import format_task_option_text, format_task_tooltip
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 20, 0, 0)
LONG_NOTE = "建模 200 次回归测试，连续运行过夜观察内存增长"


def _seed(db, name, start, status="stopped", note="", samples=3, end=None):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=1000 + len(name), process_name=name,
        metric_types=[MetricType.MEMORY_PRIVATE], interval=1.0, start_time=start,
        end_time=None if status == "running" else (end or start + timedelta(minutes=42)),
        status=status, note=note)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i),
                  MetricType.MEMORY_PRIVATE) for i in range(samples)])
    return task


@pytest.fixture
def clock():
    """两页共用的时钟"""
    return FakeClock(BASE + timedelta(hours=3, minutes=5))


@pytest.fixture
def tasks(db):
    """
    任务集合（按开始时间倒序即期望的下拉顺序）：
    - running：最新的有数据任务，运行中（数据库 status=running、无结束时间）；
    - noted：已停止，带超长备注（下拉截断、tooltip 完整）；
    - plain：已停止，去年开始（跨年，日期格式差异最明显）；
    - nodata：比所有任务都新但没有采样，两页都不应列出，也不应被默认选中。
    """
    nodata = _seed(db, "nodata.exe", BASE + timedelta(hours=4), samples=0)
    running = _seed(db, "running.exe", BASE + timedelta(hours=3), status="running")
    noted = _seed(db, "noted.exe", BASE + timedelta(hours=1), note=LONG_NOTE)
    plain = _seed(db, "plain.exe", datetime(2025, 12, 31, 23, 30),
                  end=datetime(2026, 1, 1, 1, 45))
    return SimpleNamespace(running=running, noted=noted, plain=plain, nodata=nodata,
                           order=[running.task_id, noted.task_id, plain.task_id])


@pytest.fixture
def task_status(tasks):
    """任务状态服务视角下的运行中任务（与数据库 status 保持一致）"""
    return FakeTaskStatus({tasks.running.task_id})


@pytest.fixture
def running_ids(task_status):
    """运行中任务 ID 集合：就是 task_status 内部的集合，增删即同时影响两页"""
    return task_status.running


@pytest.fixture
def services(clock, task_status):
    return make_services(clock=clock, task_status=task_status)


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def history(qapp, db, manager, services):
    # 构造函数里就会 _load_tasks：任务状态与时钟须在构造前就绪（由 services 提供）
    widget = make_history_page(db, manager, services)
    yield widget
    widget._auto_refresh_timer.stop()
    widget.close()


@pytest.fixture
def export(qapp, db, manager, services, tmp_path):
    widget = make_export_page(db, manager, services)
    widget._last_dir = str(tmp_path)
    widget._load_tasks()   # 导出页构造时不载入，生产路径由 showEvent 触发
    yield widget
    widget._running_refresh_timer.stop()
    widget.close()


def _options(page):
    combo = page.task_combo
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def _short_to_full(text, task):
    """把历史页文本里的开始时间换成导出页的完整日期格式（只替换一次）。"""
    return text.replace(task.start_time.strftime('%m-%d %H:%M'),
                        task.start_time.strftime('%Y-%m-%d %H:%M'), 1)


def _assert_same_options(db, history, export, running_ids, now):
    """两页选项：顺序/ID 一致；文本分别等于两种 full_date 调用，且只差日期格式。"""
    hist = _options(history)
    exp = _options(export)
    assert [tid for tid, _ in hist] == [tid for tid, _ in exp]
    for (tid, hist_text), (_, exp_text) in zip(hist, exp):
        task = db.get_task(tid)
        running = tid in running_ids
        assert hist_text == format_task_option_text(task, running, now)
        assert exp_text == format_task_option_text(task, running, now, full_date=True)
        assert exp_text == _short_to_full(hist_text, task)
        assert exp_text != hist_text
        assert ("运行中" in hist_text) == ("运行中" in exp_text) == running
    return hist, exp


def test_options_order_ids_and_text_match(
        db, tasks, history, export, running_ids, clock):
    """首次载入：两页列同一批任务、同一顺序，文本只差日期格式，具体文案钉死。"""
    hist, exp = _assert_same_options(db, history, export, running_ids, clock.now())

    assert [tid for tid, _ in hist] == tasks.order
    assert tasks.nodata.task_id not in [tid for tid, _ in exp]

    assert [text for _, text in hist] == [
        "running.exe · PID 1011 · 07-16 23:00 · 运行中 5分钟",
        "noted.exe · PID 1009 · 07-16 21:00 · 42分钟 · 建模 200 次回归测试，连续运…",
        "plain.exe · PID 1009 · 12-31 23:30 · 2小时15分",
    ]
    assert [text for _, text in exp] == [
        "running.exe · PID 1011 · 2026-07-16 23:00 · 运行中 5分钟",
        "noted.exe · PID 1009 · 2026-07-16 21:00 · 42分钟 · 建模 200 次回归测试，连续运…",
        "plain.exe · PID 1009 · 2025-12-31 23:30 · 2小时15分",
    ]


def test_default_selection_is_newest_on_both_pages(db, tasks, history, export, clock):
    """首次载入两页都默认选中第 0 项（最新的有数据任务），而不是更新但无数据的任务。"""
    assert history.task_combo.currentIndex() == export.task_combo.currentIndex() == 0
    assert history.current_task_id == export.current_task_id == tasks.running.task_id
    # 历史页把当前项的完整信息放在下拉框 tooltip 上（导出页没有这一项）
    assert history.task_combo.toolTip() == format_task_tooltip(
        db.get_task(tasks.running.task_id), True, clock.now())


def test_running_duration_refresh_keeps_pages_in_sync(
        db, tasks, history, export, running_ids, clock):
    """
    运行中任务的时长随时钟原地刷新（不重建列表、不改选择），两页保持同口径；
    任务结束后两页都换成最终时长、去掉"运行中"。
    """
    before_ids = [tid for tid, _ in _options(history)]

    clock.set(BASE + timedelta(hours=3, minutes=30))
    history._refresh_task_option_texts()
    export._refresh_running_tasks()
    hist, exp = _assert_same_options(db, history, export, running_ids, clock.now())
    assert hist[0][1].endswith("运行中 30分钟")
    assert exp[0][1].endswith("运行中 30分钟")
    assert [tid for tid, _ in hist] == before_ids
    assert history.current_task_id == export.current_task_id == tasks.running.task_id

    # 任务结束：数据库写入结束时间，任务状态服务也不再视其为运行中
    db.update_task_status(tasks.running.task_id, "stopped",
                          BASE + timedelta(hours=3, minutes=40))
    running_ids.clear()
    clock.set(BASE + timedelta(hours=6))
    history._refresh_task_option_texts()
    export._refresh_running_tasks()
    hist, exp = _assert_same_options(db, history, export, running_ids, clock.now())
    assert hist[0][1] == "running.exe · PID 1011 · 07-16 23:00 · 40分钟"
    assert exp[0][1] == "running.exe · PID 1011 · 2026-07-16 23:00 · 40分钟"

    # 结束后再推进时钟：两页的最终时长都不再变化
    clock.set(BASE + timedelta(hours=9))
    history._refresh_task_option_texts()
    export._refresh_running_tasks()
    assert _options(history) == hist
    assert _options(export) == exp


def test_user_choice_survives_reload_on_both_pages(
        db, tasks, history, export, running_ids, clock):
    """用户在两页都手动选中第 3 项（plain）后重新载入列表：两页都保持该选择，文案仍同口径。"""
    history.task_combo.setCurrentIndex(2)
    export.task_combo.setCurrentIndex(2)
    assert history.current_task_id == export.current_task_id == tasks.plain.task_id

    history._load_tasks()
    export._load_tasks()

    assert history.current_task_id == export.current_task_id == tasks.plain.task_id
    assert history.task_combo.currentIndex() == export.task_combo.currentIndex() == 2
    _assert_same_options(db, history, export, running_ids, clock.now())
