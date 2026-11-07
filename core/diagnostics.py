"""
诊断信息与诊断包、启动阶段计时

- collect_diagnostics(ctx, **部件)：一段纯文本（中文标签），供设置页"复制诊断信息"：
  版本与运行环境、数据目录、数据库（大小、结构版本、meta）、任务数与各任务数据质量、
  快照中心统计、自身监控最近值与判定、界面看门狗、崩溃日志大小、最近 50 行日志；
- write_diagnostic_bundle(directory, ctx, **部件)：zip 诊断包 = 上述文本 + app.log 最近
  2 MB + crash.log（及 crash.log.1）+ config.json；
- StartupTrace：--startup-trace 用的启动阶段计时（mark(阶段) 记单调时钟差，report() 出文本）。

部件一律鸭子类型、全部可选：ctx 上同名属性（paths / db / manager / hub / self_monitor /
watchdog）与关键字参数二选一，关键字优先。本模块不导入 app 与 ui；任何一节取值失败只在
该节写"读取失败: 原因"，不影响其余各节。
"""
import io
import logging
import os
import platform
import sys
import time
import zipfile
from datetime import datetime
from typing import Any, Callable, List, Optional, Sequence, Tuple

import config

logger = logging.getLogger(__name__)

LOG_TAIL_LINES = 50
BUNDLE_LOG_TAIL_BYTES = 2 * 1024 * 1024
# 诊断信息里逐个列出质量的任务数上限（最近开始的在前）
TASK_QUALITY_LIMIT = 20
DIAGNOSTICS_TEXT_NAME = 'diagnostics.txt'
BUNDLE_PREFIX = 'diagnostics_'
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'

_PARTS = ('paths', 'db', 'manager', 'hub', 'self_monitor', 'watchdog')


def _part(ctx: Any, overrides: dict, name: str) -> Any:
    if overrides.get(name) is not None:
        return overrides[name]
    return getattr(ctx, name, None) if ctx is not None else None


def _section(title: str, body: Callable[[], List[str]]) -> List[str]:
    try:
        lines = body()
    except Exception as e:   # 一节失败不连累其他节
        logger.warning("诊断信息「%s」读取失败", title, exc_info=True)
        lines = [f"读取失败: {e}"]
    return [f"【{title}】", *(f"  {line}" for line in lines), ""]


def _size_text(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "不存在"
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / 1024 / 1024:.1f} MB"


def _file_size(path: Optional[str]) -> Optional[int]:
    if not path:
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _log_dir(paths: Any, overrides: dict) -> Optional[str]:
    if overrides.get('log_dir'):
        return overrides['log_dir']
    if paths is not None and getattr(paths, 'log_dir', None):
        return paths.log_dir
    return None


def tail_lines(path: str, count: int = LOG_TAIL_LINES, max_bytes: int = 256 * 1024) -> List[str]:
    """文本文件的最后 count 行（只读末尾 max_bytes，避免大日志整份读入）"""
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.decode('utf-8', errors='replace').splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]   # 第一行可能被截断
    return lines[-count:]


# ---------------------------------------------------------------- 各节内容


def _versions() -> List[str]:
    lines = [f"应用版本: {config.APP_VERSION}",
             f"Python: {sys.version.split()[0]} ({platform.architecture()[0]})"]
    try:
        from PyQt5.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
        lines.append(f"Qt: {QT_VERSION_STR}，PyQt: {PYQT_VERSION_STR}")
    except Exception:
        lines.append("Qt: 未知")
    try:
        import psutil
        lines.append(f"psutil: {psutil.__version__}")
    except Exception:
        lines.append("psutil: 未知")
    lines.append(f"操作系统: {platform.platform()}")
    lines.append(f"打包运行: {'是' if getattr(sys, 'frozen', False) else '否'}")
    return lines


def _paths_lines(paths: Any) -> List[str]:
    if paths is None:
        return ["未提供"]
    lines = []
    for label, attr in (("数据目录", 'data_dir'), ("目录来源", 'source'),
                        ("数据库", 'db_path'), ("日志目录", 'log_dir'),
                        ("配置文件", 'config_path')):
        value = getattr(paths, attr, None)
        if value:
            lines.append(f"{label}: {value}")
    return lines or ["未提供"]


def _db_lines(db: Any) -> List[str]:
    if db is None:
        return ["未提供"]
    path = getattr(db, 'db_path', None)
    size = _file_size(path)
    wal = _file_size(f"{path}-wal") if path else None
    lines = [f"路径: {path}",
             f"大小: {_size_text(size)}（-wal {_size_text(wal)}）"]
    flags = [name for name, attr in (("只读", 'read_only'), ("结构不匹配", 'schema_mismatch'))
             if getattr(db, attr, False)]
    if flags:
        lines.append(f"状态: {'、'.join(flags)}")
    read_meta = getattr(db, 'read_meta', None)
    meta = read_meta() if callable(read_meta) else None
    if meta is not None:
        lines.append(f"结构版本: {getattr(meta, 'schema_version', None)}，"
                     f"建库版本: {getattr(meta, 'created_by_version', None)}，"
                     f"最近写入版本: {getattr(meta, 'last_writer_version', None)}，"
                     f"最低可读版本: {getattr(meta, 'min_reader_version', None)}")
    return lines


def _quality_line(quality: Any) -> str:
    from core.report import quality_text
    text = quality_text(quality)
    if not text:
        return "无质量数据"
    extra = []
    if getattr(quality, 'collect_p95_ms', None) is not None:
        extra.append(f"采集 p95 {quality.collect_p95_ms} ms")
    if getattr(quality, 'write_p95_ms', None) is not None:
        extra.append(f"写库 p95 {quality.write_p95_ms} ms")
    return text + (f"，{'，'.join(extra)}" if extra else "")


def _tasks_lines(db: Any, manager: Any) -> List[str]:
    lines: List[str] = []
    if manager is not None:
        workers = list(manager.get_all_tasks()) if hasattr(manager, 'get_all_tasks') else []
        lines.append(f"内存中任务: {len(workers)}")
        for worker in workers:
            state = getattr(getattr(worker, 'state', None), 'value', '?')
            snapshot = getattr(worker, 'quality_snapshot', None)
            quality = snapshot() if callable(snapshot) else None
            lines.append(f"  {str(worker.task_id)[:8]} {worker.process_name} "
                         f"PID {worker.pid} [{state}] {_quality_line(quality)}")
    if db is not None and getattr(db, 'tasks', None) is not None:
        tasks = db.tasks.list_all()
        with_quality = [t for t in tasks if getattr(t, 'quality', None) is not None]
        lines.append(f"库中任务: {len(tasks)}（有数据质量记录 {len(with_quality)}）")
        for task in with_quality[:TASK_QUALITY_LIMIT]:
            started = task.start_time.strftime(TIME_FORMAT) if task.start_time else '-'
            lines.append(f"  {task.task_id[:8]} {task.process_name} {started} "
                         f"{_quality_line(task.quality)}")
    return lines or ["未提供"]


def _hub_lines(hub: Any) -> List[str]:
    if hub is None:
        return ["未提供"]
    s = hub.stats()
    lines = [f"取样 {s.samples} 次，命中缓存 {s.hits} 次，失败 {s.failures} 次，"
             f"平均 {s.avg_ms:.1f} ms，最近 {s.last_ms:.1f} ms"]
    available = getattr(hub, 'available', None)
    if callable(available) and not available():
        lines.append(f"不可用: {getattr(hub, 'unavailable_reason', None)}")
    return lines


def _self_lines(self_monitor: Any) -> List[str]:
    if self_monitor is None:
        return ["未提供"]
    latest = self_monitor.latest()
    samples = self_monitor.snapshot()
    if latest is None:
        return [f"暂无样本（失败 {getattr(self_monitor, 'failures', 0)} 次）"]

    def _num(value, suffix=''):
        return '-' if value is None else f"{value:,.0f}{suffix}"

    lines = [f"最近 {latest.timestamp.strftime(TIME_FORMAT)}: 私有字节 {_num(latest.private_kb, ' KB')}，"
             f"句柄 {_num(latest.handles)}，线程 {_num(latest.threads)}，"
             f"GDI {_num(latest.gdi)}，USER {_num(latest.user)}",
             f"缓冲样本: {len(samples)} / {self_monitor.capacity}"]
    judgement = self_monitor.verdict()
    lines.append(f"私有字节判定: {judgement.verdict.value}（{judgement.reason}，"
                 f"置信度 {judgement.confidence:.2f}）")
    return lines


def _watchdog_lines(watchdog: Any) -> List[str]:
    if watchdog is None:
        try:
            from utils.crash_handler import active_ui_watchdog
            watchdog = active_ui_watchdog()
        except Exception:
            watchdog = None
    if watchdog is None:
        return ["未安装"]
    since = watchdog.seconds_since_beat()
    return [f"{'运行中' if watchdog.running else '已停止'}，阈值 {watchdog.timeout_s:g} 秒，"
            f"续期 {watchdog.beats} 次，距上次续期 "
            f"{'-' if since is None else f'{since:.1f} 秒'}"]


def _crash_lines(log_dir: Optional[str]) -> List[str]:
    if not log_dir:
        return ["日志目录未知"]
    return [f"{name}: {_size_text(_file_size(os.path.join(log_dir, name)))}"
            for name in ('crash.log', 'crash.log.1')]


def _log_tail(log_dir: Optional[str]) -> List[str]:
    if not log_dir:
        return ["日志目录未知"]
    path = os.path.join(log_dir, 'app.log')
    if not os.path.exists(path):
        return [f"{path} 不存在"]
    return tail_lines(path, LOG_TAIL_LINES)


# ---------------------------------------------------------------- 对外接口


def collect_diagnostics(ctx: Any = None, *, clock=None, **parts: Any) -> str:
    """
    诊断信息纯文本。

    Args:
        ctx: 任意带 paths / db / manager / hub / self_monitor / watchdog 属性的对象（可缺）
        clock: 提供 now() 的时钟（生成时刻），None 用 datetime.now
        **parts: 同名部件，优先于 ctx；另可给 log_dir（没有 paths 时定位日志）
    """
    unknown = set(parts) - set(_PARTS) - {'log_dir'}
    if unknown:
        raise TypeError(f"未知的诊断部件: {sorted(unknown)}")
    paths = _part(ctx, parts, 'paths')
    db = _part(ctx, parts, 'db')
    manager = _part(ctx, parts, 'manager')
    hub = _part(ctx, parts, 'hub')
    if hub is None and manager is not None and hasattr(manager, 'snapshot_hub'):
        try:
            hub = manager.snapshot_hub
            # MonitorManager.snapshot_hub 是属性；替身可能给的是返回快照中心的函数
            if not hasattr(hub, 'stats') and callable(hub):
                hub = hub()
        except Exception:
            hub = None
    self_monitor = _part(ctx, parts, 'self_monitor')
    watchdog = _part(ctx, parts, 'watchdog')
    log_dir = _log_dir(paths, parts)
    now = clock.now() if clock is not None else datetime.now()

    lines = [f"进程监控助手 诊断信息（生成于 {now.strftime(TIME_FORMAT)}）", ""]
    lines += _section("版本与环境", _versions)
    lines += _section("数据目录", lambda: _paths_lines(paths))
    lines += _section("数据库", lambda: _db_lines(db))
    lines += _section("任务与数据质量", lambda: _tasks_lines(db, manager))
    lines += _section("快照中心", lambda: _hub_lines(hub))
    lines += _section("自身资源", lambda: _self_lines(self_monitor))
    lines += _section("界面看门狗", lambda: _watchdog_lines(watchdog))
    lines += _section("崩溃日志", lambda: _crash_lines(log_dir))
    lines += _section(f"最近 {LOG_TAIL_LINES} 行日志", lambda: _log_tail(log_dir))
    return "\n".join(lines).rstrip() + "\n"


def _read_tail_bytes(path: str, max_bytes: int) -> bytes:
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read()


def bundle_members(ctx: Any = None, **parts: Any) -> List[Tuple[str, Optional[str]]]:
    """诊断包里除诊断文本外的文件：[(包内名, 本机路径)]；本机不存在的跳过"""
    paths = _part(ctx, parts, 'paths')
    log_dir = _log_dir(paths, parts)
    members: List[Tuple[str, Optional[str]]] = []
    if log_dir:
        for name in ('app.log', 'crash.log', 'crash.log.1'):
            path = os.path.join(log_dir, name)
            if os.path.isfile(path):
                members.append((name, path))
    config_path = getattr(paths, 'config_path', None) if paths is not None else None
    if config_path and os.path.isfile(config_path):
        members.append(('config.json', config_path))
    return members


def write_diagnostic_bundle(directory: str, ctx: Any = None, *, clock=None,
                            **parts: Any) -> str:
    """
    在 directory 下写一个诊断包 diagnostics_<时间>.zip 并返回其路径。

    内容：diagnostics.txt（collect_diagnostics 的文本）、app.log 最近 2 MB、crash.log
    （及 crash.log.1）、config.json。config.json 只有界面与采集设置，不含需要脱敏的字段。
    先写临时文件再改名，失败不留半个 zip。

    Raises:
        OSError: 目录不可写等
    """
    now = clock.now() if clock is not None else datetime.now()
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, f"{BUNDLE_PREFIX}{now.strftime('%Y%m%d_%H%M%S')}.zip")
    text = collect_diagnostics(ctx, clock=clock, **parts)
    tmp = target + '.tmp'
    try:
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(DIAGNOSTICS_TEXT_NAME, text.encode('utf-8'))
            for name, path in bundle_members(ctx, **parts):
                try:
                    if name == 'app.log':
                        zf.writestr(name, _read_tail_bytes(path, BUNDLE_LOG_TAIL_BYTES))
                    else:
                        zf.write(path, name)
                except OSError as e:
                    # 单个文件读不了（被占用等）不放弃整个包，在包里留一条说明
                    zf.writestr(name + '.error.txt', f"读取失败: {e}".encode('utf-8'))
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    logger.info("已导出诊断包: %s", target)
    return target


# ---------------------------------------------------------------- 启动阶段计时


class StartupTrace:
    """
    启动阶段计时（--startup-trace）：mark(阶段) 记下与上一阶段、与起点的单调时钟差，
    report() 输出文本。组合根在各阶段末尾调用 mark，结束后把 report() 写日志或标准输出。

    Args:
        clock: 提供 monotonic() 的时钟，None 用 time
        origin: 起点读数；None 取构造时刻（组合根可传进程启动后最早记下的读数）
    """

    def __init__(self, clock=None, origin: Optional[float] = None):
        self._clock = clock if clock is not None else time
        self.origin = origin if origin is not None else self._clock.monotonic()
        self._marks: List[Tuple[str, float]] = []

    def mark(self, stage: str) -> float:
        """记一个阶段结束；返回该阶段耗时（秒）"""
        now = self._clock.monotonic()
        previous = self._marks[-1][1] if self._marks else self.origin
        self._marks.append((stage, now))
        return now - previous

    @property
    def stages(self) -> Sequence[Tuple[str, float, float]]:
        """[(阶段, 本阶段毫秒, 累计毫秒)]"""
        result = []
        previous = self.origin
        for stage, at in self._marks:
            result.append((stage, (at - previous) * 1000.0, (at - self.origin) * 1000.0))
            previous = at
        return result

    def report(self) -> str:
        buf = io.StringIO()
        buf.write("启动阶段计时（毫秒）\n")
        stages = self.stages
        width = max((len(stage) for stage, _d, _t in stages), default=4)
        for stage, delta, total in stages:
            buf.write(f"  {stage.ljust(width)}  +{delta:9.1f}  累计 {total:9.1f}\n")
        if not stages:
            buf.write("  （无记录）\n")
        return buf.getvalue()
