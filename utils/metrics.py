"""
指标定义和映射
将psutil的指标映射到任务管理器可见的指标

全部元数据集中在 REGISTRY（MetricSpec 注册表）里；显示名、单位、说明、可选分组、
泄漏预设等旧表，以及整数性/KB/格式化/迷你图跨度/能否外推，都从注册表与 kind 派生。
本模块不依赖 psutil，采集逻辑在 core/process_collector.py。
"""
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set


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


class MetricKind(Enum):
    """指标的数值语义。整数性、是否 KB、格式化、迷你图跨度、能否外推增速都由它派生。"""

    BYTES_KB = "bytes_kb"                   # 当前水位的字节量（采集时换算为 KB 存储）
    PERCENT = "percent"                     # 百分比
    SECONDS = "seconds"                     # 进程启动以来累计的 CPU 时间（秒）
    COUNT = "count"                         # 当前水位的个数/状态值（取值恒为整数）
    CUMULATIVE_COUNT = "cumulative_count"   # 进程启动以来只增不减的累计次数
    CUMULATIVE_BYTES = "cumulative_bytes"   # 进程启动以来只增不减的累计字节量（KB）
    # 历史峰值，只增不减。仅用于内存峰值，单位与对应的内存指标相同（KB）
    PEAK = "peak"


# 底层单位为 KB、显示时自适应 KB/MB/GB/TB 的种类
_KB_KINDS = frozenset({MetricKind.BYTES_KB, MetricKind.CUMULATIVE_BYTES, MetricKind.PEAK})
# 取值恒为整数的种类：显示取整，历史图 Y 轴刻度强制整数
_INTEGER_KINDS = frozenset({MetricKind.COUNT, MetricKind.CUMULATIVE_COUNT})
# 单调不减、不是"水位量"的种类：对它们做线性回归得到的"增速"只是累计速率，
# 不能外推，也不能据此判断泄漏。SECONDS 也在其中——目前只有 CPU 用户/内核时间
# 属于这一类，两者都是进程启动以来的累计值。
_CUMULATIVE_KINDS = frozenset({
    MetricKind.CUMULATIVE_COUNT, MetricKind.CUMULATIVE_BYTES,
    MetricKind.PEAK, MetricKind.SECONDS,
})


@dataclass(frozen=True)
class MetricSpec:
    """一个指标的全部元数据；新增指标只需在 _SPECS 里加一项（采集侧另加一个 sampler）。"""

    key: str
    name: str                   # 界面显示名
    unit: str                   # 存储与默认显示单位（KB 类显示时可升到 MB/GB/TB）
    category: str               # 指标选择对话框里的分组
    kind: MetricKind
    description: str            # 一句话说明含义与适合发现的问题（tooltip）
    # 采集前提：'windows' 只有 Windows 版 psutil 提供该字段；
    # 'admin' 对更高权限的进程需要管理员身份才能读取
    requires: str = ''
    needs_priming: bool = False     # 首次采集需预热（cpu_percent 首次调用恒为 0）
    in_leak_preset: bool = False    # 属于"泄漏排查"预设
    # 在泄漏排查预设里的排位（越小越靠前，0 表示排在有排位的项之后、按注册表顺序）。
    # 预设顺序就是未经选择对话框直接开始监控时任务的指标顺序，泄漏主信号要排第一。
    leak_preset_rank: int = 0
    # 已废弃：不再可选，但显示名、单位、说明与格式化保留，供旧任务数据显示与导出
    deprecated: bool = False


# 注册表：顺序即界面顺序（分类按首次出现的先后，分类内按此处顺序）。
# 每项上方注释写 kind 的判定理由；单位为 "KB" 的都是采集时已除以 1024。
_SPECS = (
    # ---------- 内存 ----------
    # 当前驻留物理内存，随内存压力升降，是水位量
    MetricSpec(
        MetricType.MEMORY_RSS, "工作集内存", "KB", "内存", MetricKind.BYTES_KB,
        "工作集：当前驻留在物理内存中的页，会被系统换出而随内存压力波动，"
        "适合看内存占用的实时水位，不宜单独用来判断泄漏"),
    # 工作集占整机内存的比例
    MetricSpec(
        MetricType.MEMORY_PERCENT, "内存使用率", "%", "内存", MetricKind.PERCENT,
        "工作集占整机物理内存的百分比，适合评估被测进程对整机内存的压力"),
    # 进程启动以来的最大工作集，只增不减
    MetricSpec(
        MetricType.MEMORY_PEAK_WSET, "工作集峰值", "KB", "内存", MetricKind.PEAK,
        "进程启动以来工作集的最大值，只增不减，适合查看内存使用的历史峰值",
        requires='windows'),
    # 已提交私有内存的当前水位，泄漏主指标
    MetricSpec(
        MetricType.MEMORY_PRIVATE, "私有字节", "KB", "内存", MetricKind.BYTES_KB,
        "进程独占的已提交内存（Private Bytes），持续上涨是内存泄漏的主要信号",
        requires='windows', in_leak_preset=True, leak_preset_rank=1),
    # 私有字节的历史最大值，只增不减
    MetricSpec(
        MetricType.MEMORY_PEAK_PAGEFILE, "私有字节峰值", "KB", "内存", MetricKind.PEAK,
        "私有字节的历史峰值，只增不减，适合查看某次操作期间的内存冲高",
        requires='windows'),
    # 内核分页池的当前占用，可升可降
    MetricSpec(
        MetricType.MEMORY_PAGED_POOL, "分页池", "KB", "内存", MetricKind.BYTES_KB,
        "进程占用的内核分页池，持续上涨多见于驱动或系统对象（如文件、注册表句柄）泄漏",
        requires='windows'),
    # 分页池的历史最大值，只增不减
    MetricSpec(
        MetricType.MEMORY_PEAK_PAGED_POOL, "分页池峰值", "KB", "内存", MetricKind.PEAK,
        "分页池的历史峰值，只增不减，适合排查内核资源的瞬时冲高",
        requires='windows'),
    # 内核非分页池的当前占用，可升可降
    MetricSpec(
        MetricType.MEMORY_NONPAGED_POOL, "非分页池", "KB", "内存", MetricKind.BYTES_KB,
        "进程占用的内核非分页池（常驻物理内存），持续上涨多见于驱动、设备或句柄泄漏",
        requires='windows'),
    # 非分页池的历史最大值，只增不减
    MetricSpec(
        MetricType.MEMORY_PEAK_NONPAGED_POOL, "非分页池峰值", "KB", "内存", MetricKind.PEAK,
        "非分页池的历史峰值，只增不减，适合排查内核资源的瞬时冲高",
        requires='windows'),
    # 进程启动以来的缺页总次数，只增不减
    MetricSpec(
        MetricType.MEMORY_NUM_PAGE_FAULTS, "页面错误", "次", "内存",
        MetricKind.CUMULATIVE_COUNT,
        "累计缺页次数，增长过快说明内存被频繁换入换出，适合发现内存紧张或访问模式异常",
        requires='windows'),
    # 独占物理内存的当前水位；memory_full_info 需要较高的进程访问权限
    MetricSpec(
        MetricType.MEMORY_USS, "唯一集大小", "KB", "内存", MetricKind.BYTES_KB,
        "进程独占的物理内存（不含共享页），比工作集更接近真实占用；"
        "读取开销较大，且对更高权限进程可能因权限不足而不可用",
        requires='admin'),

    # ---------- CPU ----------
    # 两次采集之间的占用率；psutil 首次调用恒为 0，需要预热
    MetricSpec(
        MetricType.CPU_PERCENT, "CPU使用率", "%", "CPU", MetricKind.PERCENT,
        "进程 CPU 占用率（可超过 100%，按核数累计），适合发现空闲时仍高占用的死循环或忙等",
        needs_priming=True),
    # 进程启动以来累计的用户态时间（秒），只增不减
    MetricSpec(
        MetricType.CPU_USER_TIME, "CPU用户时间", "秒", "CPU", MetricKind.SECONDS,
        "累计在用户态运行的 CPU 时间，适合比较各轮操作消耗的计算量"),
    # 进程启动以来累计的内核态时间（秒），只增不减
    MetricSpec(
        MetricType.CPU_SYSTEM_TIME, "CPU系统时间", "秒", "CPU", MetricKind.SECONDS,
        "累计在内核态运行的 CPU 时间，偏高多见于大量系统调用或 I/O"),
    # 调度优先级是可被改回的整数状态值，不是累计量；无单位
    MetricSpec(
        MetricType.CPU_PRIORITY, "优先级", "", "CPU", MetricKind.COUNT,
        "进程调度优先级，用于确认被测程序的优先级是否被改动"),

    # ---------- 系统资源 ----------
    # 线程/句柄/GDI/USER 都是"当前持有数"，资源释放后会下降，是水位量
    MetricSpec(
        MetricType.NUM_THREADS, "线程数", "个", "系统资源", MetricKind.COUNT,
        "进程内的线程总数，持续上涨说明线程创建后没有退出（线程泄漏）",
        in_leak_preset=True, leak_preset_rank=5),
    # 非 Windows 上回落为文件描述符数，因此不标 windows
    MetricSpec(
        MetricType.NUM_HANDLES, "句柄数", "个", "系统资源", MetricKind.COUNT,
        "进程打开的内核句柄数（文件、事件、互斥量等），持续上涨是句柄泄漏的信号，"
        "单进程上限约 1600 万",
        in_leak_preset=True, leak_preset_rank=2),
    MetricSpec(
        MetricType.NUM_GDI_OBJECTS, "GDI对象数", "个", "系统资源", MetricKind.COUNT,
        "位图、画刷、字体等 GDI 资源数，单进程上限 10000，"
        "持续上涨会导致界面花屏或崩溃",
        requires='windows', in_leak_preset=True, leak_preset_rank=3),
    MetricSpec(
        MetricType.NUM_USER_OBJECTS, "USER对象数", "个", "系统资源", MetricKind.COUNT,
        "窗口、菜单、光标等 USER 对象数，单进程上限 10000，"
        "持续上涨说明窗口或菜单创建后没有销毁",
        requires='windows', in_leak_preset=True, leak_preset_rank=4),
    # 上下文切换是进程启动以来的累计次数，只增不减
    MetricSpec(
        MetricType.NUM_CTX_SWITCHES_VOL, "自愿上下文切换", "次", "系统资源",
        MetricKind.CUMULATIVE_COUNT,
        "累计自愿上下文切换次数（线程主动等待让出 CPU），适合分析线程同步与阻塞"),
    # 同为累计次数；psutil 在 Windows 上只能取到自愿切换，此项恒为 0，已废弃
    MetricSpec(
        MetricType.NUM_CTX_SWITCHES_INVOL, "非自愿上下文切换", "次", "系统资源",
        MetricKind.CUMULATIVE_COUNT,
        "累计非自愿上下文切换次数（时间片用完被抢占）；已废弃：Windows 上无法采集，恒为 0",
        deprecated=True),

    # ---------- IO操作 ----------
    # psutil.io_counters 的字节与次数都是进程启动以来的累计值，只增不减
    MetricSpec(
        MetricType.IO_READ_BYTES, "IO读取字节", "KB", "IO操作", MetricKind.CUMULATIVE_BYTES,
        "累计读取字节数，适合发现异常的磁盘或网络读取量"),
    MetricSpec(
        MetricType.IO_WRITE_BYTES, "IO写入字节", "KB", "IO操作", MetricKind.CUMULATIVE_BYTES,
        "累计写入字节数，适合发现日志刷屏或临时文件持续增长"),
    MetricSpec(
        MetricType.IO_READ_COUNT, "IO读取次数", "次", "IO操作", MetricKind.CUMULATIVE_COUNT,
        "累计读取操作次数，适合发现高频小块读取"),
    MetricSpec(
        MetricType.IO_WRITE_COUNT, "IO写入次数", "次", "IO操作", MetricKind.CUMULATIVE_COUNT,
        "累计写入操作次数，适合发现高频小块写入"),
    # "其他 I/O" 字段只有 Windows 版 psutil 提供
    MetricSpec(
        MetricType.IO_OTHER_BYTES, "IO其他字节", "KB", "IO操作", MetricKind.CUMULATIVE_BYTES,
        "累计其他 I/O（非读写，如设备控制）传输的字节数",
        requires='windows'),
    MetricSpec(
        MetricType.IO_OTHER_COUNT, "IO其他次数", "次", "IO操作", MetricKind.CUMULATIVE_COUNT,
        "累计其他 I/O（非读写，如设备控制）操作次数",
        requires='windows'),
)

REGISTRY: Dict[str, MetricSpec] = {s.key: s for s in _SPECS}


# KB 存储值换算到显示单位的除数（单一来源；历史图坐标轴也用它）
KB_UNIT_DIVISORS: Dict[str, float] = {
    "KB": 1.0,
    "MB": 1024.0,
    "GB": 1024.0 ** 2,
    "TB": 1024.0 ** 3,
}
_KB_DISPLAY_DIVISORS = KB_UNIT_DIVISORS

# 迷你图/图表纵轴最小跨度：字节类 max(当前值 1%, 64 KB)；计数类至少 4；
# 百分比至少 1 个百分点；其余按当前值 1%
MIN_SPAN_BYTES_KB = 64.0
MIN_SPAN_BYTES_RATIO = 0.01
MIN_SPAN_COUNT = 4.0
MIN_SPAN_PERCENT = 1.0

# 累计类指标的"增速"不适用时的文本（与表示"无数据"的 "--" 区分开）
RATE_NOT_APPLICABLE = "—"


# ---------- 派生表（全部由 REGISTRY 生成；保留旧名，调用方零改动） ----------
# 这些容器在注册表变化时原地重建，`from utils.metrics import X` 拿到的引用始终有效；
# 调用方只读，不要直接修改。
METRIC_DISPLAY_NAMES: Dict[str, str] = {}
METRIC_UNITS: Dict[str, str] = {}
# 指标说明：一句话讲清含义，以及适合发现哪类问题（指标选择对话框的 tooltip）
METRIC_DESCRIPTIONS: Dict[str, str] = {}
# 底层采集单位固定为 KB 的指标；界面可按数值量级切换到 MB/GB/TB，数据库与 CSV 不受影响
_KB_VALUE_METRICS: Set[str] = set()
# 取值恒为整数的指标（个数、次数、优先级）：界面显示取整，历史图 Y 轴刻度强制整数
_INTEGER_METRICS: Set[str] = set()
# 可选（可新建监控）的指标，按分类分组；不含已废弃指标
AVAILABLE_METRICS: Dict[str, List[str]] = {}
# 已废弃的指标：不在可选集合里，但旧任务数据照常读取、显示与导出
DEPRECATED_METRICS: Set[str] = set()
# "泄漏排查"预设：判断内存/句柄/GDI/USER 对象/线程泄漏的最小必要指标，监控页默认勾选
LEAK_CHECK_PRESET: List[str] = []


def _rebuild_derived_tables() -> None:
    """按 REGISTRY 原地重建全部派生表。"""
    specs = list(REGISTRY.values())
    for table, attr in ((METRIC_DISPLAY_NAMES, "name"), (METRIC_UNITS, "unit"),
                        (METRIC_DESCRIPTIONS, "description")):
        table.clear()
        table.update((s.key, getattr(s, attr)) for s in specs)

    _KB_VALUE_METRICS.clear()
    _KB_VALUE_METRICS.update(s.key for s in specs if s.kind in _KB_KINDS)
    _INTEGER_METRICS.clear()
    _INTEGER_METRICS.update(s.key for s in specs if s.kind in _INTEGER_KINDS)
    DEPRECATED_METRICS.clear()
    DEPRECATED_METRICS.update(s.key for s in specs if s.deprecated)

    AVAILABLE_METRICS.clear()
    for s in specs:
        if not s.deprecated:
            AVAILABLE_METRICS.setdefault(s.category, []).append(s.key)

    order = {key: i for i, key in enumerate(REGISTRY)}
    unranked = len(order) + 1
    preset = [s for s in specs if s.in_leak_preset and not s.deprecated]
    preset.sort(key=lambda s: (s.leak_preset_rank or unranked, order[s.key]))
    LEAK_CHECK_PRESET[:] = [s.key for s in preset]


def register_metric(metric: MetricSpec) -> None:
    """登记（或替换）一个指标并同步全部派生表。

    正式指标直接写进 _SPECS；本函数供扩展与测试在运行时增删指标。
    """
    REGISTRY[metric.key] = metric
    _rebuild_derived_tables()


def unregister_metric(key: str) -> None:
    """移除一个指标并同步全部派生表（不存在时忽略）。"""
    REGISTRY.pop(key, None)
    _rebuild_derived_tables()


_rebuild_derived_tables()


# 旧版本曾独立提供的"虚拟内存""提交大小"，在 Windows 上与私有字节数值完全相同，
# 现已合并。数据库迁移失败或残留旧数据时，旧键仍按私有字节显示并按 KB 格式化。
# 迁移代码依赖这份固定映射，因此不接注册表。
LEGACY_METRIC_ALIASES = {
    "memory_vms": "memory_private",
    "memory_pagefile": "memory_private",
}


def canonical_metric_type(metric_type: str) -> str:
    """把旧指标键归一为当前键；其他键原样返回。"""
    return LEGACY_METRIC_ALIASES.get(metric_type, metric_type)


def spec(key: str) -> MetricSpec:
    """取指标元数据；旧键先归一。未知键抛 KeyError。"""
    return REGISTRY[canonical_metric_type(key)]


def _kind_of(metric_type: Optional[str]) -> Optional[MetricKind]:
    """指标的 kind；None 或未知键返回 None（按"无特殊语义"处理）。"""
    if metric_type is None:
        return None
    found = REGISTRY.get(canonical_metric_type(metric_type))
    return found.kind if found else None


def get_metric_description(metric_type: str) -> str:
    """获取指标说明（含义 + 适合发现的问题）；未知指标返回空串。"""
    return METRIC_DESCRIPTIONS.get(canonical_metric_type(metric_type), "")


def all_metric_types() -> list:
    """按分类顺序列出全部可选（可新建监控）的指标键，不含已废弃指标。"""
    return [m for metrics in AVAILABLE_METRICS.values() for m in metrics]


def get_metric_display_name(metric_type: str) -> str:
    """获取指标的显示名称"""
    metric_type = canonical_metric_type(metric_type)
    return METRIC_DISPLAY_NAMES.get(metric_type, metric_type)


def get_metric_unit(metric_type: str) -> str:
    """获取指标的单位"""
    metric_type = canonical_metric_type(metric_type)
    return METRIC_UNITS.get(metric_type, "")


def is_integer_metric(metric_type: str) -> bool:
    """该指标的取值是否恒为整数（个数/次数/优先级）。"""
    return _kind_of(metric_type) in _INTEGER_KINDS


def is_kb_metric(metric_type: str) -> bool:
    """该指标的底层单位是否为 KB（内存与 I/O 字节类，显示时自适应 KB/MB/GB/TB）。"""
    return _kind_of(metric_type) in _KB_KINDS


def is_cumulative_metric(metric_type: str) -> bool:
    """该指标是否单调不减（累计次数/累计字节/CPU 累计时间/历史峰值）。

    这类指标不是水位量：回归斜率只反映累计速率，不能当"增速"外推，也不能据此
    判断泄漏；历史页统计条与任务卡片应隐藏它们的增速。未知键返回 False。
    """
    return _kind_of(metric_type) in _CUMULATIVE_KINDS


def display_unit_for(metric_type: str, reference_value: float) -> str:
    """按参考值选择界面显示单位；非 KB 指标保持定义单位。"""
    if not is_kb_metric(metric_type):
        return get_metric_unit(metric_type)

    magnitude = abs(reference_value)
    for unit in ("TB", "GB", "MB"):
        if magnitude >= KB_UNIT_DIVISORS[unit]:
            return unit
    return "KB"


def get_metric_display_unit(metric_type: str, reference_value: float) -> str:
    """display_unit_for 的旧名，保留给现有调用方。"""
    return display_unit_for(metric_type, reference_value)


def min_span_for(metric_type: Optional[str], reference: float) -> float:
    """纵轴最小跨度（原始单位）：低于该跨度的起伏不应被拉伸成满幅波动。

    metric_type 为 None 表示不设下限（返回 0）；未知键按参考值的 1% 处理。
    """
    if metric_type is None:
        return 0.0
    kind = _kind_of(metric_type)
    if kind in _KB_KINDS:
        return max(abs(reference) * MIN_SPAN_BYTES_RATIO, MIN_SPAN_BYTES_KB)
    if kind in _INTEGER_KINDS:
        return MIN_SPAN_COUNT
    if kind is MetricKind.PERCENT:
        return MIN_SPAN_PERCENT
    return abs(reference) * MIN_SPAN_BYTES_RATIO


def _compact_number(number: float, decimals: int = 2) -> str:
    """添加千分位并去掉无信息量的尾随零。"""
    return f"{number:,.{decimals}f}".rstrip("0").rstrip(".")


def format_metric_value(
        metric_type: str, value: float, display_unit: str = None) -> str:
    """格式化指标值为可读字符串"""
    kind = _kind_of(metric_type)
    unit = get_metric_unit(metric_type)

    # 字节类自动选择 KB/MB/GB/TB；历史页可显式传 display_unit，
    # 让同一图表里的统计、悬停、表格与坐标轴始终使用同一个单位。
    if kind in _KB_KINDS:
        unit = display_unit or display_unit_for(metric_type, value)
        divisor = KB_UNIT_DIVISORS.get(unit, 1.0)
        return f"{_compact_number(value / divisor)} {unit}"
    # 百分比，保留 1 位小数
    if kind is MetricKind.PERCENT:
        return f"{value:,.1f}{unit}"
    # 时间（秒），保留 2 位小数
    if kind is MetricKind.SECONDS:
        return f"{value:,.2f} {unit}"
    # 整数类（次数、个数、优先级）
    if kind in _INTEGER_KINDS:
        return f"{int(value):,} {unit}".strip()
    # 未知指标：至多保留 2 位小数
    return f"{_compact_number(value)} {unit}".strip()


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
    100 以上取整）。累计类指标（见 is_cumulative_metric）的增速没有意义，一律返回
    RATE_NOT_APPLICABLE；其余指标 per_hour 为 None 或非有限数时返回 "--"。
    """
    if is_cumulative_metric(metric_type):
        return RATE_NOT_APPLICABLE
    if per_hour is None or not math.isfinite(per_hour):
        return "--"
    kind = _kind_of(metric_type)
    magnitude = abs(per_hour)

    if kind in _KB_KINDS:
        body = format_metric_value(metric_type, magnitude)
    else:
        unit = get_metric_unit(metric_type)
        if kind in _INTEGER_KINDS:
            decimals = 0 if magnitude >= 100 else 1
        else:
            decimals = 1 if magnitude >= 10 else 2
        number = f"{magnitude:,.{decimals}f}"
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        body = f"{number}{unit}" if unit == "%" else f"{number} {unit}".strip()

    if _text_is_zero(body):
        return "±0/小时"
    return ("+" if per_hour > 0 else "-") + body + "/小时"


# ---------- 历史分析的显示类别（由 kind 派生，供统计条与图表选择口径） ----------
# 水位量：正常显示数值、回归增速与趋势判定
VALUE_CLASS_LEVEL = "level"
# 累计量：图表画每秒速率，统计改为平均/峰值速率（CPU 累计时间同属此类）
VALUE_CLASS_RATE = "rate"
# 历史峰值：只增不减，不外推增速，统计"峰值抬升"次数与幅度
VALUE_CLASS_PEAK = "peak"

_RATE_CLASS_KINDS = frozenset({
    MetricKind.CUMULATIVE_COUNT, MetricKind.CUMULATIVE_BYTES, MetricKind.SECONDS,
})

# 单进程对象数上限（Windows 默认配额），用于显示"已用 x%"
OBJECT_COUNT_LIMIT = 10000
_OBJECT_LIMITED_METRICS = frozenset({
    MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS,
})


def value_class(metric_type: Optional[str]) -> str:
    """指标在历史分析里的显示类别：VALUE_CLASS_LEVEL / RATE / PEAK。

    None 与未知键按水位量处理（与 is_cumulative_metric 对未知键返回 False 一致）。
    """
    kind = _kind_of(metric_type)
    if kind is MetricKind.PEAK:
        return VALUE_CLASS_PEAK
    if kind in _RATE_CLASS_KINDS:
        return VALUE_CLASS_RATE
    return VALUE_CLASS_LEVEL


def object_count_limit(metric_type: Optional[str]) -> Optional[int]:
    """GDI/USER 对象数的单进程上限；其余指标返回 None。"""
    if metric_type is None:
        return None
    if canonical_metric_type(metric_type) in _OBJECT_LIMITED_METRICS:
        return OBJECT_COUNT_LIMIT
    return None
