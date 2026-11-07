"""
存储预估（纯函数）

新建任务时按"指标数 × 采集间隔"估算数据库的增长，提示浸泡测试要留多少磁盘：

- 样本：每个指标每次采集一行，每行按 BYTES_PER_SAMPLE 字节计（存储 v5 实测：
  `samples` 表 WITHOUT ROWID、主键即聚簇，3 天 5 指标 1 秒周期的合成库约 18 字节/行）；
- 分钟汇总：每个指标每个有样本的自然分钟一行（rollup_1m），每行按 BYTES_PER_ROLLUP_ROW
  字节计。采集间隔超过 1 分钟时并非每分钟都有样本，汇总行数随之减少。

结果是量级提示，不是精确值（不计打点、空洞记录与 WAL 的临时增长）。
"""
from dataclasses import dataclass

# 每个样本行的平均占用（字节），v5 实测
BYTES_PER_SAMPLE = 18
# 每个分钟汇总行的平均占用（字节）：16 列数值 + 主键与页内开销的估计
BYTES_PER_ROLLUP_ROW = 120

SECONDS_PER_HOUR = 3600
MINUTES_PER_HOUR = 60

_KB = 1024
_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024

# ---------- 文案 ----------
ESTIMATE_TEMPLATE = "每小时约 {rows} 行 · 12 小时约 {size_12h}"
ESTIMATE_TOOLTIP_TEMPLATE = (
    "按 {metrics} 项指标、每 {interval:g} 秒采集一次估算：\n"
    "每小时约 {rows} 行样本，数据库约增长 {size_1h}；12 小时约 {size_12h}，"
    "24 小时约 {size_24h}。\n"
    "样本每行约 {sample_bytes} 字节，另有每指标每分钟一行的汇总（约 {rollup_bytes} 字节）；"
    "是量级提示，不含打点与临时日志文件。")


@dataclass(frozen=True)
class StorageEstimate:
    """存储预估结果。

    Attributes:
        rows_per_hour: 每小时新增的样本行数（不含汇总行）
        bytes_per_hour: 每小时数据库增长的字节数（样本 + 汇总）
        bytes_12h: 12 小时的增长字节数
        bytes_24h: 24 小时的增长字节数
    """
    rows_per_hour: int
    bytes_per_hour: int
    bytes_12h: int
    bytes_24h: int


def estimate(metric_count: int, interval_seconds: float) -> StorageEstimate:
    """按指标数与采集间隔（秒）估算存储增长；指标数或间隔不是正数时全部为 0。"""
    if metric_count <= 0 or not interval_seconds or interval_seconds <= 0:
        return StorageEstimate(0, 0, 0, 0)
    samples_per_hour = SECONDS_PER_HOUR / float(interval_seconds)
    rows = metric_count * samples_per_hour
    # 每分钟最多一行汇总；间隔超过 1 分钟时有样本的分钟数就是每小时的采集次数
    rollup_rows = metric_count * min(float(MINUTES_PER_HOUR), samples_per_hour)
    per_hour = rows * BYTES_PER_SAMPLE + rollup_rows * BYTES_PER_ROLLUP_ROW
    return StorageEstimate(
        rows_per_hour=int(round(rows)),
        bytes_per_hour=int(round(per_hour)),
        bytes_12h=int(round(per_hour * 12)),
        bytes_24h=int(round(per_hour * 24)),
    )


def format_size(size: int) -> str:
    """字节数 → 约数文案：不足 1 MB 用 KB，不足 1 GB 用整数 MB，否则保留一位小数的 GB。"""
    if size < _MB:
        return f"{max(1, round(size / _KB)):,} KB"
    if size < _GB:
        return f"{max(1, round(size / _MB)):,} MB"
    return f"{size / _GB:.1f} GB"


def format_estimate(result: StorageEstimate) -> str:
    """一行提示，如"每小时约 18,000 行 · 12 小时约 4 MB"；没有可估的（0 行）时为空串。"""
    if result.rows_per_hour <= 0:
        return ""
    return ESTIMATE_TEMPLATE.format(rows=f"{result.rows_per_hour:,}",
                                    size_12h=format_size(result.bytes_12h))


def estimate_tooltip(metric_count: int, interval_seconds: float) -> str:
    """悬停说明：估算依据与 1 / 12 / 24 小时的增长；没有可估的时为空串。"""
    result = estimate(metric_count, interval_seconds)
    if result.rows_per_hour <= 0:
        return ""
    return ESTIMATE_TOOLTIP_TEMPLATE.format(
        metrics=metric_count, interval=float(interval_seconds),
        rows=f"{result.rows_per_hour:,}", size_1h=format_size(result.bytes_per_hour),
        size_12h=format_size(result.bytes_12h), size_24h=format_size(result.bytes_24h),
        sample_bytes=BYTES_PER_SAMPLE, rollup_bytes=BYTES_PER_ROLLUP_ROW)
