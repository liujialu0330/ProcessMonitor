"""
指标定义和映射
将psutil的指标映射到任务管理器可见的指标
"""
import math
import re

class MetricType:
    """监控指标类型"""

    # 内存相关
    MEMORY_RSS = "memory_rss"           # 工作集内存 (RSS/WSET)
    MEMORY_PERCENT = "memory_percent"   # 内存使用百分比
    MEMORY_PEAK_WSET = "memory_peak_wset"       # 工作集峰值
    # 私有字节（Private Bytes）：对应 Windows 的 PrivateUsage，即任务管理器的
    # "提交大小"；进程独占、不与其他进程共享的已提交内存，是判断内存泄漏的主指标。
    # psutil 在 Windows 上的 vms、pagefile、private 三个字段数值完全相同，
    # 因此旧版本的"虚拟内存""提交大小"已合并到这一项。
    MEMORY_PRIVATE = "memory_private"
    MEMORY_PEAK_PAGEFILE = "memory_peak_pagefile"   # 私有字节峰值
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
    NUM_GDI_OBJECTS = "num_gdi_objects"     # GDI对象数 (Windows)
    NUM_USER_OBJECTS = "num_user_objects"   # USER对象数 (Windows)
    NUM_CTX_SWITCHES_VOL = "num_ctx_switches_voluntary"     # 自愿上下文切换
    # 已废弃：psutil 在 Windows 上只能取到自愿上下文切换，非自愿项恒为 0。
    # 不再出现在可选指标里，键与显示名、单位、格式化保留，供旧任务数据显示与导出。
    NUM_CTX_SWITCHES_INVOL = "num_ctx_switches_involuntary" # 非自愿上下文切换（已废弃）

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
    MetricType.MEMORY_PERCENT: "内存使用率",
    MetricType.MEMORY_PEAK_WSET: "工作集峰值",
    MetricType.MEMORY_PRIVATE: "私有字节",
    MetricType.MEMORY_PEAK_PAGEFILE: "私有字节峰值",
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
    MetricType.NUM_GDI_OBJECTS: "GDI对象数",
    MetricType.NUM_USER_OBJECTS: "USER对象数",
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
    MetricType.MEMORY_PERCENT: "%",
    MetricType.MEMORY_PEAK_WSET: "KB",
    MetricType.MEMORY_PRIVATE: "KB",
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
    MetricType.NUM_GDI_OBJECTS: "个",
    MetricType.NUM_USER_OBJECTS: "个",
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


# 这些指标的底层采集单位固定为 KB；界面可按数值量级切换到 MB/GB/TB，数据库与
# CSV 的原始单位不受影响。
_KB_VALUE_METRICS = {
    MetricType.MEMORY_RSS,
    MetricType.MEMORY_PEAK_WSET,
    MetricType.MEMORY_PRIVATE,
    MetricType.MEMORY_PEAK_PAGEFILE,
    MetricType.MEMORY_PAGED_POOL,
    MetricType.MEMORY_PEAK_PAGED_POOL,
    MetricType.MEMORY_NONPAGED_POOL,
    MetricType.MEMORY_PEAK_NONPAGED_POOL,
    MetricType.MEMORY_USS,
    MetricType.IO_READ_BYTES,
    MetricType.IO_WRITE_BYTES,
    MetricType.IO_OTHER_BYTES,
}

# 取值恒为整数的指标（个数、次数、优先级）：界面显示取整，历史图 Y 轴刻度强制整数。
_INTEGER_METRICS = {
    MetricType.NUM_THREADS,
    MetricType.NUM_HANDLES,
    MetricType.NUM_GDI_OBJECTS,
    MetricType.NUM_USER_OBJECTS,
    MetricType.NUM_CTX_SWITCHES_VOL,
    MetricType.NUM_CTX_SWITCHES_INVOL,
    MetricType.IO_READ_COUNT,
    MetricType.IO_WRITE_COUNT,
    MetricType.IO_OTHER_COUNT,
    MetricType.MEMORY_NUM_PAGE_FAULTS,
    MetricType.CPU_PRIORITY,
}

_KB_DISPLAY_DIVISORS = {
    "KB": 1.0,
    "MB": 1024.0,
    "GB": 1024.0 * 1024.0,
    "TB": 1024.0 * 1024.0 * 1024.0,
}

# 可用的监控指标列表（按类别分组）
AVAILABLE_METRICS = {
    "内存": [
        MetricType.MEMORY_RSS,
        MetricType.MEMORY_PERCENT,
        MetricType.MEMORY_PEAK_WSET,
        MetricType.MEMORY_PRIVATE,
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
        MetricType.NUM_GDI_OBJECTS,
        MetricType.NUM_USER_OBJECTS,
        MetricType.NUM_CTX_SWITCHES_VOL,
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


# 已废弃的指标：不在 AVAILABLE_METRICS（可选集合）里，但显示名、单位、说明与格式化
# 仍保留，旧任务里已有的数据照常读取、显示与导出。
DEPRECATED_METRICS = frozenset({
    MetricType.NUM_CTX_SWITCHES_INVOL,
})


# "泄漏排查"预设：判断被测程序是否存在内存/句柄/GDI/USER 对象/线程泄漏时的一组
# 最小必要指标，监控页默认勾选。
LEAK_CHECK_PRESET = [
    MetricType.MEMORY_PRIVATE,
    MetricType.NUM_HANDLES,
    MetricType.NUM_GDI_OBJECTS,
    MetricType.NUM_USER_OBJECTS,
    MetricType.NUM_THREADS,
]

# 指标说明：一句话讲清含义，以及适合发现哪类问题（指标选择对话框的 tooltip）。
METRIC_DESCRIPTIONS = {
    # 内存类
    MetricType.MEMORY_RSS:
        "工作集：当前驻留在物理内存中的页，会被系统换出而随内存压力波动，"
        "适合看内存占用的实时水位，不宜单独用来判断泄漏",
    MetricType.MEMORY_PERCENT:
        "工作集占整机物理内存的百分比，适合评估被测进程对整机内存的压力",
    MetricType.MEMORY_PEAK_WSET:
        "进程启动以来工作集的最大值，只增不减，适合查看内存使用的历史峰值",
    MetricType.MEMORY_PRIVATE:
        "进程独占的已提交内存（Private Bytes），持续上涨是内存泄漏的主要信号",
    MetricType.MEMORY_PEAK_PAGEFILE:
        "私有字节的历史峰值，只增不减，适合查看某次操作期间的内存冲高",
    MetricType.MEMORY_PAGED_POOL:
        "进程占用的内核分页池，持续上涨多见于驱动或系统对象（如文件、注册表句柄）泄漏",
    MetricType.MEMORY_PEAK_PAGED_POOL:
        "分页池的历史峰值，只增不减，适合排查内核资源的瞬时冲高",
    MetricType.MEMORY_NONPAGED_POOL:
        "进程占用的内核非分页池（常驻物理内存），持续上涨多见于驱动、设备或句柄泄漏",
    MetricType.MEMORY_PEAK_NONPAGED_POOL:
        "非分页池的历史峰值，只增不减，适合排查内核资源的瞬时冲高",
    MetricType.MEMORY_NUM_PAGE_FAULTS:
        "累计缺页次数，增长过快说明内存被频繁换入换出，适合发现内存紧张或访问模式异常",
    MetricType.MEMORY_USS:
        "进程独占的物理内存（不含共享页），比工作集更接近真实占用；"
        "读取开销较大，且对更高权限进程可能因权限不足而不可用",
    # CPU类
    MetricType.CPU_PERCENT:
        "进程 CPU 占用率（可超过 100%，按核数累计），适合发现空闲时仍高占用的死循环或忙等",
    MetricType.CPU_USER_TIME:
        "累计在用户态运行的 CPU 时间，适合比较各轮操作消耗的计算量",
    MetricType.CPU_SYSTEM_TIME:
        "累计在内核态运行的 CPU 时间，偏高多见于大量系统调用或 I/O",
    MetricType.CPU_PRIORITY:
        "进程调度优先级，用于确认被测程序的优先级是否被改动",
    # 线程和句柄
    MetricType.NUM_THREADS:
        "进程内的线程总数，持续上涨说明线程创建后没有退出（线程泄漏）",
    MetricType.NUM_HANDLES:
        "进程打开的内核句柄数（文件、事件、互斥量等），持续上涨是句柄泄漏的信号，"
        "单进程上限约 1600 万",
    MetricType.NUM_GDI_OBJECTS:
        "位图、画刷、字体等 GDI 资源数，单进程上限 10000，"
        "持续上涨会导致界面花屏或崩溃",
    MetricType.NUM_USER_OBJECTS:
        "窗口、菜单、光标等 USER 对象数，单进程上限 10000，"
        "持续上涨说明窗口或菜单创建后没有销毁",
    MetricType.NUM_CTX_SWITCHES_VOL:
        "累计自愿上下文切换次数（线程主动等待让出 CPU），适合分析线程同步与阻塞",
    MetricType.NUM_CTX_SWITCHES_INVOL:
        "累计非自愿上下文切换次数（时间片用完被抢占）；已废弃：Windows 上无法采集，恒为 0",
    # I/O类
    MetricType.IO_READ_BYTES:
        "累计读取字节数，适合发现异常的磁盘或网络读取量",
    MetricType.IO_WRITE_BYTES:
        "累计写入字节数，适合发现日志刷屏或临时文件持续增长",
    MetricType.IO_READ_COUNT:
        "累计读取操作次数，适合发现高频小块读取",
    MetricType.IO_WRITE_COUNT:
        "累计写入操作次数，适合发现高频小块写入",
    MetricType.IO_OTHER_BYTES:
        "累计其他 I/O（非读写，如设备控制）传输的字节数",
    MetricType.IO_OTHER_COUNT:
        "累计其他 I/O（非读写，如设备控制）操作次数",
}


def get_metric_description(metric_type: str) -> str:
    """获取指标说明（含义 + 适合发现的问题）；未知指标返回空串。"""
    return METRIC_DESCRIPTIONS.get(canonical_metric_type(metric_type), "")


def all_metric_types() -> list:
    """按分类顺序列出全部可选（可新建监控）的指标键，不含已废弃指标。"""
    return [m for metrics in AVAILABLE_METRICS.values() for m in metrics]


# 旧版本曾独立提供的"虚拟内存""提交大小"，在 Windows 上与私有字节数值完全相同，
# 现已合并。数据库迁移失败或残留旧数据时，旧键仍按私有字节显示并按 KB 格式化。
LEGACY_METRIC_ALIASES = {
    "memory_vms": "memory_private",
    "memory_pagefile": "memory_private",
}


def canonical_metric_type(metric_type: str) -> str:
    """把旧指标键归一为当前键；其他键原样返回。"""
    return LEGACY_METRIC_ALIASES.get(metric_type, metric_type)


def get_metric_display_name(metric_type: str) -> str:
    """获取指标的显示名称"""
    metric_type = canonical_metric_type(metric_type)
    return METRIC_DISPLAY_NAMES.get(metric_type, metric_type)


def get_metric_unit(metric_type: str) -> str:
    """获取指标的单位"""
    metric_type = canonical_metric_type(metric_type)
    return METRIC_UNITS.get(metric_type, "")


def get_metric_display_unit(metric_type: str, reference_value: float) -> str:
    """按参考值选择界面显示单位；非 KB 指标保持定义单位。"""
    metric_type = canonical_metric_type(metric_type)
    if metric_type not in _KB_VALUE_METRICS:
        return get_metric_unit(metric_type)

    magnitude = abs(reference_value)
    if magnitude >= _KB_DISPLAY_DIVISORS["TB"]:
        return "TB"
    if magnitude >= _KB_DISPLAY_DIVISORS["GB"]:
        return "GB"
    if magnitude >= _KB_DISPLAY_DIVISORS["MB"]:
        return "MB"
    return "KB"


def format_metric_value(
        metric_type: str, value: float, display_unit: str = None) -> str:
    """格式化指标值为可读字符串"""
    metric_type = canonical_metric_type(metric_type)
    unit = get_metric_unit(metric_type)

    def compact_number(number: float, decimals: int = 2) -> str:
        """添加千分位并去掉无信息量的尾随零。"""
        return f"{number:,.{decimals}f}".rstrip("0").rstrip(".")

    # 内存与 I/O 字节类自动选择 KB/MB/GB/TB；历史页可显式传 display_unit，
    # 让同一图表里的统计、悬停、表格与坐标轴始终使用同一个单位。
    if metric_type in _KB_VALUE_METRICS:
        unit = display_unit or get_metric_display_unit(metric_type, value)
        divisor = _KB_DISPLAY_DIVISORS.get(unit, 1.0)
        return f"{compact_number(value / divisor)} {unit}"

    # 百分比，保留1位小数
    elif metric_type in [MetricType.MEMORY_PERCENT, MetricType.CPU_PERCENT]:
        return f"{value:,.1f}{unit}"

    # 时间类（秒），保留2位小数
    elif metric_type in [MetricType.CPU_USER_TIME, MetricType.CPU_SYSTEM_TIME]:
        return f"{value:,.2f} {unit}"

    # 整数类（次数、个数、优先级等）
    elif metric_type in _INTEGER_METRICS:
        return f"{int(value):,} {unit}".strip()

    # 默认保留2位小数
    else:
        return f"{compact_number(value)} {unit}".strip()


def is_integer_metric(metric_type: str) -> bool:
    """该指标的取值是否恒为整数（个数/次数/优先级）。"""
    return canonical_metric_type(metric_type) in _INTEGER_METRICS


def is_kb_metric(metric_type: str) -> bool:
    """该指标的底层单位是否为 KB（内存与 I/O 字节类，显示时自适应 KB/MB/GB/TB）。"""
    return canonical_metric_type(metric_type) in _KB_VALUE_METRICS


def _text_is_zero(text: str) -> bool:
    """格式化后的数值文本（可带单位）所表示的数是否为 0。"""
    match = re.match(r"[\d,]*\.?\d+|[\d,]+", text.strip())
    if not match:
        return False
    try:
        return float(match.group(0).replace(",", "")) == 0.0
    except ValueError:
        return False


def format_metric_delta(
        metric_type: str, delta: float, display_unit: str = None) -> str:
    """格式化"较基线的变化量"：带正负号；四舍五入后为 0 显示 "±0"。

    字节类按变化量本身的量级自适应 KB/MB/GB/TB（可用 display_unit 固定单位），
    计数类为带符号整数。delta 为 None 或非有限数时返回 "--"。
    """
    if delta is None or not math.isfinite(delta):
        return "--"
    body = format_metric_value(metric_type, abs(delta), display_unit)
    if _text_is_zero(body):
        return "±0"
    return ("+" if delta > 0 else "-") + body


def format_metric_rate(metric_type: str, per_hour: float) -> str:
    """格式化"每小时增长速度"：带正负号，后缀 "/小时"；为 0 显示 "±0/小时"。

    字节类自适应单位（如 "+12.3 MB/小时"）；计数类允许小数（如 "+12.5 个/小时"，
    100 以上取整）。per_hour 为 None 或非有限数时返回 "--"。
    """
    if per_hour is None or not math.isfinite(per_hour):
        return "--"
    metric_type = canonical_metric_type(metric_type)
    magnitude = abs(per_hour)

    if metric_type in _KB_VALUE_METRICS:
        body = format_metric_value(metric_type, magnitude)
    else:
        unit = get_metric_unit(metric_type)
        if metric_type in _INTEGER_METRICS:
            decimals = 0 if magnitude >= 100 else 1
        elif metric_type in (MetricType.CPU_USER_TIME, MetricType.CPU_SYSTEM_TIME):
            decimals = 2
        else:
            decimals = 1 if magnitude >= 10 else 2
        number = f"{magnitude:,.{decimals}f}"
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        body = f"{number}{unit}" if unit == "%" else f"{number} {unit}".strip()

    if _text_is_zero(body):
        return "±0/小时"
    return ("+" if per_hour > 0 else "-") + body + "/小时"
