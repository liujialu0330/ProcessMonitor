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
