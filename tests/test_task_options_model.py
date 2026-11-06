"""
TaskOptionsModel（纯单元，不建任何控件）：
- 文本/提示与 ui.formatting 的 format_task_option_text / format_task_tooltip 逐字一致；
- 顺序与传入任务一致；index_of / option / task 查询；
- refresh_running 只刷新运行中项、返回变化下标；任务结束后换最终时长且不再变化。
"""
from datetime import datetime, timedelta

from data.models import MonitorTask
from ui.formatting import format_task_option_text, format_task_tooltip
from ui.models.task_options import TaskOption, TaskOptionsModel
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 20, 0, 0)
NOW = BASE + timedelta(hours=3, minutes=5)


def _task(task_id, start, *, end=None, note="", name=None, last_sample_at=None):
    return MonitorTask(
        task_id=task_id, pid=1000, process_name=name or f"{task_id}.exe",
        metric_types=[MetricType.MEMORY_PRIVATE], interval=1.0, start_time=start,
        end_time=end, status="running" if end is None else "stopped", note=note,
        last_sample_at=last_sample_at)


def _tasks():
    return [
        _task("run", BASE + timedelta(hours=3)),
        _task("noted", BASE + timedelta(hours=1), end=BASE + timedelta(hours=1, minutes=42),
              note="建模 200 次回归测试，连续运行过夜观察内存增长"),
        _task("plain", datetime(2025, 12, 31, 23, 30), end=datetime(2026, 1, 1, 1, 45)),
    ]


def test_text_and_tooltip_match_formatting():
    tasks = _tasks()
    for full_date in (False, True):
        model = TaskOptionsModel(full_date=full_date)
        options = model.rebuild(tasks, {"run"}, NOW)
        for task, option in zip(tasks, options):
            running = task.task_id == "run"
            assert option == TaskOption(
                task_id=task.task_id,
                text=format_task_option_text(task, running, NOW, full_date=full_date),
                tooltip=format_task_tooltip(task, running, NOW),
                running=running)


def test_pinned_texts():
    """两种日期格式的具体文案（与安全网测试同一组数据）。"""
    short = TaskOptionsModel().rebuild(_tasks(), {"run"}, NOW)
    full = TaskOptionsModel(full_date=True).rebuild(_tasks(), {"run"}, NOW)
    assert [o.text for o in short] == [
        "run.exe · PID 1000 · 07-16 23:00 · 运行中 5分钟",
        "noted.exe · PID 1000 · 07-16 21:00 · 42分钟 · 建模 200 次回归测试，连续运…",
        "plain.exe · PID 1000 · 12-31 23:30 · 2小时15分",
    ]
    assert full[2].text == "plain.exe · PID 1000 · 2025-12-31 23:30 · 2小时15分"
    assert "备注：建模 200 次回归测试，连续运行过夜观察内存增长" in short[1].tooltip


def test_order_and_lookup_follow_input():
    model = TaskOptionsModel()
    tasks = list(reversed(_tasks()))
    options = model.rebuild(tasks, set(), NOW)
    assert [o.task_id for o in options] == ["plain", "noted", "run"]
    assert len(model) == 3
    assert model.index_of("noted") == 1
    assert model.index_of("missing") == -1 and model.index_of(None) == -1
    assert model.option("run").task_id == "run"
    assert model.option("missing") is None
    assert model.task("plain") is tasks[0]
    # options() 返回新列表，改它不影响模型
    model.options().clear()
    assert len(model.options()) == 3


def test_running_follows_running_ids_not_db_status():
    """"运行中"只看传入的集合：库里 status=running 但不在集合中按已停止显示。"""
    orphan = _task("orphan", BASE, last_sample_at=BASE + timedelta(seconds=2))
    stopping = _task("stopping", BASE, end=BASE + timedelta(minutes=1))
    options = TaskOptionsModel().rebuild([orphan, stopping], {"stopping"}, NOW)
    assert not options[0].running and options[0].text.endswith("2秒")
    assert options[1].running and "运行中 3小时5分" in options[1].text


def test_refresh_running_returns_changed_indices():
    model = TaskOptionsModel()
    model.rebuild(_tasks(), {"run"}, NOW)
    assert model.tracked_running_ids() == ["run"]

    # 时钟没动：没有变化
    assert model.refresh_running(NOW, {"run"}, {}) == []
    # 时长增长：只有运行中项变化
    later = NOW + timedelta(minutes=25)
    assert model.refresh_running(later, {"run"}, {}) == [0]
    assert model.options()[0].text.endswith("运行中 30分钟")
    assert model.options()[1].text == format_task_option_text(_tasks()[1], False, NOW)


def test_refresh_after_task_ends_uses_final_duration_and_freezes():
    model = TaskOptionsModel(full_date=True)
    model.rebuild(_tasks(), {"run"}, NOW)
    ended = _task("run", BASE + timedelta(hours=3), end=BASE + timedelta(hours=3, minutes=40))

    changed = model.refresh_running(BASE + timedelta(hours=6), set(), {"run": ended})
    assert changed == [0]
    option = model.options()[0]
    assert option.text == "run.exe · PID 1000 · 2026-07-16 23:00 · 40分钟"
    assert not option.running and "状态：已结束" in option.tooltip
    assert model.task("run") is ended
    assert model.tracked_running_ids() == []

    # 结束后再推进时钟：不再刷新
    assert model.refresh_running(BASE + timedelta(hours=9), set(), {"run": ended}) == []
    assert model.options()[0] == option


def test_refresh_keeps_old_record_when_latest_missing():
    """取不到最新记录（缺失或 None）时沿用已有记录，不抛异常。"""
    model = TaskOptionsModel()
    model.rebuild(_tasks(), {"run"}, NOW)
    later = NOW + timedelta(minutes=10)
    assert model.refresh_running(later, {"run"}, {"run": None}) == [0]
    assert model.options()[0].text.endswith("运行中 15分钟")
    assert model.task("run").task_id == "run"


def test_rebuild_with_no_tasks():
    model = TaskOptionsModel()
    model.rebuild(_tasks(), {"run"}, NOW)
    assert model.rebuild([], (), NOW) == []
    assert len(model) == 0 and model.tracked_running_ids() == []
    assert model.refresh_running(NOW, set(), {}) == []
