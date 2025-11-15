"""
指标定义和映射
将psutil的指标映射到任务管理器可见的指标
"""

class MetricType:
    """监控指标类型"""

    # 内存相关
    MEMORY_RSS = "memory_rss"           # 工作集内存 (RSS)
    MEMORY_VMS = "memory_vms"           # 虚拟内存大小
    MEMORY_PERCENT = "memory_percent"   # 内存使用百分比

    # CPU相关
    CPU_PERCENT = "cpu_percent"         # CPU使用率
    CPU_TIMES = "cpu_times"             # CPU时间

    # 线程和句柄
    NUM_THREADS = "num_threads"         # 线程数
    NUM_HANDLES = "num_handles"         # 句柄数 (Windows)

    # IO相关
    IO_READ_COUNT = "io_read_count"     # 读取次数
    IO_WRITE_COUNT = "io_write_count"   # 写入次数
    IO_READ_BYTES = "io_read_bytes"     # 读取字节数
    IO_WRITE_BYTES = "io_write_bytes"   # 写入字节数


# 指标显示名称映射
METRIC_DISPLAY_NAMES = {
    MetricType.MEMORY_RSS: "工作集内存",
    MetricType.MEMORY_VMS: "虚拟内存",
    MetricType.MEMORY_PERCENT: "内存使用率",
    MetricType.CPU_PERCENT: "CPU使用率",
    MetricType.CPU_TIMES: "CPU时间",
    MetricType.NUM_THREADS: "线程数",
    MetricType.NUM_HANDLES: "句柄数",
    MetricType.IO_READ_COUNT: "IO读取次数",
    MetricType.IO_WRITE_COUNT: "IO写入次数",
    MetricType.IO_READ_BYTES: "IO读取字节",
    MetricType.IO_WRITE_BYTES: "IO写入字节",
}

# 指标单位映射
METRIC_UNITS = {
    MetricType.MEMORY_RSS: "MB",
    MetricType.MEMORY_VMS: "MB",
    MetricType.MEMORY_PERCENT: "%",
    MetricType.CPU_PERCENT: "%",
    MetricType.CPU_TIMES: "秒",
    MetricType.NUM_THREADS: "个",
    MetricType.NUM_HANDLES: "个",
    MetricType.IO_READ_COUNT: "次",
    MetricType.IO_WRITE_COUNT: "次",
    MetricType.IO_READ_BYTES: "MB",
    MetricType.IO_WRITE_BYTES: "MB",
}

# 可用的监控指标列表（按类别分组）
AVAILABLE_METRICS = {
    "内存": [
        MetricType.MEMORY_RSS,
        MetricType.MEMORY_VMS,
        MetricType.MEMORY_PERCENT,
    ],
    "CPU": [
        MetricType.CPU_PERCENT,
    ],
    "系统资源": [
        MetricType.NUM_THREADS,
        MetricType.NUM_HANDLES,
    ],
    "IO操作": [
        MetricType.IO_READ_BYTES,
        MetricType.IO_WRITE_BYTES,
    ]
}


def get_metric_display_name(metric_type: str) -> str:
    """获取指标的显示名称"""
    return METRIC_DISPLAY_NAMES.get(metric_type, metric_type)


def get_metric_unit(metric_type: str) -> str:
    """获取指标的单位"""
    return METRIC_UNITS.get(metric_type, "")


def format_metric_value(metric_type: str, value: float) -> str:
    """格式化指标值为可读字符串"""
    unit = get_metric_unit(metric_type)

    # 根据不同类型格式化
    if metric_type in [MetricType.MEMORY_RSS, MetricType.MEMORY_VMS,
                       MetricType.IO_READ_BYTES, MetricType.IO_WRITE_BYTES]:
        # 字节转换为MB，保留2位小数
        return f"{value:.2f} {unit}"
    elif metric_type in [MetricType.MEMORY_PERCENT, MetricType.CPU_PERCENT]:
        # 百分比，保留1位小数
        return f"{value:.1f} {unit}"
    elif metric_type in [MetricType.NUM_THREADS, MetricType.NUM_HANDLES,
                        MetricType.IO_READ_COUNT, MetricType.IO_WRITE_COUNT]:
        # 整数
        return f"{int(value)} {unit}"
    else:
        # 默认保留2位小数
        return f"{value:.2f} {unit}"
