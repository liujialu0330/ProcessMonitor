"""
指标定义和映射
将psutil的指标映射到任务管理器可见的指标
"""

class MetricType:
    """监控指标类型"""

    # 内存相关
    MEMORY_RSS = "memory_rss"           # 工作集内存 (RSS/WSET)
    MEMORY_VMS = "memory_vms"           # 虚拟内存大小
    MEMORY_PERCENT = "memory_percent"   # 内存使用百分比
    MEMORY_PEAK_WSET = "memory_peak_wset"       # 工作集峰值
    MEMORY_PRIVATE = "memory_private"           # 专用工作集
    MEMORY_PAGEFILE = "memory_pagefile"         # 提交大小
    MEMORY_PEAK_PAGEFILE = "memory_peak_pagefile"   # 提交大小峰值
    MEMORY_PAGED_POOL = "memory_paged_pool"         # 分页池
    MEMORY_PEAK_PAGED_POOL = "memory_peak_paged_pool"   # 分页池峰值
    MEMORY_NONPAGED_POOL = "memory_nonpaged_pool"       # 非分页池
    MEMORY_PEAK_NONPAGED_POOL = "memory_peak_nonpaged_pool"  # 非分页池峰值
    MEMORY_NUM_PAGE_FAULTS = "memory_num_page_faults"   # 页面错误
    MEMORY_USS = "memory_uss"                   # 唯一集大小

    # CPU相关
    CPU_PERCENT = "cpu_percent"         # CPU使用率
    CPU_USER_TIME = "cpu_user_time"     # CPU用户时间
    CPU_SYSTEM_TIME = "cpu_system_time" # CPU系统时间
    CPU_PRIORITY = "cpu_priority"       # 进程优先级

    # 线程和句柄
    NUM_THREADS = "num_threads"         # 线程数
    NUM_HANDLES = "num_handles"         # 句柄数 (Windows)
    NUM_CTX_SWITCHES_VOL = "num_ctx_switches_voluntary"     # 自愿上下文切换
    NUM_CTX_SWITCHES_INVOL = "num_ctx_switches_involuntary" # 非自愿上下文切换

    # IO相关
    IO_READ_COUNT = "io_read_count"     # 读取次数
    IO_WRITE_COUNT = "io_write_count"   # 写入次数
    IO_READ_BYTES = "io_read_bytes"     # 读取字节数
    IO_WRITE_BYTES = "io_write_bytes"   # 写入字节数
    IO_OTHER_COUNT = "io_other_count"   # 其他I/O次数
    IO_OTHER_BYTES = "io_other_bytes"   # 其他I/O字节数


# 指标显示名称映射
METRIC_DISPLAY_NAMES = {
    # 内存类
    MetricType.MEMORY_RSS: "工作集内存",
    MetricType.MEMORY_VMS: "虚拟内存",
    MetricType.MEMORY_PERCENT: "内存使用率",
    MetricType.MEMORY_PEAK_WSET: "工作集峰值",
    MetricType.MEMORY_PRIVATE: "专用工作集",
    MetricType.MEMORY_PAGEFILE: "提交大小",
    MetricType.MEMORY_PEAK_PAGEFILE: "提交大小峰值",
    MetricType.MEMORY_PAGED_POOL: "分页池",
    MetricType.MEMORY_PEAK_PAGED_POOL: "分页池峰值",
    MetricType.MEMORY_NONPAGED_POOL: "非分页池",
    MetricType.MEMORY_PEAK_NONPAGED_POOL: "非分页池峰值",
    MetricType.MEMORY_NUM_PAGE_FAULTS: "页面错误",
    MetricType.MEMORY_USS: "唯一集大小",
    # CPU类
    MetricType.CPU_PERCENT: "CPU使用率",
    MetricType.CPU_USER_TIME: "CPU用户时间",
    MetricType.CPU_SYSTEM_TIME: "CPU系统时间",
    MetricType.CPU_PRIORITY: "优先级",
    # 线程和句柄
    MetricType.NUM_THREADS: "线程数",
    MetricType.NUM_HANDLES: "句柄数",
    MetricType.NUM_CTX_SWITCHES_VOL: "自愿上下文切换",
    MetricType.NUM_CTX_SWITCHES_INVOL: "非自愿上下文切换",
    # I/O类
    MetricType.IO_READ_COUNT: "IO读取次数",
    MetricType.IO_WRITE_COUNT: "IO写入次数",
    MetricType.IO_READ_BYTES: "IO读取字节",
    MetricType.IO_WRITE_BYTES: "IO写入字节",
    MetricType.IO_OTHER_COUNT: "IO其他次数",
    MetricType.IO_OTHER_BYTES: "IO其他字节",
}

# 指标单位映射
METRIC_UNITS = {
    # 内存类
    MetricType.MEMORY_RSS: "KB",
    MetricType.MEMORY_VMS: "KB",
    MetricType.MEMORY_PERCENT: "%",
    MetricType.MEMORY_PEAK_WSET: "KB",
    MetricType.MEMORY_PRIVATE: "KB",
    MetricType.MEMORY_PAGEFILE: "KB",
    MetricType.MEMORY_PEAK_PAGEFILE: "KB",
    MetricType.MEMORY_PAGED_POOL: "KB",
    MetricType.MEMORY_PEAK_PAGED_POOL: "KB",
    MetricType.MEMORY_NONPAGED_POOL: "KB",
    MetricType.MEMORY_PEAK_NONPAGED_POOL: "KB",
    MetricType.MEMORY_NUM_PAGE_FAULTS: "次",
    MetricType.MEMORY_USS: "KB",
    # CPU类
    MetricType.CPU_PERCENT: "%",
    MetricType.CPU_USER_TIME: "秒",
    MetricType.CPU_SYSTEM_TIME: "秒",
    MetricType.CPU_PRIORITY: "",
    # 线程和句柄
    MetricType.NUM_THREADS: "个",
    MetricType.NUM_HANDLES: "个",
    MetricType.NUM_CTX_SWITCHES_VOL: "次",
    MetricType.NUM_CTX_SWITCHES_INVOL: "次",
    # I/O类
    MetricType.IO_READ_COUNT: "次",
    MetricType.IO_WRITE_COUNT: "次",
    MetricType.IO_READ_BYTES: "KB",
    MetricType.IO_WRITE_BYTES: "KB",
    MetricType.IO_OTHER_COUNT: "次",
    MetricType.IO_OTHER_BYTES: "KB",
}

# 可用的监控指标列表（按类别分组）
AVAILABLE_METRICS = {
    "内存": [
        MetricType.MEMORY_RSS,
        MetricType.MEMORY_VMS,
        MetricType.MEMORY_PERCENT,
        MetricType.MEMORY_PEAK_WSET,
        MetricType.MEMORY_PRIVATE,
        MetricType.MEMORY_PAGEFILE,
        MetricType.MEMORY_PEAK_PAGEFILE,
        MetricType.MEMORY_PAGED_POOL,
        MetricType.MEMORY_PEAK_PAGED_POOL,
        MetricType.MEMORY_NONPAGED_POOL,
        MetricType.MEMORY_PEAK_NONPAGED_POOL,
        MetricType.MEMORY_NUM_PAGE_FAULTS,
        MetricType.MEMORY_USS,
    ],
    "CPU": [
        MetricType.CPU_PERCENT,
        MetricType.CPU_USER_TIME,
        MetricType.CPU_SYSTEM_TIME,
        MetricType.CPU_PRIORITY,
    ],
    "系统资源": [
        MetricType.NUM_THREADS,
        MetricType.NUM_HANDLES,
        MetricType.NUM_CTX_SWITCHES_VOL,
        MetricType.NUM_CTX_SWITCHES_INVOL,
    ],
    "IO操作": [
        MetricType.IO_READ_BYTES,
        MetricType.IO_WRITE_BYTES,
        MetricType.IO_READ_COUNT,
        MetricType.IO_WRITE_COUNT,
        MetricType.IO_OTHER_BYTES,
        MetricType.IO_OTHER_COUNT,
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

    # 内存类（KB），保留2位小数
    if metric_type in [
        MetricType.MEMORY_RSS, MetricType.MEMORY_VMS,
        MetricType.MEMORY_PEAK_WSET, MetricType.MEMORY_PRIVATE,
        MetricType.MEMORY_PAGEFILE, MetricType.MEMORY_PEAK_PAGEFILE,
        MetricType.MEMORY_PAGED_POOL, MetricType.MEMORY_PEAK_PAGED_POOL,
        MetricType.MEMORY_NONPAGED_POOL, MetricType.MEMORY_PEAK_NONPAGED_POOL,
        MetricType.MEMORY_USS,
        MetricType.IO_READ_BYTES, MetricType.IO_WRITE_BYTES, MetricType.IO_OTHER_BYTES
    ]:
        return f"{value:.2f} {unit}"

    # 百分比，保留1位小数
    elif metric_type in [MetricType.MEMORY_PERCENT, MetricType.CPU_PERCENT]:
        return f"{value:.1f} {unit}"

    # 时间类（秒），保留2位小数
    elif metric_type in [MetricType.CPU_USER_TIME, MetricType.CPU_SYSTEM_TIME]:
        return f"{value:.2f} {unit}"

    # 整数类（次数、个数、优先级等）
    elif metric_type in [
        MetricType.NUM_THREADS, MetricType.NUM_HANDLES,
        MetricType.NUM_CTX_SWITCHES_VOL, MetricType.NUM_CTX_SWITCHES_INVOL,
        MetricType.IO_READ_COUNT, MetricType.IO_WRITE_COUNT, MetricType.IO_OTHER_COUNT,
        MetricType.MEMORY_NUM_PAGE_FAULTS, MetricType.CPU_PRIORITY
    ]:
        return f"{int(value)} {unit}".strip()

    # 默认保留2位小数
    else:
        return f"{value:.2f} {unit}"
