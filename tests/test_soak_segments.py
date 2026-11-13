"""
重启后的分段统计：判定与区间表只用最后一次启动之后的数据。

数据构造：每个启动段内部平稳，但第二段的起点水平比第一段高得多。把两段连起来看是一条
陡峭的阶梯（拟合会判"持续上涨"）；按启动段切开后各自平稳，不应判持续上涨。
"""
import uuid
from datetime import datetime, timedelta

from core.analysis.trend import Verdict
from core.history_query import build_request, run_history_query, segment_note, segment_of
from data.models import DataPoint, Marker, MonitorTask
from utils.metrics import MetricType

MEM = MetricType.MEMORY_PRIVATE
BASE = datetime(2026, 11, 6, 9, 0, 0)
INTERVAL = 30.0
POINTS_PER_SEGMENT = 40     # 40 点、跨度约 20 分钟：满足判定所需的最小点数与跨度
LOW = 100 * 1024.0 * 1024
HIGH = 500 * 1024.0 * 1024


def _flat(level: float, i: int) -> float:
    # 极小的交替抖动，避免值域为零；相对水平可以忽略
    return level * (1.0 + 0.0005 * (i % 2))


def _seed(db, restarts=1):
    """restarts 个重启：共 restarts+1 段，每段平稳，段与段之间水平阶梯上升。"""
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=100, process_name='app.exe',
                       metric_types=[MEM], interval=INTERVAL, start_time=BASE,
                       end_time=None, status='running')
    db.save_task(task)
    points = []
    restart_times = []
    cursor = BASE
    for segment in range(restarts + 1):
        level = LOW if segment == 0 else HIGH * segment
        if segment:
            # 重启打点是发现新进程的时刻，早于该段第一个样本
            restart_times.append(cursor - timedelta(seconds=5))
        for i in range(POINTS_PER_SEGMENT):
            points.append(DataPoint(task.task_id, cursor, _flat(level, i), MEM))
            cursor += timedelta(seconds=INTERVAL)
        cursor += timedelta(seconds=60)          # 两个进程之间的空档
    db.save_data_points(points)
    return task, restart_times


def _query(db, task, **options):
    request = build_request(task.task_id, MEM, 'all', analysis=True, **options)
    return run_history_query(db, request)


def test_without_restart_marker_the_step_reads_as_rising(db):
    """对照：同一份数据不写重启打点时，三级阶梯被当成持续上涨——证明下面的用例确实依赖分段"""
    task, _times = _seed(db, restarts=2)
    snapshot = _query(db, task)
    assert snapshot.judgement.verdict is Verdict.RISING
    assert snapshot.segment_count == 1
    assert segment_note(snapshot) == ""


def test_verdict_uses_only_last_start_segment(db):
    task, times = _seed(db)
    db.markers.add(task.task_id, times[0], "第 2 次启动 PID 200", 'restart')
    snapshot = _query(db, task)

    assert snapshot.segment_number == 2
    assert snapshot.segment_count == 2
    assert snapshot.segment_start == times[0]
    assert snapshot.judgement.verdict is not Verdict.RISING
    assert snapshot.judgement.verdict is Verdict.STABLE
    # 统计窗口从第二段开始：样本数只算最后一段，最小值是第二段的水平
    assert snapshot.stats_since == times[0]
    assert snapshot.stats['count'] == POINTS_PER_SEGMENT
    assert snapshot.stats['min'] >= HIGH
    assert segment_note(snapshot) == "仅统计第 2 次启动后的数据"


def test_three_starts_use_only_the_third_segment(db):
    task, times = _seed(db, restarts=2)
    for n, when in enumerate(times, start=2):
        db.markers.add(task.task_id, when, f"第 {n} 次启动 PID {100 * n}", 'restart')
    snapshot = _query(db, task)
    assert (snapshot.segment_number, snapshot.segment_count) == (3, 3)
    assert snapshot.stats_since == times[1]
    assert snapshot.stats['count'] == POINTS_PER_SEGMENT
    assert snapshot.judgement.verdict is Verdict.STABLE
    assert segment_note(snapshot) == "仅统计第 3 次启动后的数据"


def test_interval_table_starts_at_last_segment(db):
    """区间表的第一段从最后一次启动起，不含上一个进程的样本"""
    task, times = _seed(db)
    db.markers.add(task.task_id, times[0], "第 2 次启动 PID 200", 'restart')
    snapshot = _query(db, task)
    assert snapshot.intervals
    assert snapshot.intervals[0].start == times[0]
    assert sum(i.sample_count for i in snapshot.intervals) == POINTS_PER_SEGMENT
    assert snapshot.intervals[0].start_value >= HIGH


def test_crash_and_recover_markers_do_not_cut_a_segment(db):
    """崩溃与续采打点不是新的启动段：只有 restart 打点切段"""
    task, times = _seed(db)
    db.markers.add(task.task_id, times[0] - timedelta(seconds=30), "异常退出 0xC0000005（访问冲突）", 'crash')
    db.markers.add(task.task_id, BASE + timedelta(minutes=5), "工具重启后继续监控", 'recover')
    snapshot = _query(db, task)
    assert snapshot.segment_count == 1
    assert snapshot.stats['count'] == 2 * POINTS_PER_SEGMENT


def test_segment_of_respects_until():
    markers = [Marker('t', BASE + timedelta(minutes=10), "第 2 次启动", kind='restart'),
               Marker('t', BASE + timedelta(minutes=30), "第 3 次启动", kind='restart'),
               Marker('t', BASE + timedelta(minutes=20), "x", kind='crash')]
    assert segment_of([]) == (None, 1, 1)
    assert segment_of(markers) == (BASE + timedelta(minutes=30), 3, 3)
    # 自定义范围终点在第三次启动之前：只算到第二段
    assert segment_of(markers, BASE + timedelta(minutes=25)) == (
        BASE + timedelta(minutes=10), 2, 3)
    assert segment_of(markers, BASE + timedelta(minutes=5)) == (None, 1, 3)
