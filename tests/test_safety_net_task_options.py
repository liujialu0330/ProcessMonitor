"""
安全网（特性测试）：分析页任务下拉 ↔ TaskOptionsModel 直接输出同口径。

历史页与导出页合并为分析页后，下拉只剩一个消费者；这里锁住"页面看到的选项就是
TaskOptionsModel 对同一批任务、同一运行中集合、同一时刻的输出"，任何在页面或视图
模型里另行拼文案、改顺序、改运行中判定的漂移都应变红：
- 同一任务集合（运行中、已结束、带长备注、跨年、无数据）下，选项顺序与 task_id 一致：
  只列有数据的任务，按开始时间倒序；文本与 tooltip 逐字等于模型输出（具体文案钉死）；
- 运行中任务的时长原地刷新后仍与模型一致；任务结束后改为最终时长，之后不再变化；
- 首次载入默认选最新（第 0 项）；用户手动选中的任务在重新载入后保持；
- 对比任务下拉同口径（候选 = 除当前任务外的全部任务）。

"运行中"以任务状态服务为准（FakeTaskStatus），与数据库 status 列无关；这里让它与
数据库状态一致后只比较文案口径。
"""
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock, FakeTaskStatus
from tests.factories import make_analysis_page, make_services
from tests.helpers import SyncHistoryRunner
from ui.formatting import format_task_tooltip
from ui.models.task_options import TaskOptionsModel
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
    return FakeClock(BASE + timedelta(hours=3, minutes=5))


@pytest.fixture
def tasks(db):
    """
    任务集合（按开始时间倒序即期望的下拉顺序）：
    - running：最新的有数据任务，运行中（数据库 status=running、无结束时间）；
    - noted：已结束，带超长备注（下拉截断、tooltip 完整）；
    - plain：已结束，去年开始（跨年）；
    - nodata：比所有任务都新但没有采样，不应列出，也不应被默认选中。
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
    return FakeTaskStatus({tasks.running.task_id})


@pytest.fixture
def running_ids(task_status):
    """运行中任务 ID 集合：就是 task_status 内部的集合，增删即影响页面视角"""
    return task_status.running


@pytest.fixture
def analysis(qapp, db, clock, task_status):
    services = make_services(clock=clock, task_status=task_status)
    widget = make_analysis_page(db, MonitorManager(db=db), services,
                                worker_runner=SyncHistoryRunner(db), defer=lambda fn: fn())
    widget.vm.reload_tasks()
    yield widget
    widget.auto_refresh.stop()
    widget.close()


def _page_options(selector):
    """下拉项 (task_id, 显示文本)：经 TaskSelector 公开接口读。"""
    return [(selector.task_id_at(i), selector.item_text(i)) for i in range(selector.count())]


def _model_options(db, task_ids, running_ids, now):
    """同一批任务（取库里最新记录）交给 TaskOptionsModel 的直接输出。"""
    model = TaskOptionsModel()
    return model.rebuild([db.get_task(tid) for tid in task_ids], set(running_ids), now)


def _assert_same_as_model(db, page, running_ids, now):
    shown = _page_options(page.task_selector)
    expected = _model_options(db, [tid for tid, _ in shown], running_ids, now)
    assert shown == [(o.task_id, o.text) for o in expected]
    return shown


def test_options_order_ids_and_text_match_model(db, tasks, analysis, running_ids, clock):
    """首次载入：只列有数据的任务、开始时间倒序，文本逐字等于模型输出（文案钉死）。"""
    shown = _assert_same_as_model(db, analysis, running_ids, clock.now())
    assert [tid for tid, _ in shown] == tasks.order
    assert tasks.nodata.task_id not in [tid for tid, _ in shown]
    assert [text for _, text in shown] == [
        "running.exe · PID 1011 · 07-16 23:00 · 运行中 5分钟",
        "noted.exe · PID 1009 · 07-16 21:00 · 42分钟 · 建模 200 次回归测试，连续运…",
        "plain.exe · PID 1009 · 12-31 23:30 · 2小时15分",
    ]


def test_default_selection_and_tooltip(db, tasks, analysis, clock):
    """默认选中第 0 项（最新的有数据任务），tooltip 是该任务的完整信息。"""
    assert analysis.task_selector.current_index() == 0
    assert analysis.current_task_id == tasks.running.task_id
    assert analysis.task_selector.tooltip() == format_task_tooltip(
        db.get_task(tasks.running.task_id), True, clock.now())


def test_running_duration_refresh_matches_model(db, tasks, analysis, running_ids, clock):
    """运行中时长随时钟原地刷新（不重建列表、不改选择）；结束后换成最终时长且不再变化。"""
    before_ids = [tid for tid, _ in _page_options(analysis.task_selector)]

    clock.set(BASE + timedelta(hours=3, minutes=30))
    analysis.auto_refresh.tick()
    shown = _assert_same_as_model(db, analysis, running_ids, clock.now())
    assert shown[0][1].endswith("运行中 30分钟")
    assert [tid for tid, _ in shown] == before_ids
    assert analysis.current_task_id == tasks.running.task_id

    db.update_task_status(tasks.running.task_id, "stopped",
                          BASE + timedelta(hours=3, minutes=40))
    running_ids.clear()
    clock.set(BASE + timedelta(hours=6))
    analysis.auto_refresh.tick()
    shown = _assert_same_as_model(db, analysis, running_ids, clock.now())
    assert shown[0][1] == "running.exe · PID 1011 · 07-16 23:00 · 40分钟"

    clock.set(BASE + timedelta(hours=9))
    analysis.auto_refresh.tick()
    assert _page_options(analysis.task_selector) == shown


def test_user_choice_survives_reload(db, tasks, analysis, running_ids, clock):
    """用户手动选中第 3 项（plain）后重新载入列表：保持该选择，文案仍与模型一致。"""
    analysis.task_selector.combo.setCurrentIndex(2)
    assert analysis.current_task_id == tasks.plain.task_id

    analysis.vm.reload_tasks()

    assert analysis.current_task_id == tasks.plain.task_id
    assert analysis.task_selector.current_index() == 2
    _assert_same_as_model(db, analysis, running_ids, clock.now())


def test_compare_options_match_model(db, tasks, analysis, running_ids, clock):
    """对比任务下拉：候选为除当前任务外的全部任务，文本同样是模型的直接输出。"""
    analysis.toolbar.compare_button.setChecked(True)
    shown = _page_options(analysis.compare_panel.selector)
    expected = _model_options(db, [tasks.noted.task_id, tasks.plain.task_id],
                              running_ids, clock.now())
    assert shown == [(o.task_id, o.text) for o in expected]
