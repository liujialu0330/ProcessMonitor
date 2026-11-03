"""
core/export.py 纯函数用例
覆盖 build_csv_header 与流式 pivot_rows 生成器
"""
from datetime import datetime, timedelta

from core.export import build_csv_header, pivot_rows
from data.models import MonitorTask, DataPoint


def _make_task(metric_types, process_name="test.exe", pid=1234):
    return MonitorTask(
        task_id="task-1",
        pid=pid,
        process_name=process_name,
        metric_types=metric_types,
        interval=1.0,
        start_time=datetime.now(),
        end_time=None,
        status="running",
    )


def test_build_csv_header_single_metric():
    task = _make_task(["memory_rss"])
    header = build_csv_header(task)
    assert header == ['时间', '进程名称', 'PID', '工作集内存(KB)']


def test_build_csv_header_multi_metric():
    task = _make_task(["memory_rss", "cpu_percent", "cpu_priority"])
    header = build_csv_header(task)
    # cpu_priority 无单位，不应带括号
    assert header == ['时间', '进程名称', 'PID', '工作集内存(KB)', 'CPU使用率(%)', '优先级']


def test_pivot_rows_multi_metric_alignment():
    """多指标透视对齐：同一时间戳的多条数据点合并为一行，按 metric_types 顺序排列"""
    task = _make_task(["memory_rss", "cpu_percent"])
    ts1 = datetime(2026, 1, 1, 0, 0, 0)
    ts2 = ts1 + timedelta(seconds=1)
    data_points = [
        DataPoint(task_id="task-1", timestamp=ts1, value=100.5, metric_type="memory_rss"),
        DataPoint(task_id="task-1", timestamp=ts1, value=12.3, metric_type="cpu_percent"),
        DataPoint(task_id="task-1", timestamp=ts2, value=101.25, metric_type="memory_rss"),
        DataPoint(task_id="task-1", timestamp=ts2, value=15.678, metric_type="cpu_percent"),
    ]

    rows = list(pivot_rows(task, data_points))

    assert rows == [
        ["2026-01-01 00:00:00", "test.exe", 1234, "100.5000", "12.3000"],
        ["2026-01-01 00:00:01", "test.exe", 1234, "101.2500", "15.6780"],
    ]


def test_pivot_rows_missing_metric_filled_empty():
    """缺失指标填空串"""
    task = _make_task(["memory_rss", "cpu_percent"])
    ts1 = datetime(2026, 1, 1, 0, 0, 0)
    data_points = [
        DataPoint(task_id="task-1", timestamp=ts1, value=100.5, metric_type="memory_rss"),
        # 该采集周期缺失 cpu_percent
    ]

    rows = list(pivot_rows(task, data_points))

    assert rows == [["2026-01-01 00:00:00", "test.exe", 1234, "100.5000", ""]]


def test_pivot_rows_value_format_four_decimals():
    """数值格式统一保留 4 位小数"""
    task = _make_task(["memory_rss"])
    ts1 = datetime(2026, 1, 1, 0, 0, 0)
    data_points = [
        DataPoint(task_id="task-1", timestamp=ts1, value=100, metric_type="memory_rss"),
    ]
    rows = list(pivot_rows(task, data_points))
    assert rows == [["2026-01-01 00:00:00", "test.exe", 1234, "100.0000"]]


def test_pivot_rows_null_and_empty_metric_type_fallback_to_first_metric():
    """NULL/空串 metric_type 兜底归入 metric_types[0]（旧数据兼容）"""
    task = _make_task(["memory_rss", "cpu_percent"])
    ts1 = datetime(2026, 1, 1, 0, 0, 0)
    data_points = [
        DataPoint(task_id="task-1", timestamp=ts1, value=100.0, metric_type=""),
        DataPoint(task_id="task-1", timestamp=ts1, value=12.0, metric_type="cpu_percent"),
    ]
    rows = list(pivot_rows(task, data_points))
    assert rows == [["2026-01-01 00:00:00", "test.exe", 1234, "100.0000", "12.0000"]]


def test_pivot_rows_none_metric_type_fallback_to_first_metric():
    """metric_type 显式为 None（旧库回读场景）同样兜底归入首指标"""
    task = _make_task(["memory_rss"])
    ts1 = datetime(2026, 1, 1, 0, 0, 0)
    data_points = [
        DataPoint(task_id="task-1", timestamp=ts1, value=100.0, metric_type=None),
    ]
    rows = list(pivot_rows(task, data_points))
    assert rows == [["2026-01-01 00:00:00", "test.exe", 1234, "100.0000"]]


def test_pivot_rows_multi_timestamp_grouping_and_last_group_flush():
    """多 timestamp 分组正确，且最后一组也会 flush（无需哨兵数据）"""
    task = _make_task(["memory_rss"])
    ts1 = datetime(2026, 1, 1, 0, 0, 0)
    ts2 = ts1 + timedelta(seconds=1)
    ts3 = ts1 + timedelta(seconds=2)
    data_points = [
        DataPoint(task_id="task-1", timestamp=ts1, value=1.0, metric_type="memory_rss"),
        DataPoint(task_id="task-1", timestamp=ts2, value=2.0, metric_type="memory_rss"),
        DataPoint(task_id="task-1", timestamp=ts3, value=3.0, metric_type="memory_rss"),
    ]
    rows = list(pivot_rows(task, data_points))
    assert len(rows) == 3
    assert rows[-1] == ["2026-01-01 00:00:02", "test.exe", 1234, "3.0000"]


def test_pivot_rows_empty_iterable_yields_no_rows():
    """空迭代器出 0 行"""
    task = _make_task(["memory_rss"])
    rows = list(pivot_rows(task, []))
    assert rows == []


def test_pivot_rows_single_metric_task_degrades_to_single_column():
    """旧单指标任务退化为单列"""
    task = _make_task(["memory_rss"])
    ts1 = datetime(2026, 1, 1, 0, 0, 0)
    data_points = [
        DataPoint(task_id="task-1", timestamp=ts1, value=55.5, metric_type="memory_rss"),
    ]
    rows = list(pivot_rows(task, data_points))
    assert rows == [["2026-01-01 00:00:00", "test.exe", 1234, "55.5000"]]


def test_pivot_rows_accepts_generator_input():
    """入参为生成器（流式）时同样正常工作"""
    task = _make_task(["memory_rss"])
    ts1 = datetime(2026, 1, 1, 0, 0, 0)

    def gen():
        yield DataPoint(task_id="task-1", timestamp=ts1, value=1.0, metric_type="memory_rss")

    rows = list(pivot_rows(task, gen()))
    assert rows == [["2026-01-01 00:00:00", "test.exe", 1234, "1.0000"]]


# ---------- 打点列 ----------

def _marker(ts, label):
    from data.models import Marker
    return Marker(task_id="task-1", timestamp=ts, label=label)


def test_header_marker_column_is_optional():
    task = _make_task(["memory_rss"])
    assert build_csv_header(task)[-1] == '工作集内存(KB)'
    assert build_csv_header(task, include_markers=True)[-1] == '打点'


def test_pivot_rows_without_markers_param_has_no_marker_column():
    """不传 markers 时行结构保持原样（向后兼容）"""
    task = _make_task(["memory_rss"])
    ts = datetime(2026, 1, 1, 0, 0, 0)
    rows = list(pivot_rows(task, [
        DataPoint(task_id="task-1", timestamp=ts, value=1.0, metric_type="memory_rss")]))
    assert len(rows[0]) == 4


def test_pivot_rows_marker_goes_to_first_row_at_or_after_marker_time():
    """打点写在时间不早于打点时刻的第一行；其余行该列为空"""
    task = _make_task(["memory_rss"])
    base = datetime(2026, 1, 1, 0, 0, 0)
    points = [
        DataPoint(task_id="task-1", timestamp=base + timedelta(seconds=i),
                  value=float(i), metric_type="memory_rss")
        for i in range(5)
    ]
    markers = [
        _marker(base + timedelta(seconds=1.5), "第1轮"),   # 落在 1s 与 2s 之间 -> 2s 行
        _marker(base + timedelta(seconds=3), "第2轮"),     # 恰好等于 3s 行时刻 -> 3s 行
    ]
    rows = list(pivot_rows(task, points, markers))
    assert [row[-1] for row in rows] == ["", "", "第1轮", "第2轮", ""]


def test_pivot_rows_multiple_markers_same_row_joined_and_sorted():
    task = _make_task(["memory_rss"])
    base = datetime(2026, 1, 1, 0, 0, 0)
    points = [
        DataPoint(task_id="task-1", timestamp=base + timedelta(seconds=i),
                  value=1.0, metric_type="memory_rss")
        for i in range(3)
    ]
    # 故意乱序传入
    markers = [
        _marker(base + timedelta(seconds=1.8), "B"),
        _marker(base + timedelta(seconds=1.2), "A"),
    ]
    rows = list(pivot_rows(task, points, markers))
    assert rows[2][-1] == "A; B"
    assert rows[0][-1] == rows[1][-1] == ""


def test_pivot_rows_marker_after_last_sample_attaches_to_last_row():
    """晚于最后一次采集的打点并入最后一行，不丢信息"""
    task = _make_task(["memory_rss"])
    base = datetime(2026, 1, 1, 0, 0, 0)
    points = [
        DataPoint(task_id="task-1", timestamp=base + timedelta(seconds=i),
                  value=1.0, metric_type="memory_rss")
        for i in range(2)
    ]
    rows = list(pivot_rows(task, points, [_marker(base + timedelta(seconds=30), "收尾")]))
    assert [row[-1] for row in rows] == ["", "收尾"]


def test_pivot_rows_marker_before_first_sample_attaches_to_first_row():
    task = _make_task(["memory_rss"])
    base = datetime(2026, 1, 1, 0, 0, 5)
    points = [DataPoint(task_id="task-1", timestamp=base, value=1.0, metric_type="memory_rss")]
    rows = list(pivot_rows(task, points, [_marker(base - timedelta(seconds=3), "早")]))
    assert rows[0][-1] == "早"


def test_pivot_rows_empty_markers_list_gives_empty_column():
    task = _make_task(["memory_rss"])
    ts = datetime(2026, 1, 1, 0, 0, 0)
    rows = list(pivot_rows(task, [
        DataPoint(task_id="task-1", timestamp=ts, value=1.0, metric_type="memory_rss")], []))
    assert rows == [["2026-01-01 00:00:00", "test.exe", 1234, "1.0000", ""]]
