"""遥测值的可见格式契约。"""
from utils.metrics import MetricType, format_metric_value


def test_metric_values_are_compact_and_scannable():
    assert format_metric_value(MetricType.MEMORY_RSS, 36004.0) == "35.16 MB"
    assert format_metric_value(MetricType.MEMORY_RSS, 512.5) == "512.5 KB"
    assert format_metric_value(
        MetricType.MEMORY_RSS, 1024.5, display_unit="KB") == "1,024.5 KB"
    assert format_metric_value(MetricType.MEMORY_RSS, 0) == "0 KB"
    assert format_metric_value(MetricType.MEMORY_RSS, 1024) == "1 MB"
    assert format_metric_value(MetricType.MEMORY_RSS, -1024) == "-1 MB"
    assert format_metric_value(MetricType.MEMORY_RSS, 1024 ** 2) == "1 GB"
    assert format_metric_value(MetricType.MEMORY_RSS, 1024 ** 3) == "1 TB"
    assert format_metric_value(MetricType.CPU_PERCENT, 3) == "3.0%"
    assert format_metric_value(MetricType.IO_READ_COUNT, 12345) == "12,345 次"
    assert format_metric_value("unknown_metric", 12.5) == "12.5"


def test_metric_values_format_by_kind():
    """按 kind 派生：峰值/累计字节同为 KB 自适应，CPU 时间两位小数，优先级无单位整数"""
    assert format_metric_value(MetricType.MEMORY_PEAK_WSET, 2048.0) == "2 MB"
    assert format_metric_value(MetricType.IO_WRITE_BYTES, 1024.0 ** 2) == "1 GB"
    assert format_metric_value(MetricType.MEMORY_PERCENT, 12.345) == "12.3%"
    assert format_metric_value(MetricType.CPU_USER_TIME, 1.5) == "1.50 秒"
    assert format_metric_value(MetricType.CPU_PRIORITY, 8.0) == "8"
    assert format_metric_value(MetricType.NUM_CTX_SWITCHES_VOL, 1234.0) == "1,234 次"
