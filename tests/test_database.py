"""
Database 基础存取用例
平移自 data/database.py 原 `__main__` 冒烟块，改为 tmp_path 临时库注入
"""
import uuid
from datetime import datetime, timedelta

from data.models import MonitorTask, DataPoint


def _make_task(**overrides) -> MonitorTask:
    """构造一个多指标任务，供各用例复用"""
    defaults = dict(
        task_id=str(uuid.uuid4()),
        pid=1234,
        process_name="test.exe",
        metric_types=["memory_rss", "cpu_percent"],
        interval=1.0,
        start_time=datetime.now(),
        end_time=None,
        status="running",
    )
    defaults.update(overrides)
    return MonitorTask(**defaults)


def test_save_and_get_task(db):
    """保存任务后可按 task_id 取回，多指标列表原样回读"""
    task = _make_task()
    assert db.save_task(task) is True

    retrieved = db.get_task(task.task_id)
    assert retrieved is not None
    assert retrieved.task_id == task.task_id
    assert retrieved.process_name == task.process_name
    assert retrieved.metric_types == ["memory_rss", "cpu_percent"], "多指标存取回读不一致"


def test_save_and_filter_multi_metric_data_points(db):
    """同一采集周期共用一个时间戳；按指标过滤取回条数与内容正确"""
    task = _make_task()
    db.save_task(task)

    ts1 = datetime.now()
    ts2 = ts1 + timedelta(seconds=1)
    data_points = [
        DataPoint(task_id=task.task_id, timestamp=ts1, value=100.5, metric_type="memory_rss"),
        DataPoint(task_id=task.task_id, timestamp=ts1, value=12.3, metric_type="cpu_percent"),
        DataPoint(task_id=task.task_id, timestamp=ts2, value=101.2, metric_type="memory_rss"),
        DataPoint(task_id=task.task_id, timestamp=ts2, value=15.6, metric_type="cpu_percent"),
    ]
    assert db.save_data_points(data_points) is True

    all_points = db.get_task_data_points(task.task_id)
    mem_points = db.get_task_data_points(task.task_id, metric_type="memory_rss")
    cpu_points = db.get_task_data_points(task.task_id, metric_type="cpu_percent")

    assert len(all_points) == 4
    assert len(mem_points) == 2
    assert len(cpu_points) == 2
    for dp in mem_points:
        assert dp.metric_type == "memory_rss"


def test_sample_count_counts_distinct_timestamps(db):
    """采集次数口径：COUNT DISTINCT timestamp，同一时间戳多指标算一次采集"""
    task = _make_task()
    db.save_task(task)

    ts1 = datetime.now()
    ts2 = ts1 + timedelta(seconds=1)
    data_points = [
        DataPoint(task_id=task.task_id, timestamp=ts1, value=100.5, metric_type="memory_rss"),
        DataPoint(task_id=task.task_id, timestamp=ts1, value=12.3, metric_type="cpu_percent"),
        DataPoint(task_id=task.task_id, timestamp=ts2, value=101.2, metric_type="memory_rss"),
        DataPoint(task_id=task.task_id, timestamp=ts2, value=15.6, metric_type="cpu_percent"),
    ]
    db.save_data_points(data_points)

    sample_count = db.get_sample_count(task.task_id)
    point_count = db.get_data_point_count(task.task_id)

    assert sample_count == 2
    assert point_count == 4


def test_update_task_status(db):
    """更新任务状态与结束时间后可回读"""
    task = _make_task()
    db.save_task(task)

    end_time = datetime.now()
    assert db.update_task_status(task.task_id, "stopped", end_time) is True

    updated = db.get_task(task.task_id)
    assert updated.status == "stopped"
    assert updated.end_time is not None


def test_get_all_tasks(db):
    """新库保存多个任务后 get_all_tasks 数量匹配"""
    for _ in range(3):
        db.save_task(_make_task(task_id=str(uuid.uuid4())))

    all_tasks = db.get_all_tasks()
    assert len(all_tasks) == 3


def test_delete_task_removes_task_and_data_points(db):
    """delete_task 同时清空任务与数据点"""
    task = _make_task()
    db.save_task(task)
    ts = datetime.now()
    db.save_data_points([
        DataPoint(task_id=task.task_id, timestamp=ts, value=1.0, metric_type="memory_rss"),
    ])

    assert db.delete_task(task.task_id) is True
    assert db.get_task(task.task_id) is None
    assert db.get_task_data_points(task.task_id) == []


def test_reconcile_orphan_tasks_marks_running_as_stopped(db):
    """孤儿任务校正：running 状态任务被置为 stopped，end_time 为空时补当前时间"""
    running_no_end = _make_task(status="running", end_time=None)
    running_with_end = _make_task(status="running", end_time=datetime(2026, 1, 1))
    already_stopped = _make_task(status="stopped", end_time=datetime(2026, 1, 1))
    db.save_task(running_no_end)
    db.save_task(running_with_end)
    db.save_task(already_stopped)

    count = db.reconcile_orphan_tasks()

    assert count == 2  # 两条 running 任务被校正，已 stopped 的不受影响

    t1 = db.get_task(running_no_end.task_id)
    assert t1.status == "stopped"
    assert t1.end_time is not None

    t2 = db.get_task(running_with_end.task_id)
    assert t2.status == "stopped"
    assert t2.end_time == datetime(2026, 1, 1)  # 原有 end_time 不被覆盖

    t3 = db.get_task(already_stopped.task_id)
    assert t3.status == "stopped"
    assert t3.end_time == datetime(2026, 1, 1)


def test_reconcile_orphan_tasks_no_running_tasks_returns_zero(db):
    """无 running 任务时返回 0，不报错"""
    db.save_task(_make_task(status="stopped", end_time=datetime.now()))
    assert db.reconcile_orphan_tasks() == 0


def test_single_data_point_save(db):
    """save_data_point 单条保存路径"""
    task = _make_task()
    db.save_task(task)
    dp = DataPoint(task_id=task.task_id, timestamp=datetime.now(), value=42.0, metric_type="memory_rss")

    assert db.save_data_point(dp) is True
    points = db.get_task_data_points(task.task_id)
    assert len(points) == 1
    assert points[0].value == 42.0


def test_get_task_data_points_limit_returns_recent_n_ascending(db):
    """
    v1.2.0 批3 变更：limit 分支改为子查询"最近 N 条且按时间升序"（不再是 DESC）。
    历史页依赖该语义直接顺序填表/绘图，不再自行 reversed()。
    """
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(10)
    ]
    db.save_data_points(points)

    recent = db.get_task_data_points(task.task_id, metric_type="memory_rss", limit=3)

    assert len(recent) == 3
    # 最近3条为 i=7,8,9，且按时间升序排列（不是 DESC）
    assert [p.value for p in recent] == [7.0, 8.0, 9.0]
    assert recent[0].timestamp < recent[1].timestamp < recent[2].timestamp


def test_get_task_data_points_limit_larger_than_data_returns_all_ascending(db):
    """limit 大于实际数据量时返回全部数据，仍按升序"""
    task = _make_task()
    db.save_task(task)
    base = datetime(2026, 1, 1)
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(5)
    ]
    db.save_data_points(points)

    recent = db.get_task_data_points(task.task_id, metric_type="memory_rss", limit=100)
    assert [p.value for p in recent] == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_get_task_data_points_bucketed_bounds_and_spike_preserved(db):
    """构造已知锯齿波+尖峰数据，断言分桶输出 <=2*max_buckets、保留全局极值、按时间有序"""
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    n = 10000
    spike_index = n // 2
    points = []
    for i in range(n):
        value = float(i % 10)
        if i == spike_index:
            value = 99999.0  # 尖峰：必须被分桶 MIN/MAX 保留，不能被平滑掉
        points.append(DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                                 value=value, metric_type="memory_rss"))

    # 分批写入，避免单次 executemany 参数过多
    for i in range(0, n, 2000):
        db.save_data_points(points[i:i + 2000])

    max_buckets = 500
    bucketed = db.get_task_data_points_bucketed(
        task.task_id, metric_type="memory_rss", max_buckets=max_buckets)

    assert 0 < len(bucketed) <= 2 * max_buckets
    values = [p.value for p in bucketed]
    assert max(values) == 99999.0  # 全局最大值（尖峰）被保留
    assert min(values) == 0.0      # 全局最小值被保留

    timestamps = [p.timestamp for p in bucketed]
    assert timestamps == sorted(timestamps)  # 按时间升序


def test_get_task_data_points_bucketed_small_dataset_returns_all_points(db):
    """数据量小于 max_buckets 时每行自成一桶，等价于返回全部数据（去重后升序）"""
    task = _make_task()
    db.save_task(task)
    base = datetime(2026, 1, 1)
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(10)
    ]
    db.save_data_points(points)

    bucketed = db.get_task_data_points_bucketed(
        task.task_id, metric_type="memory_rss", max_buckets=2000)

    assert [p.value for p in bucketed] == [float(i) for i in range(10)]


def test_cleanup_old_tasks_disabled_when_retention_zero(db):
    """retention_days<=0 视为禁用，不做任何删除"""
    task = _make_task(status="stopped", end_time=datetime(2000, 1, 1))
    db.save_task(task)

    assert db.cleanup_old_tasks(0) == 0
    assert db.get_task(task.task_id) is not None


def test_cleanup_old_tasks_deletes_expired_stopped_only(db):
    """过期且已停止的任务被删除；未过期、running 状态的任务不受影响"""
    old_stopped = _make_task(status="stopped", end_time=datetime.now() - timedelta(days=40))
    recent_stopped = _make_task(status="stopped", end_time=datetime.now() - timedelta(days=1))
    # running 任务即使 start_time 很老也不能被清理（清理只看 stopped）
    old_running = _make_task(status="running", end_time=None,
                              start_time=datetime.now() - timedelta(days=40))
    db.save_task(old_stopped)
    db.save_task(recent_stopped)
    db.save_task(old_running)
    db.save_data_points([
        DataPoint(task_id=old_stopped.task_id, timestamp=datetime.now(),
                  value=1.0, metric_type="memory_rss"),
    ])

    deleted = db.cleanup_old_tasks(retention_days=30)

    assert deleted == 1
    assert db.get_task(old_stopped.task_id) is None
    assert db.get_task_data_points(old_stopped.task_id) == []
    assert db.get_task(recent_stopped.task_id) is not None
    assert db.get_task(old_running.task_id) is not None


def test_cleanup_old_tasks_uses_start_time_when_end_time_null(db):
    """stopped 但 end_time 为 NULL 的老数据（迁移遗留场景）用 start_time 兜底判断过期"""
    task = _make_task(status="stopped", end_time=None,
                       start_time=datetime.now() - timedelta(days=40))
    db.save_task(task)

    deleted = db.cleanup_old_tasks(retention_days=30)

    assert deleted == 1
    assert db.get_task(task.task_id) is None
