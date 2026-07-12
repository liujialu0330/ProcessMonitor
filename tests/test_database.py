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


# ========== v1.3.0 批2：since_iso 时间过滤 / 统计 / 最新时间戳 ==========
# 评审修订 B1 是本批最高优先级事实：data_points.timestamp 在库里是 ISO TEXT，
# SQL 过滤参数必须用 ISO 字符串。以下用例均显式断言"过滤后行数 < 全量行数"，
# 防止重犯"float 过滤恒真、时间过滤静默失效"的回归（opus 评审已实测过该缺陷）。

def test_data_points_since_filter(db):
    """since_iso 只返回该时间及之后的点（取第6个点的 ISO 字符串），且行数必须收窄"""
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(10)
    ]
    db.save_data_points(points)

    since_iso = points[5].timestamp.isoformat()  # 第6个点（i=5）
    result = db.get_task_data_points(task.task_id, metric_type="memory_rss", since_iso=since_iso)

    assert len(result) == 5  # i=5..9
    assert len(result) < len(points), "过滤后行数必须收窄，否则时间过滤静默失效（B1 回归）"
    assert [p.value for p in result] == [5.0, 6.0, 7.0, 8.0, 9.0]
    assert result[0].timestamp < result[1].timestamp < result[-1].timestamp


def test_data_points_since_with_limit(db):
    """since 范围内 5 条、limit=3 时取"范围内最近 3 条"，仍按时间升序"""
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(10)
    ]
    db.save_data_points(points)

    since_iso = points[5].timestamp.isoformat()
    result = db.get_task_data_points(
        task.task_id, metric_type="memory_rss", limit=3, since_iso=since_iso)

    assert len(result) == 3
    assert [p.value for p in result] == [7.0, 8.0, 9.0]
    assert result[0].timestamp < result[1].timestamp < result[2].timestamp


def test_bucketed_since_filter(db):
    """分桶查询带 since 只对范围内数据分桶，行数（等价于覆盖的原始点数）必须收窄"""
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(20)
    ]
    db.save_data_points(points)

    full = db.get_task_data_points_bucketed(task.task_id, metric_type="memory_rss", max_buckets=2000)
    since_iso = points[15].timestamp.isoformat()
    filtered = db.get_task_data_points_bucketed(
        task.task_id, metric_type="memory_rss", max_buckets=2000, since_iso=since_iso)

    assert len(full) == 20
    assert len(filtered) == 5  # i=15..19，未超过 max_buckets，逐行成桶等价于全量返回
    assert len(filtered) < len(full), "过滤后行数必须收窄，否则时间过滤静默失效（B1 回归）"
    assert min(p.value for p in filtered) == 15.0
    assert max(p.value for p in filtered) == 19.0
    timestamps = [p.timestamp for p in filtered]
    assert timestamps == sorted(timestamps)


def test_metric_stats_basic(db):
    """已知数据集校验 count/min/max/avg 精确值（不含 last，评审修订 M3）"""
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=v, metric_type="memory_rss")
        for i, v in enumerate(values)
    ]
    db.save_data_points(points)

    stats = db.get_metric_stats(task.task_id, "memory_rss")

    assert stats is not None
    assert 'last' not in stats
    assert stats['count'] == 5
    assert stats['min'] == 10.0
    assert stats['max'] == 50.0
    assert stats['avg'] == 30.0


def test_metric_stats_since_filter_narrows_range(db):
    """stats 的 since_iso 过滤同样必须收窄，覆盖 get_metric_stats 自身的 B1 回归风险"""
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(10)
    ]
    db.save_data_points(points)

    full_stats = db.get_metric_stats(task.task_id, "memory_rss")
    since_iso = points[5].timestamp.isoformat()
    filtered_stats = db.get_metric_stats(task.task_id, "memory_rss", since_iso=since_iso)

    assert full_stats['count'] == 10
    assert filtered_stats['count'] == 5
    assert filtered_stats['count'] < full_stats['count'], "过滤后行数必须收窄（B1 回归）"
    assert filtered_stats['min'] == 5.0
    assert filtered_stats['max'] == 9.0


def test_metric_stats_empty_returns_none(db):
    """任务无数据（或过滤后范围内无数据）时返回 None，而不是 count=0 的字典"""
    task = _make_task()
    db.save_task(task)

    assert db.get_metric_stats(task.task_id, "memory_rss") is None

    # 有数据但 since_iso 晚于全部数据点时，范围内同样无数据
    db.save_data_points([
        DataPoint(task_id=task.task_id, timestamp=datetime(2026, 1, 1),
                  value=1.0, metric_type="memory_rss"),
    ])
    future_iso = datetime(2099, 1, 1).isoformat()
    assert db.get_metric_stats(task.task_id, "memory_rss", since_iso=future_iso) is None


def test_last_point_timestamp(db):
    """返回该任务全部指标里最新一条数据点的时间戳（跨指标取 MAX，不局限于单指标）"""
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    db.save_data_points([
        DataPoint(task_id=task.task_id, timestamp=base, value=1.0, metric_type="memory_rss"),
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=5),
                  value=2.0, metric_type="cpu_percent"),
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=2),
                  value=3.0, metric_type="memory_rss"),
    ])

    last_dt = db.get_last_point_timestamp(task.task_id)

    assert last_dt == base + timedelta(seconds=5)
    assert isinstance(last_dt, datetime)


def test_last_point_timestamp_empty_none(db):
    """任务无数据点时返回 None"""
    task = _make_task()
    db.save_task(task)

    assert db.get_last_point_timestamp(task.task_id) is None


# ========== v1.3.0 批4：数据库维护（get_db_size_bytes / vacuum） ==========

def test_db_size_positive(db):
    """写入数据后总占用字节数（db 主文件 + -wal/-shm 边车文件求和）应 > 0"""
    task = _make_task()
    db.save_task(task)
    db.save_data_points([
        DataPoint(task_id=task.task_id, timestamp=datetime.now(),
                  value=1.0, metric_type="memory_rss"),
    ])

    assert db.get_db_size_bytes() > 0


def test_vacuum_runs_and_shrinks_or_noop(db):
    """删除大量行后 vacuum 不报错、且总占用不增大（不强断言一定缩小，避免对
    SQLite 内部页面回收策略做过强假设——但已用临时脚本实测同等规模数据确实
    会缩小，这里保留宽松断言以降低对实现细节的耦合）"""
    task = _make_task()
    db.save_task(task)

    base = datetime(2026, 1, 1)
    points = [
        DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(5000)
    ]
    for i in range(0, len(points), 2000):
        db.save_data_points(points[i:i + 2000])

    db.delete_task(task.task_id)  # 只留下空壳：已删除行占用的页尚未回收

    size_before = db.get_db_size_bytes()
    db.vacuum()
    size_after = db.get_db_size_bytes()

    assert size_after <= size_before
