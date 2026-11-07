"""
IPC 命令白名单与处理器（CommandDispatcher）

服务端（core.ipc.server.IpcServer）把每条合法请求交给 CommandDispatcher.handle(conn, request)。
只认 COMMANDS 里的命令，每个命令单独校验参数（路径参数必须是绝对路径），不执行任何代码。

门禁顺序：未知命令 → unknown_command；设置里关闭了"允许命令行与脚本控制"→ disabled
（ping / activate 除外；无窗口模式的 quit 也除外——那是结束它的唯一途径）；程序尚未就绪
（数据库、管理器还没组装好）→ not_ready；危险命令（start / export / quit / stop）要求客户端
完整性级别不低于服务端 → forbidden（Windows 上读不到服务端自身的完整性级别时同样拒绝）。

资源上限：每个连接同时最多 MAX_PENDING_PER_CONNECTION 条延迟回复、全局同时最多
MAX_CONCURRENT_VERDICTS 个 verdict 分析，超出回 busy；秒数参数不超过 MAX_SECONDS（30 天），
label / note 有长度上限，超出回 invalid_args。处理器中途抛出任何异常时，本次请求已创建的
延迟回复一律取消（不留下继续发心跳的孤儿）。

| 命令 | 参数 | 结果 |
|---|---|---|
| ping | — | proto, app_version, pid, ready, automation_enabled, headless |
| activate | — | 显示主窗口；无窗口模式回 invalid_state |
| status | — | tasks（快照 + 方案 + 当前轮次 + 数据质量）、finished（含数据质量）、waits |
| start | pid / name / launch 三选一，metrics?, interval?, note?, plan?, wait?, wait_timeout_s? | task_id |
| marker | task_id（或 "all"），label? | markers |
| pause / resume / stop | task_id | — |
| wait | task_id, until（stopped / rounds>=N / elapsed>=S），timeout_s? | reached |
| export | task_id, dir, kinds（csv / report / summary），evidence? | paths |
| verdict | task_id | verdict, metrics |
| quit | — | 回包后走真退出路径 |

launch = {exe, args?, cwd?, close_on_stop?}：先启动被测程序（exe 与 cwd 必须是绝对路径），用拿到的
PID 与创建时间建任务；close_on_stop 为真时，任务因用户停止（stop 命令、方案的停止条件）结束后
在后台关闭该程序（先 WM_CLOSE，等不到再结束进程，PID 已被复用则不动）。

wait、export、verdict 与 start（等待进程启动时）是延迟回复：回复前每 5 秒发一行心跳事件，
客户端断开即取消。全部在界面线程执行（与 MonitorManager 同线程），读库与写文件在后台线程。
"""
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from core import verdict as verdict_rules
from core.automation.plan import AutomationPlan, PlanFormatError, is_absolute_path
from core.contracts import StopReason, TaskStatus
from core.ipc import protocol
from core.ipc.protocol import (
    ERR_BUSY, ERR_DISABLED, ERR_FORBIDDEN, ERR_INTERNAL, ERR_INVALID_ARGS, ERR_INVALID_STATE,
    ERR_NOT_FOUND, ERR_NOT_READY, ERR_TIMEOUT, ERR_UNKNOWN_COMMAND, EVENT_HEARTBEAT,
    PROTOCOL_VERSION, SUPPORTED_VERSIONS, Request,
)
from core.monitor_manager import default_marker_kind
from core.monitor_task import SYSTEM_CLOCK
from core.wait_service import WAIT_FAILED_TIMEOUT, WaitMatch, WaitService
from core.workers.base import BackgroundTask, CancelToken
from data.errors import StorageError

logger = logging.getLogger(__name__)

# ---------- 命令名 ----------
CMD_PING = 'ping'
CMD_ACTIVATE = 'activate'
CMD_STATUS = 'status'
CMD_START = 'start'
CMD_MARKER = 'marker'
CMD_PAUSE = 'pause'
CMD_RESUME = 'resume'
CMD_STOP = 'stop'
CMD_WAIT = 'wait'
CMD_EXPORT = 'export'
CMD_VERDICT = 'verdict'
CMD_QUIT = 'quit'

# 关闭自动化后仍可用的命令
ALWAYS_ALLOWED = frozenset({CMD_PING, CMD_ACTIVATE})
# 无窗口模式下关闭自动化后仍可用的命令：quit 是结束无窗口实例的唯一途径
HEADLESS_ALWAYS_ALLOWED = ALWAYS_ALLOWED | {CMD_QUIT}
# 要求客户端完整性级别不低于服务端的命令
DANGEROUS_COMMANDS = frozenset({CMD_START, CMD_EXPORT, CMD_QUIT, CMD_STOP})

# ---------- 取值 ----------
EXPORT_KIND_CSV = 'csv'
EXPORT_KIND_REPORT = 'report'
EXPORT_KIND_SUMMARY = 'summary'
EXPORT_KINDS = (EXPORT_KIND_CSV, EXPORT_KIND_REPORT, EXPORT_KIND_SUMMARY)
MARKER_ALL = 'all'
UNTIL_STOPPED = 'stopped'
_UNTIL_ROUNDS = re.compile(r'^\s*rounds\s*>=\s*(\d+)\s*$')
_UNTIL_ELAPSED = re.compile(r'^\s*elapsed\s*>=\s*(\d+(?:\.\d+)?)\s*$')

# 判定取值与规则统一在 core.verdict；这里再导出，保持原有名字可用
VERDICT_STABLE = verdict_rules.VERDICT_STABLE
VERDICT_RISING = verdict_rules.VERDICT_RISING
VERDICT_INSUFFICIENT = verdict_rules.VERDICT_INSUFFICIENT
VERDICT_TARGET_EXITED = verdict_rules.VERDICT_TARGET_EXITED
# 指标没有趋势判定（非水位量）
METRIC_NOT_JUDGED = verdict_rules.METRIC_NOT_JUDGED
# 视为"被测进程退出"的停止原因
TARGET_EXIT_REASONS = frozenset(r for r in StopReason if verdict_rules.is_target_exit(r))

# ---------- 上限 ----------
# 秒数参数（wait_timeout_s、timeout_s、until elapsed>=S）的上限：30 天
MAX_SECONDS = 30 * 24 * 3600
# 每个连接同时进行中的延迟回复（wait / export / verdict / start wait=true）上限
MAX_PENDING_PER_CONNECTION = 4
# 同时进行中的 verdict 分析上限（每个都要在后台读全量数据）
MAX_CONCURRENT_VERDICTS = 2
# 文本参数的长度上限（字符）
MAX_LABEL_CHARS = 200
MAX_NOTE_CHARS = 2000
# PID 的取值上限（DWORD）
MAX_PID = 0xFFFFFFFF

# 心跳间隔、等待条件的检查间隔与等待进程启动的轮询间隔（毫秒）
HEARTBEAT_INTERVAL_MS = 5000
WAIT_CHECK_INTERVAL_MS = 250
WAIT_POLL_EVERY_TICKS = 4
# 后台线程在退出路径上的名称与等待上限
EXPORT_THREAD_NAME = "命令行导出"
ANALYSIS_THREAD_NAME = "命令行分析"
THREAD_TIMEOUT_MS = 2000
# 导出与判定的数据范围（与分析页的"全部"一致）
ANALYSIS_RANGE_KEY = 'all'
EXPORT_RANGE_TEXT = "全部（命令行导出）"

# ---------- 文案 ----------
MSG_UNKNOWN_COMMAND = "未知命令：{cmd}，可用命令：{commands}"
MSG_DISABLED = "已在设置里关闭“允许命令行与脚本控制”"
MSG_NOT_READY = "程序仍在启动，请稍后再试"
MSG_FORBIDDEN = "该命令需要与进程监控助手相同或更高的权限（以管理员身份运行的程序不接受普通权限的控制）"
MSG_HEADLESS_ACTIVATE = "无窗口模式没有主窗口可显示"
MSG_ARG_REQUIRED = "缺少参数 {name}"
MSG_ARG_TYPE = "参数 {name} 必须是{kind}"
MSG_ARG_RANGE = "参数 {name} 必须在 {low:g} 到 {high:g} 之间"
MSG_ARG_TOO_LARGE = "参数 {name} 不能超过 {limit:g}"
MSG_ARG_TOO_LONG = "参数 {name} 不能超过 {limit} 个字符"
MSG_SELF_PROCESS = "不能监控本程序自身（PID {pid}）"
MSG_TOO_MANY_PENDING = "同一连接上进行中的等待/导出/判定请求已达上限 {limit} 条，请等前面的完成"
MSG_VERDICT_BUSY = "同时进行中的判定已达上限 {limit} 个，请稍后再试"
MSG_IDENTITY_UNKNOWN = "无法确认进程监控助手自身的权限级别，出于安全拒绝该命令"
MSG_PID_OR_NAME = "start 需要 pid 与 name 二选一"
MSG_TARGET_REQUIRED = "start 需要 pid、name、launch 三者之一"
MSG_LAUNCH_INVALID = "launch 有误：{detail}"
MSG_LAUNCH_NO_WAIT = "launch 不能与 wait 同时使用（程序由本次命令启动，不需要等待）"
MSG_LAUNCH_FAILED = "启动被测程序失败：{detail}"
MSG_UNKNOWN_METRIC = "未知的指标：{metrics}"
MSG_NO_METRICS = "metrics 不能为空"
MSG_PROCESS_NOT_FOUND = "进程 PID {pid} 不存在或无法访问"
MSG_NAME_NOT_FOUND = "没有名为 {name} 的运行中进程（可加 wait=true 等它启动）"
MSG_NAME_AMBIGUOUS = "有 {count} 个名为 {name} 的进程，请改用 pid 指定：{pids}"
MSG_PLAN_INVALID = "自动化方案有误：{detail}"
MSG_READ_ONLY = "数据库只读：{reason}"
MSG_START_FAILED = "无法创建监控任务：{reason}"
MSG_MAINTENANCE = "正在清理数据库，请稍后再试"
MSG_TASK_LIMIT = "最多只能同时监控 {limit} 个任务"
MSG_TASK_NOT_FOUND = "任务 {task_id} 不存在"
MSG_TASK_NOT_ACTIVE = "任务 {task_id} 不在运行中"
MSG_NO_ACTIVE_TASK = "没有运行中的任务"
MSG_MARKER_FAILED = "打点失败（{task_id}），详见日志"
MSG_PAUSE_FAILED = "任务 {task_id} 无法暂停（未在运行或已暂停）"
MSG_RESUME_FAILED = "任务 {task_id} 无法恢复（未在暂停）"
MSG_STOP_FAILED = "任务 {task_id} 已在停止或已结束"
MSG_UNTIL_INVALID = "until 只能是 stopped、rounds>=N 或 elapsed>=S"
MSG_WAIT_START_TIMEOUT = "等待进程 {name} 启动超时"
MSG_EXPORT_KINDS = "kinds 只能取 csv、report、summary（至少一项）"
MSG_EVIDENCE_UNSUPPORTED = "尚未支持证据包（evidence）"
MSG_PATH_NOT_ABSOLUTE = "{name} 必须是完整的绝对路径"
MSG_EXPORT_BUSY = "上一次命令行导出尚未完成"
MSG_EXPORT_NO_DATA = "任务 {task_id} 还没有采集到数据"
MSG_EXPORT_DIR_FAILED = "无法创建导出目录：{detail}"
MSG_EXPORT_MAINTENANCE = "数据库维护中，暂时无法导出"
MSG_EXPORT_NO_RENDERER = "当前模式不能生成报告图片"
MSG_EXPORT_RENDER_FAILED = "生成报告图片失败：{detail}"
MSG_ANALYSIS_FAILED = "分析数据失败：{detail}"


class CommandError(Exception):
    """处理器内的预期失败：直接回 ok:false。"""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


# ---------- 进程查询 ----------

class CatalogLookup:
    """生产实现：委托 core.process_catalog 与 psutil（只读）。"""

    def find_pids_by_name(self, name: str) -> List[Tuple[int, Optional[float]]]:
        from core import process_catalog
        return process_catalog.find_pids_by_name(name)

    def identity_of(self, pid: int) -> Optional[Tuple[str, Optional[float]]]:
        """(进程名, 创建时间)；进程不存在或无权访问为 None。"""
        import psutil
        try:
            process = psutil.Process(pid)
            name = process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OverflowError):
            return None
        try:
            create_time = process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            create_time = None
        return name, create_time


# ---------- 运行环境 ----------

@dataclass
class CommandSettings:
    """命令处理器读取的用户设置（组合根从 app_config 接入；测试给定值）。"""
    automation_enabled: Callable[[], bool] = lambda: True
    default_metrics: Callable[[], Sequence[str]] = lambda: ()
    default_interval: Callable[[], float] = lambda: 1.0
    interval_range: Callable[[], Tuple[float, float]] = lambda: (1.0, 3600.0)
    task_limit: int = 5


@dataclass
class CommandContext:
    """就绪后命令处理器用到的长生命周期对象（组合根组装）。"""
    manager: Any
    db: Any
    auto_marker: Any
    registry: Any = None
    clock: Any = SYSTEM_CLOCK
    process_lookup: Any = field(default_factory=CatalogLookup)
    # quit 的执行函数：有窗口时等价于托盘"退出"，无窗口时结束事件循环
    quit: Optional[Callable[[], None]] = None
    # 报告图片的渲染函数 (task, analysis, now) -> QImage；None 表示不能导出报告图片
    report_renderer: Optional[Callable[..., Any]] = None
    # 无窗口模式：带"停止后自动导出"的方案由这里导出（有窗口时由监控页处理）
    handle_auto_export: bool = False
    # 启动 / 关闭被测程序（core.process_launcher.ProcessLauncher 或替身）；None 时现建生产实现
    launcher: Any = None
    # 后台线程工厂（测试替换）
    analysis_worker_factory: Optional[Callable[..., Any]] = None
    export_worker_factory: Optional[Callable[..., Any]] = None


# ---------- 参数校验 ----------

def _require(args: Dict[str, Any], name: str) -> Any:
    if name not in args or args[name] is None:
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_REQUIRED.format(name=name))
    return args[name]


def _string(args: Dict[str, Any], name: str, *, required: bool = False,
            default: Optional[str] = None, max_length: Optional[int] = None) -> Optional[str]:
    value = _require(args, name) if required else args.get(name, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name=name, kind="字符串"))
    if max_length is not None and len(value) > max_length:
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TOO_LONG.format(name=name, limit=max_length))
    return value


def _integer(args: Dict[str, Any], name: str, *, required: bool = False) -> Optional[int]:
    value = _require(args, name) if required else args.get(name)
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name=name, kind="整数"))
    return value


def _number(args: Dict[str, Any], name: str, *, required: bool = False,
            maximum: Optional[float] = None) -> Optional[float]:
    value = _require(args, name) if required else args.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name=name, kind="数字"))
    try:
        number = float(value)
    except OverflowError:
        # 超大整数（JSON 允许任意位数）转不成浮点数
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name=name, kind="数字")) from None
    if not math.isfinite(number):
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name=name, kind="数字"))
    if maximum is not None and number > maximum:
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TOO_LARGE.format(name=name, limit=maximum))
    return number


def _positive_seconds(args: Dict[str, Any], name: str) -> Optional[float]:
    """可选的正秒数参数（≤ MAX_SECONDS）。"""
    value = _number(args, name, maximum=MAX_SECONDS)
    if value is not None and value <= 0:
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name=name, kind="正数"))
    return value


def _boolean(args: Dict[str, Any], name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name=name, kind="true 或 false"))
    return value


def _task_id(args: Dict[str, Any]) -> str:
    value = _string(args, 'task_id', required=True)
    if not value.strip():
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_REQUIRED.format(name='task_id'))
    return value.strip()


def parse_until(text: Any) -> Tuple[str, Optional[float]]:
    """wait 的 until：('stopped', None) / ('rounds', N) / ('elapsed', S)；不合法抛 CommandError。"""
    if not isinstance(text, str):
        raise CommandError(ERR_INVALID_ARGS, MSG_UNTIL_INVALID)
    if text.strip() == UNTIL_STOPPED:
        return UNTIL_STOPPED, None
    match = _UNTIL_ROUNDS.match(text)
    if match and int(match.group(1)) >= 1:
        return 'rounds', float(int(match.group(1)))
    match = _UNTIL_ELAPSED.match(text)
    if match and 0 < float(match.group(1)) <= MAX_SECONDS:
        return 'elapsed', float(match.group(1))
    raise CommandError(ERR_INVALID_ARGS, MSG_UNTIL_INVALID)


def _clean_number(value: Any) -> Any:
    """JSON 不允许 NaN / Infinity：非有限浮点数换成 None。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec='milliseconds') if value is not None else None


def quality_dict(quality) -> Optional[Dict[str, Any]]:
    """数据质量（QualityStats）→ status 里的 quality：一句话文本 + 原始数值；没有为 None。"""
    if quality is None:
        return None
    from core.report import quality_text
    return {
        'text': quality_text(quality) or None,
        'planned_cycles': quality.planned_cycles,
        'done_cycles': quality.done_cycles,
        'missed_cycles': quality.missed_cycles,
        'completion_ratio': _clean_number(quality.completion_ratio),
        'lag_p95_ms': quality.lag_p95_ms,
        'collect_p95_ms': quality.collect_p95_ms,
        'write_p95_ms': getattr(quality, 'write_p95_ms', None),
    }


def parse_launch(value: Any) -> Tuple[Any, bool]:
    """start 的 launch 参数 → (LaunchSpec, close_on_stop)；不合法抛 CommandError。"""
    from core.process_launcher import LaunchFormatError, LaunchSpec
    if not isinstance(value, dict):
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name='launch', kind="JSON 对象"))
    data = dict(value)
    close_on_stop = data.pop('close_on_stop', False)
    if close_on_stop is None:
        close_on_stop = False
    if not isinstance(close_on_stop, bool):
        raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(
            name='launch.close_on_stop', kind="true 或 false"))
    try:
        spec = LaunchSpec.from_dict(data)
    except LaunchFormatError as exc:
        raise CommandError(ERR_INVALID_ARGS, MSG_LAUNCH_INVALID.format(detail=exc)) from None
    if not is_absolute_path(spec.exe):
        raise CommandError(ERR_INVALID_ARGS, MSG_PATH_NOT_ABSOLUTE.format(name='launch.exe'))
    if spec.cwd is not None and not is_absolute_path(spec.cwd):
        raise CommandError(ERR_INVALID_ARGS, MSG_PATH_NOT_ABSOLUTE.format(name='launch.cwd'))
    return spec, close_on_stop


# ---------- 判定 ----------

def compute_verdict(analysis, stop_reason: Optional[StopReason] = None, *,
                    stopped_before_condition: bool = True) -> Dict[str, Any]:
    """
    由 TaskAnalysis 汇总判定（只回字符串，退出码由客户端映射；规则在 core.verdict）：
    被测进程在停止条件达成之前退出 → target_exited（优先）；任一指标"持续上涨" → rising；
    没有任何指标得出平稳/波动的结论（全部数据不足或无判定）→ insufficient；否则 stable。

    stopped_before_condition：被测进程退出时停止条件是否尚未满足（处理器按任务的方案、
    时长与轮数算出后传入）；缺省为真——不知道方案时，被测进程退出一律按异常退出处理。
    停止原因是退出类时结果另附 target_exit = {reason, before_stop_condition}：条件已满足后
    才退出的，照常判定，退出信息只放在这里。
    """
    from core.analysis.intervals import mean_round_delta
    from utils.metrics import get_metric_display_name

    metrics: List[Dict[str, Any]] = []
    for key, snapshot in analysis.snapshots.items():
        if snapshot.error is not None:
            raise CommandError(ERR_INTERNAL, MSG_ANALYSIS_FAILED.format(detail=snapshot.error))
        verdict = verdict_rules.metric_verdict(snapshot)
        reason = verdict_rules.metric_reason(snapshot)
        stats = snapshot.stats or {}
        baseline = snapshot.baseline.value if snapshot.baseline is not None else None
        metrics.append({
            'key': key,
            'name': get_metric_display_name(key),
            'verdict': verdict,
            'reason': reason,
            'samples': stats.get('count', snapshot.total_count),
            'first': _clean_number(stats.get('first')),
            'last': _clean_number(stats.get('last')),
            'baseline': _clean_number(baseline),
            'rate_per_hour': _clean_number(stats.get('slope_per_hour')),
            'rate_per_round': _clean_number(mean_round_delta(snapshot.rounds)),
            'rounds': len(snapshot.rounds),
        })
    overall = verdict_rules.overall_verdict(analysis, stop_reason=stop_reason,
                                            stopped_before_condition=stopped_before_condition)
    target_exit = None
    if verdict_rules.is_target_exit(stop_reason):
        target_exit = {'reason': verdict_rules.stop_reason_value(stop_reason),
                       'before_stop_condition': bool(stopped_before_condition)}
    return {'verdict': overall, 'metrics': metrics,
            'stop_reason': verdict_rules.stop_reason_value(stop_reason),
            'target_exit': target_exit}


# ---------- 后台线程 ----------

class TaskAnalysisWorker(BackgroundTask):
    """在后台按"全部"范围逐指标跑 run_history_query，结果为 TaskAnalysis。"""

    def __init__(self, db, task_id: str, metrics: Sequence[str], *, analysis: bool,
                 parent=None):
        super().__init__(parent)
        self.db = db
        self.task_id = task_id
        self.metrics = list(metrics)
        self.analysis = analysis

    def execute(self, token: CancelToken):
        from core.history_query import TaskAnalysis, build_request, run_history_query
        snapshots = {}
        for metric in self.metrics:
            if token.is_cancelled:
                return None
            request = build_request(self.task_id, metric, ANALYSIS_RANGE_KEY,
                                    analysis=self.analysis)
            snapshots[metric] = run_history_query(self.db, request)
        return TaskAnalysis(self.task_id, ANALYSIS_RANGE_KEY, snapshots)


# ---------- 延迟回复 ----------

class _Pending:
    """一条延迟回复：回复前定时发心跳，连接断开即取消（执行 on_cancel）。"""

    def __init__(self, dispatcher: "CommandDispatcher", conn, request: Request, kind: str):
        self.dispatcher = dispatcher
        self.conn = conn
        self.request_id = request.id
        self.kind = kind
        self.done = False
        self.on_cancel: List[Callable[[], None]] = []
        self._timer = QTimer(dispatcher)
        self._timer.setInterval(dispatcher.heartbeat_ms)
        self._timer.timeout.connect(self._beat)
        self._timer.start()
        self._beats = 0
        dispatcher._pending.append(self)
        conn.on_closed(self.cancel)

    def _beat(self) -> None:
        try:
            if self.done:
                return
            if not self.conn.writable:
                self.cancel()
                return
            self._beats += 1
            self.conn.send_event(self.request_id, EVENT_HEARTBEAT,
                                 {'kind': self.kind, 'beats': self._beats})
        except Exception:
            logger.error("发送心跳失败", exc_info=True)

    def _finish(self) -> bool:
        if self.done:
            return False
        self.done = True
        self._timer.stop()
        self._timer.deleteLater()
        if self in self.dispatcher._pending:
            self.dispatcher._pending.remove(self)
        return True

    def ok(self, result: Optional[Dict[str, Any]] = None) -> None:
        if self._finish():
            self.conn.send_ok(self.request_id, result)

    def error(self, code: str, message: str, **extra) -> None:
        if self._finish():
            self.conn.send_error(self.request_id, code, message, **extra)

    def cancel(self) -> None:
        if not self._finish():
            return
        for callback in self.on_cancel:
            try:
                callback()
            except Exception:
                logger.error("取消延迟命令失败: %s", self.kind, exc_info=True)


@dataclass
class _WaitJob:
    pending: _Pending
    task_id: str
    mode: str
    target: Optional[float]
    deadline: Optional[datetime]


@dataclass
class _StartRequest:
    """start wait=true 交给 WaitService 的请求。"""
    name: str
    metrics: Tuple[str, ...]
    interval: float
    note: str
    plan: Optional[AutomationPlan]


# ---------- 导出 ----------

class ExportRun:
    """一次导出：（报告/摘要需要时）后台分析 → 主线程渲染图片 → ReportWorker 写文件。
    begin_export 从开始持有到 ReportWorker 结束（或任一步失败）。"""

    def __init__(self, dispatcher: "CommandDispatcher", task, directory: str,
                 kinds: Sequence[str], on_done: Callable[[Dict[str, Any]], None],
                 on_failed: Callable[[str, str], None]):
        self.dispatcher = dispatcher
        self.task = task
        self.directory = directory
        self.kinds = list(kinds)
        self.on_done = on_done
        self.on_failed = on_failed
        self.cancelled = False
        self.finished = False
        self._workers: List[Any] = []

    @property
    def ctx(self) -> CommandContext:
        return self.dispatcher.context

    def start(self) -> None:
        if EXPORT_KIND_REPORT in self.kinds or EXPORT_KIND_SUMMARY in self.kinds:
            factory = self.ctx.analysis_worker_factory or TaskAnalysisWorker
            worker = factory(self.ctx.db, self.task.task_id, self.task.metric_types,
                             analysis=True)
            worker.result.connect(self._on_analysis)
            worker.failed.connect(lambda message: self._fail(
                ERR_INTERNAL, MSG_ANALYSIS_FAILED.format(detail=message)))
            self._launch(worker, ANALYSIS_THREAD_NAME)
        else:
            self._write(None)

    def cancel(self) -> None:
        self.cancelled = True
        for worker in list(self._workers):
            try:
                worker.cancel()
            except Exception:
                logger.debug("取消导出线程失败", exc_info=True)

    def _launch(self, worker, name: str) -> None:
        self._workers.append(worker)
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        registry = self.ctx.registry
        if registry is not None:
            registry.register(worker, name=name, cancel=worker.cancel, timeout_ms=THREAD_TIMEOUT_MS)
        worker.start()

    def _worker_finished(self, worker) -> None:
        try:
            if worker in self._workers:
                self._workers.remove(worker)
            # 取消（退出路径）时线程不发结果也不发失败：这里补收尾
            if not self._workers and not self.finished and self.cancelled:
                self._release()
        except Exception:
            logger.error("导出线程收尾失败", exc_info=True)

    def _on_analysis(self, analysis) -> None:
        try:
            if self.cancelled:
                self._release()
                return
            self._write(analysis)
        except Exception as exc:
            logger.error("命令行导出失败", exc_info=True)
            self._fail(ERR_INTERNAL, str(exc) or type(exc).__name__)

    def _paths(self) -> Dict[str, str]:
        from core.report import DATA_CSV_SUFFIX, REPORT_IMAGE_SUFFIX, SUMMARY_CSV_SUFFIX, \
            task_basename
        suffixes = {EXPORT_KIND_CSV: DATA_CSV_SUFFIX, EXPORT_KIND_REPORT: REPORT_IMAGE_SUFFIX,
                    EXPORT_KIND_SUMMARY: SUMMARY_CSV_SUFFIX}
        base = os.path.join(self.directory, task_basename(self.task))
        candidate, n = base, 1
        # 不覆盖已有文件：三件套共用同一个编号
        while any(os.path.exists(candidate + suffixes[k]) for k in self.kinds):
            n += 1
            candidate = f"{base}_{n}"
        return {k: candidate + suffixes[k] for k in self.kinds}

    def _write(self, analysis) -> None:
        from core.report import gap_threshold_seconds, metadata_lines, summary_rows
        from core.report_worker import ReportJob, ReportWorker
        ctx = self.ctx
        task = self.task
        paths = self._paths()
        image = None
        if EXPORT_KIND_REPORT in paths:
            try:
                image = ctx.report_renderer(task, analysis, ctx.clock.now())
            except Exception as exc:
                logger.error("命令行导出渲染报告图片失败", exc_info=True)
                self._fail(ERR_INTERNAL, MSG_EXPORT_RENDER_FAILED.format(detail=exc))
                return
        rows = summary_rows(analysis.snapshots) if (
            analysis is not None and EXPORT_KIND_SUMMARY in paths) else []
        running = ctx.manager.is_task_active(task.task_id)
        end_time = None if running else (task.end_time or task.last_sample_at)
        metadata = metadata_lines(task, exported_at=ctx.clock.now(), range_text=EXPORT_RANGE_TEXT,
                                  sample_count=task.sample_count or None, end_time=end_time)
        job = ReportJob(task=task, csv_path=paths.get(EXPORT_KIND_CSV),
                        summary_path=paths.get(EXPORT_KIND_SUMMARY),
                        image_path=paths.get(EXPORT_KIND_REPORT), image=image,
                        metadata=tuple(metadata), summary_rows=tuple(tuple(r) for r in rows),
                        gap_seconds=gap_threshold_seconds(task.interval))
        factory = ctx.export_worker_factory or ReportWorker
        worker = factory(ctx.db, job)
        worker.result.connect(self._on_written)
        worker.failed.connect(lambda message: self._fail(ERR_INTERNAL, message))
        self._launch(worker, EXPORT_THREAD_NAME)

    def _on_written(self, result) -> None:
        try:
            if self._release():
                self.on_done({'paths': list(getattr(result, 'paths', ()) or ()),
                              'rows': getattr(result, 'row_count', 0),
                              'points': getattr(result, 'point_count', 0)})
        except Exception:
            logger.error("命令行导出回包失败", exc_info=True)

    def _fail(self, code: str, message: str) -> None:
        if self._release():
            self.on_failed(code, message)

    def _release(self) -> bool:
        if self.finished:
            return False
        self.finished = True
        try:
            self.ctx.manager.end_export()
        finally:
            self.dispatcher._exports_done(self)
        return True


# ---------- 处理器 ----------

class CommandDispatcher(QObject):
    """命令白名单与处理器（界面线程）。

    信号：
        activation_requested()：收到 activate（有窗口时由组合根连到"显示主窗口"）
    """

    activation_requested = pyqtSignal()

    def __init__(self, *, app_version: str, pid: Optional[int] = None, headless: bool = False,
                 settings: Optional[CommandSettings] = None, identity=None,
                 parent: Optional[QObject] = None,
                 heartbeat_ms: int = HEARTBEAT_INTERVAL_MS,
                 wait_check_ms: int = WAIT_CHECK_INTERVAL_MS):
        """
        Args:
            app_version: ping 报告的程序版本
            pid: ping 报告的进程号（客户端据此与管道服务端 PID 比对）；None 取本进程
            headless: 是否无窗口模式
            settings: 用户设置（自动化开关、默认指标与间隔）；None 用缺省值
            identity: 身份提供者（own_integrity()），用于危险命令的完整性比较；None 不比较
            heartbeat_ms / wait_check_ms: 心跳与等待条件的检查间隔（测试可调小）
        """
        super().__init__(parent)
        self.app_version = app_version
        self.pid = pid if pid is not None else os.getpid()
        self.headless = headless
        self.settings = settings or CommandSettings()
        self.identity = identity
        self.heartbeat_ms = heartbeat_ms
        self.context: Optional[CommandContext] = None
        self.wait_service: Optional[WaitService] = None
        self._pending: List[_Pending] = []
        self._wait_jobs: List[_WaitJob] = []
        # 等待进程启动的 start：{等待 ID: (延迟回复, 进程名)}
        self._start_waits: Dict[str, Tuple[_Pending, str]] = {}
        self._exports: List[ExportRun] = []
        self._stop_reasons: Dict[str, StopReason] = {}
        self._unsaved_plans: Dict[str, AutomationPlan] = {}
        # launch 启动且要求"停止时关闭"的任务：{task_id: (pid, 创建时间)}
        self._close_on_stop: Dict[str, Tuple[int, Optional[float]]] = {}
        self._launcher = None
        self._ticks = 0
        self._timer = QTimer(self)
        self._timer.setInterval(wait_check_ms)
        self._timer.timeout.connect(self._on_timer)
        self._handlers: Dict[str, Callable[[Any, Request], Optional[Dict[str, Any]]]] = {
            CMD_PING: self._cmd_ping,
            CMD_ACTIVATE: self._cmd_activate,
            CMD_STATUS: self._cmd_status,
            CMD_START: self._cmd_start,
            CMD_MARKER: self._cmd_marker,
            CMD_PAUSE: self._cmd_pause,
            CMD_RESUME: self._cmd_resume,
            CMD_STOP: self._cmd_stop,
            CMD_WAIT: self._cmd_wait,
            CMD_EXPORT: self._cmd_export,
            CMD_VERDICT: self._cmd_verdict,
            CMD_QUIT: self._cmd_quit,
        }

    # ---------- 就绪 ----------

    @property
    def ready(self) -> bool:
        return self.context is not None

    @property
    def commands(self) -> List[str]:
        return list(self._handlers)

    def has_pending(self) -> bool:
        """是否有进行中的命令行请求（延迟回复、等待启动的进程、导出）。"""
        return (bool(self._pending) or bool(self._exports)
                or bool(self.wait_service is not None and self.wait_service.has_waits()))

    def set_settings(self, settings: CommandSettings) -> None:
        self.settings = settings

    def set_context(self, context: CommandContext) -> None:
        """组合根组装完成后调用：此后命令不再回 not_ready。"""
        self.context = context
        manager = context.manager
        self.wait_service = WaitService(manager, context.process_lookup, context.clock, self,
                                        id_prefix='ipc-wait-')
        self.wait_service.failed.connect(self._on_wait_failed)
        self.wait_service.started.connect(lambda wait_id, _task_id: self._start_waits.pop(
            wait_id, None))
        manager.task_stopped.connect(self._on_task_stopped)
        manager.data_updated.connect(self._on_data_updated)
        if context.handle_auto_export and context.auto_marker is not None:
            context.auto_marker.export_requested.connect(self._on_auto_export)

    # ---------- 入口 ----------

    def handle(self, conn, request: Request) -> None:
        handler = self._handlers.get(request.cmd)
        if handler is None:
            conn.send_error(request.id, ERR_UNKNOWN_COMMAND, MSG_UNKNOWN_COMMAND.format(
                cmd=request.cmd, commands="、".join(self._handlers)))
            return
        before = list(self._pending)
        try:
            if request.cmd not in ALWAYS_ALLOWED:
                exempt = self.headless and request.cmd in HEADLESS_ALWAYS_ALLOWED
                if not exempt and not self._automation_enabled():
                    raise CommandError(ERR_DISABLED, MSG_DISABLED)
                if not self.ready:
                    raise CommandError(ERR_NOT_READY, MSG_NOT_READY)
            if request.cmd in DANGEROUS_COMMANDS and not self._client_trusted(conn):
                logger.warning("拒绝低完整性客户端的危险命令: cmd=%s pid=%s",
                               request.cmd, conn.client_pid())
                raise CommandError(ERR_FORBIDDEN, MSG_FORBIDDEN)
            result = handler(conn, request)
        except CommandError as exc:
            self._cancel_new_pending(before)
            conn.send_error(request.id, exc.code, exc.message, **exc.extra)
            return
        except StorageError as exc:
            self._cancel_new_pending(before)
            logger.error("IPC 命令读写数据库失败: cmd=%s", request.cmd, exc_info=True)
            conn.send_error(request.id, ERR_INTERNAL, f"数据库读写失败：{exc}")
            return
        except BaseException:
            # 其余异常交给服务端回 internal；本次请求已创建的延迟回复先取消（停心跳、撤等待）
            self._cancel_new_pending(before)
            raise
        if result is not None:
            conn.send_ok(request.id, result)

    def _cancel_new_pending(self, before: Sequence["_Pending"]) -> None:
        """取消本次请求处理中新建、尚未完成的延迟回复（执行其取消回调）。"""
        for pending in [p for p in self._pending if p not in before]:
            try:
                pending.cancel()
            except Exception:
                logger.error("取消延迟回复失败: %s", pending.kind, exc_info=True)

    def _new_pending(self, conn, request: Request, kind: str) -> "_Pending":
        """新建延迟回复：同一连接进行中的延迟回复不超过 MAX_PENDING_PER_CONNECTION 条。
        调用前应完成全部参数校验；之后的步骤若抛异常，handle() 会取消它。"""
        if sum(1 for p in self._pending if p.conn is conn) >= MAX_PENDING_PER_CONNECTION:
            raise CommandError(ERR_BUSY, MSG_TOO_MANY_PENDING.format(
                limit=MAX_PENDING_PER_CONNECTION))
        return _Pending(self, conn, request, kind)

    def _automation_enabled(self) -> bool:
        try:
            return bool(self.settings.automation_enabled())
        except Exception:
            logger.error("读取自动化开关失败，按关闭处理", exc_info=True)
            return False

    def _client_trusted(self, conn) -> bool:
        """客户端完整性级别不低于服务端。客户端的级别取不到按不可信处理；服务端自己的
        级别取不到时，Windows 上同样拒绝（无从比较时不能假定安全：以管理员运行的实例读不到
        自身级别就放行，等于把提权口子留给同用户的普通进程），其他平台放行（管道 DACL 已
        限定为当前用户）。没有身份提供者（测试）时放行。"""
        if self.identity is None:
            return True
        own = self.identity.own_integrity()
        if own is None:
            if sys.platform == 'win32':
                logger.warning(MSG_IDENTITY_UNKNOWN)
                return False
            return True
        client = conn.client_integrity()
        return client is not None and client >= own

    # ---------- 小工具 ----------

    def _read_only_reason(self) -> Optional[str]:
        db = self.context.db
        if getattr(db, 'read_only', False):
            if getattr(db, 'schema_mismatch', False):
                return "数据库尚未升级，本次以只读方式打开"
            return "数据库由更高版本创建，本版本只读"
        return None

    def _reject_if_read_only(self) -> None:
        reason = self._read_only_reason()
        if reason:
            raise CommandError(ERR_INVALID_STATE, MSG_READ_ONLY.format(reason=reason))

    def _rounds(self, task_id: str) -> int:
        return sum(1 for m in self.context.manager.list_markers(task_id) if m.kind == 'user')

    def _plan_of(self, task_id: str) -> Optional[AutomationPlan]:
        auto = self.context.auto_marker
        plan = auto.plan_of(task_id) if auto is not None else None
        return plan if plan is not None else self._unsaved_plans.get(task_id)

    def _task_dict(self, info) -> Dict[str, Any]:
        plan = self._plan_of(info.task_id)
        return {
            'task_id': info.task_id, 'pid': info.pid, 'name': info.process_name,
            'metrics': list(info.metric_types), 'interval': info.interval, 'note': info.note,
            'status': str(info.status), 'paused': info.is_paused, 'stopping': info.is_stopping,
            'start_time': _iso(info.start_time), 'end_time': _iso(info.end_time),
            'rounds': self._rounds(info.task_id),
            'plan': plan.to_dict() if plan is not None else None,
            'quality': quality_dict(info.quality),
        }

    def _stored_quality(self, task_id: str) -> Optional[Dict[str, Any]]:
        """已结束任务收尾时写进库的数据质量；读不到为 None（不影响 status 回复）。"""
        try:
            task = self.context.db.get_task(task_id)
        except Exception:
            logger.error("读取任务数据质量失败: task_id=%s", task_id, exc_info=True)
            return None
        return quality_dict(getattr(task, 'quality', None)) if task is not None else None

    def _sync_timer(self) -> None:
        active = bool(self._wait_jobs) or bool(self.wait_service and self.wait_service.has_waits())
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()

    # ---------- 命令：基础 ----------

    def _cmd_ping(self, conn, request):
        return {'proto': list(SUPPORTED_VERSIONS), 'app_version': self.app_version,
                'pid': self.pid, 'ready': self.ready,
                'automation_enabled': self._automation_enabled(), 'headless': self.headless}

    def _cmd_activate(self, conn, request):
        if self.headless:
            raise CommandError(ERR_INVALID_STATE, MSG_HEADLESS_ACTIVATE)
        self.activation_requested.emit()
        return {'activated': True}

    def _cmd_status(self, conn, request):
        manager = self.context.manager
        tasks = [self._task_dict(info) for info in manager.list_task_infos()]
        live = {t['task_id'] for t in tasks}
        finished = [{'task_id': task_id, 'stop_reason': reason.value,
                     'quality': self._stored_quality(task_id)}
                    for task_id, reason in self._stop_reasons.items() if task_id not in live]
        waits = [{'wait_id': wait_id, 'name': self.wait_service.request_of(wait_id).name}
                 for wait_id in self.wait_service.wait_ids()]
        return {'tasks': tasks, 'finished': finished, 'waits': waits,
                'read_only': self._read_only_reason() is not None}

    # ---------- 命令：开始 ----------

    def _parse_metrics(self, args) -> Tuple[str, ...]:
        from utils.metrics import all_metric_types, canonical_metric_type
        value = args.get('metrics')
        if value is None:
            metrics = list(self.settings.default_metrics())
        else:
            if not isinstance(value, list) or not all(isinstance(m, str) for m in value):
                raise CommandError(ERR_INVALID_ARGS,
                                   MSG_ARG_TYPE.format(name='metrics', kind="字符串数组"))
            known = set(all_metric_types())
            metrics, unknown = [], []
            for item in value:
                key = canonical_metric_type(item.strip())
                if key not in known:
                    unknown.append(item)
                elif key not in metrics:
                    metrics.append(key)
            if unknown:
                raise CommandError(ERR_INVALID_ARGS,
                                   MSG_UNKNOWN_METRIC.format(metrics="、".join(unknown)))
        if not metrics:
            raise CommandError(ERR_INVALID_ARGS, MSG_NO_METRICS)
        return tuple(metrics)

    def _parse_interval(self, args) -> float:
        value = _number(args, 'interval')
        if value is None:
            return float(self.settings.default_interval())
        low, high = self.settings.interval_range()
        if not low <= value <= high:
            raise CommandError(ERR_INVALID_ARGS,
                               MSG_ARG_RANGE.format(name='interval', low=low, high=high))
        return value

    @staticmethod
    def _parse_plan(args, interval: float) -> Optional[AutomationPlan]:
        value = args.get('plan')
        if value is None:
            return None
        if not isinstance(value, dict):
            raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name='plan', kind="JSON 对象"))
        try:
            plan = AutomationPlan.from_dict(value)
        except PlanFormatError as exc:
            raise CommandError(ERR_INVALID_ARGS, MSG_PLAN_INVALID.format(detail=exc)) from None
        errors = plan.validate(interval)
        if errors:
            raise CommandError(ERR_INVALID_ARGS, MSG_PLAN_INVALID.format(detail="；".join(errors)))
        return None if plan.is_empty() else plan

    def _cmd_start(self, conn, request):
        args = request.args
        if args.get('launch') is not None:
            return self._start_launch(args)
        pid = _integer(args, 'pid')
        name = _string(args, 'name')
        if (pid is None) == (name is None or not name.strip()):
            raise CommandError(ERR_INVALID_ARGS, MSG_PID_OR_NAME)
        if pid is not None and not 0 < pid <= MAX_PID:
            raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TYPE.format(name='pid', kind="正整数"))
        self._reject_self(pid)
        metrics = self._parse_metrics(args)
        interval = self._parse_interval(args)
        note = self._parse_note(args)
        plan = self._parse_plan(args, interval)
        wait = _boolean(args, 'wait')
        wait_timeout = _positive_seconds(args, 'wait_timeout_s')
        self._reject_if_read_only()
        lookup = self.context.process_lookup

        if pid is not None:
            identity = lookup.identity_of(pid)
            if identity is None:
                raise CommandError(ERR_NOT_FOUND, MSG_PROCESS_NOT_FOUND.format(pid=pid))
            process_name, create_time = identity
            return self._create(pid, process_name, create_time, metrics, interval, note, plan)

        name = name.strip()
        found = self._find_by_name(name)
        if found is not None:
            return self._create(*found, metrics, interval, note, plan)
        if not wait:
            raise CommandError(ERR_NOT_FOUND, MSG_NAME_NOT_FOUND.format(name=name))
        start = _StartRequest(name, metrics, interval, note, plan)
        pending = self._new_pending(conn, request, CMD_START)
        wait_id = self.wait_service.add(start, starter=lambda wid, match: self._start_waited(
            pending, match), timeout_s=wait_timeout)
        self._start_waits[wait_id] = (pending, name)
        pending.on_cancel.append(lambda: self._cancel_start_wait(wait_id))
        self._sync_timer()
        return None

    @staticmethod
    def _reject_self(pid: Optional[int]) -> None:
        if pid is not None and pid == os.getpid():
            raise CommandError(ERR_INVALID_ARGS, MSG_SELF_PROCESS.format(pid=pid))

    @staticmethod
    def _parse_note(args) -> str:
        return _string(args, 'note', default='', max_length=MAX_NOTE_CHARS) or ''

    @property
    def launcher(self):
        """启动被测程序用的 launcher：上下文给了就用它（与监控页同一份），否则现建一个。"""
        if self.context is not None and self.context.launcher is not None:
            return self.context.launcher
        if self._launcher is None:
            from core.process_launcher import ProcessLauncher
            self._launcher = ProcessLauncher()
        return self._launcher

    def _start_launch(self, args) -> Dict[str, Any]:
        """start launch=…：参数全部校验通过、确认能建任务之后才启动程序（不留下没人监控的
        进程）；启动失败回 invalid_state，不建任务。"""
        if args.get('pid') is not None or args.get('name') is not None:
            raise CommandError(ERR_INVALID_ARGS, MSG_TARGET_REQUIRED)
        spec, close_on_stop = parse_launch(args.get('launch'))
        if _boolean(args, 'wait'):
            raise CommandError(ERR_INVALID_ARGS, MSG_LAUNCH_NO_WAIT)
        metrics = self._parse_metrics(args)
        interval = self._parse_interval(args)
        note = self._parse_note(args)
        plan = self._parse_plan(args, interval)
        self._reject_if_read_only()
        manager = self.context.manager
        if manager.is_maintenance_running():
            raise CommandError(ERR_BUSY, MSG_MAINTENANCE)
        if not manager.can_add_task():
            raise CommandError(ERR_BUSY, MSG_TASK_LIMIT.format(limit=self.settings.task_limit))
        result = self.launcher.launch(spec)
        if not result.ok:
            raise CommandError(ERR_INVALID_STATE, MSG_LAUNCH_FAILED.format(detail=result.error))
        reply = self._create(result.pid, result.name or spec.name, result.create_time,
                             metrics, interval, note, plan)
        if close_on_stop:
            self._close_on_stop[reply['task_id']] = (result.pid, result.create_time)
        reply['launched'] = True
        reply['close_on_stop'] = close_on_stop
        logger.info("命令行启动被测程序并监控: task_id=%s pid=%s %s", reply['task_id'],
                    result.pid, spec.command_line())
        return reply

    def _close_launched(self, task_id: str, reason) -> None:
        """launch 启动、要求停止时关闭的任务结束了：用户停止（含方案的停止条件）才关闭程序，
        被测进程自己退出等其他原因不动。"""
        target = self._close_on_stop.pop(task_id, None)
        if target is None or reason is not StopReason.USER:
            return
        from core.workers.terminate_worker import close_in_background
        pid, create_time = target
        close_in_background(self.launcher, pid, create_time, self.context.registry)

    def _find_by_name(self, name: str):
        """按名找唯一的进程：(pid, 名称, 创建时间)；没有返回 None；多个抛 invalid_args。"""
        from core.wait_service import candidate_names
        lookup = self.context.process_lookup
        for candidate in candidate_names(name):
            matches = lookup.find_pids_by_name(candidate)
            if len(matches) > 1:
                pids = sorted(pid for pid, _ in matches)
                raise CommandError(ERR_INVALID_ARGS, MSG_NAME_AMBIGUOUS.format(
                    count=len(matches), name=candidate, pids="、".join(map(str, pids))),
                    pids=pids)
            if matches:
                pid, create_time = matches[0]
                self._reject_self(pid)
                return pid, candidate, create_time
        return None

    def _create(self, pid: int, name: str, create_time: Optional[float],
                metrics: Sequence[str], interval: float, note: str,
                plan: Optional[AutomationPlan]) -> Dict[str, Any]:
        manager = self.context.manager
        if manager.is_maintenance_running():
            raise CommandError(ERR_BUSY, MSG_MAINTENANCE)
        if not manager.can_add_task():
            raise CommandError(ERR_BUSY, MSG_TASK_LIMIT.format(limit=self.settings.task_limit))
        task_id = manager.create_task(pid, name, list(metrics), interval, note=note,
                                      create_time=create_time)
        if not task_id:
            raise CommandError(ERR_BUSY, MSG_START_FAILED.format(reason="数据库维护中或任务数已满"))
        try:
            if plan is not None:
                self._unsaved_plans[task_id] = plan
                self._save_plan(task_id)
                if self.context.auto_marker is not None:
                    self.context.auto_marker.attach(task_id, plan)
            manager.start_task(task_id)
        except Exception as exc:
            # 挂接方案或启动失败：回滚，不留下一个没人启动、也没人知道的任务
            logger.error("命令行开始监控失败，已回滚任务: task_id=%s", task_id, exc_info=True)
            self._rollback_task(task_id)
            raise CommandError(ERR_INTERNAL, MSG_START_FAILED.format(
                reason=str(exc) or type(exc).__name__)) from None
        logger.info("命令行开始监控: task_id=%s pid=%s name=%s", task_id, pid, name)
        return {'task_id': task_id, 'pid': pid, 'name': name, 'metrics': list(metrics),
                'interval': interval}

    def _rollback_task(self, task_id: str) -> None:
        self._unsaved_plans.pop(task_id, None)
        steps = []
        if self.context.auto_marker is not None:
            steps.append(lambda: self.context.auto_marker.detach(task_id))
        steps.append(lambda: self.context.manager.remove_task(task_id))
        steps.append(lambda: self.context.manager.delete_task_data(task_id))
        for step in steps:
            try:
                step()
            except Exception:
                logger.error("回滚任务失败: task_id=%s", task_id, exc_info=True)

    def _start_waited(self, pending: _Pending, match: WaitMatch) -> Optional[str]:
        """等待的进程出现：与 start name=… 同一套按名解析规则（同名多个回 invalid_args 并
        列出 PID、不能是本程序自身），再建任务。"""
        request = match.request
        try:
            found = self._find_by_name(match.name)
            if found is None:
                found = (match.pid, match.name, match.create_time)
            pid, name, create_time = found
            result = self._create(pid, name, create_time, request.metrics,
                                  request.interval, request.note, request.plan)
        except CommandError as exc:
            pending.error(exc.code, exc.message, **exc.extra)
            return None
        pending.ok(result)
        return result['task_id']

    def _cancel_start_wait(self, wait_id: str) -> None:
        self._start_waits.pop(wait_id, None)
        if self.wait_service is not None:
            self.wait_service.cancel(wait_id)
        self._sync_timer()

    def _on_wait_failed(self, wait_id: str, reason: str) -> None:
        """等待启动失败：超时回 timeout；建任务失败时启动函数已回复，这里只清理。"""
        entry = self._start_waits.pop(wait_id, None)
        if entry is None:
            return
        pending, name = entry
        if reason == WAIT_FAILED_TIMEOUT:
            pending.error(ERR_TIMEOUT, MSG_WAIT_START_TIMEOUT.format(name=name))
        else:
            pending.error(ERR_INTERNAL, MSG_START_FAILED.format(reason=reason))
        self._sync_timer()

    def _save_plan(self, task_id: str) -> None:
        plan = self._unsaved_plans.get(task_id)
        store = getattr(self.context.db, 'tasks', None)
        if plan is None or store is None:
            return
        try:
            if store.save_plan(task_id, plan.to_json()):
                self._unsaved_plans.pop(task_id, None)
        except StorageError:
            logger.error("保存自动化方案失败: task_id=%s", task_id, exc_info=True)

    # ---------- 命令：打点、暂停、恢复、停止 ----------

    def _require_task(self, task_id: str):
        info = self.context.manager.get_task_info(task_id)
        if info is None:
            raise CommandError(ERR_NOT_FOUND, MSG_TASK_NOT_FOUND.format(task_id=task_id))
        return info

    def _cmd_marker(self, conn, request):
        args = request.args
        task_id = _task_id(args)
        label = _string(args, 'label', max_length=MAX_LABEL_CHARS)
        self._reject_if_read_only()
        manager = self.context.manager
        if task_id == MARKER_ALL:
            ids = [info.task_id for info in manager.list_task_infos()
                   if manager.is_task_active(info.task_id)]
            if not ids:
                raise CommandError(ERR_INVALID_STATE, MSG_NO_ACTIVE_TASK)
        else:
            self._require_task(task_id)
            if not manager.is_task_active(task_id):
                raise CommandError(ERR_INVALID_STATE, MSG_TASK_NOT_ACTIVE.format(task_id=task_id))
            ids = [task_id]
        when = self.context.clock.now()
        markers = []
        for tid in ids:
            default_label = manager.next_marker_label(tid)
            text = label.strip() if label and label.strip() else default_label
            marker = manager.add_marker(tid, text, when,
                                        kind=default_marker_kind(default_label, text))
            if marker is None:
                raise CommandError(ERR_INTERNAL, MSG_MARKER_FAILED.format(task_id=tid))
            markers.append({'task_id': tid, 'marker_id': marker.id, 'label': marker.label,
                            'kind': marker.kind, 'timestamp': _iso(marker.timestamp)})
        return {'markers': markers}

    def _cmd_pause(self, conn, request):
        task_id = _task_id(request.args)
        self._require_task(task_id)
        self._reject_if_read_only()
        if not self.context.manager.pause_task(task_id):
            raise CommandError(ERR_INVALID_STATE, MSG_PAUSE_FAILED.format(task_id=task_id))
        self._process_auto(task_id)
        return {'task_id': task_id, 'paused': True}

    def _cmd_resume(self, conn, request):
        task_id = _task_id(request.args)
        self._require_task(task_id)
        self._reject_if_read_only()
        if not self.context.manager.resume_task(task_id):
            raise CommandError(ERR_INVALID_STATE, MSG_RESUME_FAILED.format(task_id=task_id))
        self._process_auto(task_id)
        return {'task_id': task_id, 'paused': False}

    def _process_auto(self, task_id: str) -> None:
        """暂停/恢复后立即让自动打点服务同步（恢复时刻精确到命令发出的那一刻）。"""
        auto = self.context.auto_marker
        if auto is not None:
            auto.process(task_id)

    def _cmd_stop(self, conn, request):
        task_id = _task_id(request.args)
        self._require_task(task_id)
        if not self.context.manager.stop_task(task_id):
            raise CommandError(ERR_INVALID_STATE, MSG_STOP_FAILED.format(task_id=task_id))
        return {'task_id': task_id, 'stopping': True}

    # ---------- 命令：等待 ----------

    def _task_known(self, task_id: str) -> bool:
        if self.context.manager.get_task_info(task_id) is not None:
            return True
        if task_id in self._stop_reasons:
            return True
        try:
            return self.context.db.get_task(task_id) is not None
        except Exception:
            logger.error("查询任务失败: task_id=%s", task_id, exc_info=True)
            return False

    def _cmd_wait(self, conn, request):
        args = request.args
        task_id = _task_id(args)
        mode, target = parse_until(_require(args, 'until'))
        timeout = _positive_seconds(args, 'timeout_s')
        if not self._task_known(task_id):
            raise CommandError(ERR_NOT_FOUND, MSG_TASK_NOT_FOUND.format(task_id=task_id))
        from datetime import timedelta
        try:
            deadline = (self.context.clock.now() + timedelta(seconds=timeout)
                        if timeout is not None else None)
        except OverflowError:
            raise CommandError(ERR_INVALID_ARGS, MSG_ARG_TOO_LARGE.format(
                name='timeout_s', limit=MAX_SECONDS)) from None
        pending = self._new_pending(conn, request, CMD_WAIT)
        job = _WaitJob(pending, task_id, mode, target, deadline)
        self._wait_jobs.append(job)
        pending.on_cancel.append(lambda: self._drop_wait_job(job))
        self._check_wait(job)
        self._sync_timer()
        return None

    def _drop_wait_job(self, job: _WaitJob) -> None:
        if job in self._wait_jobs:
            self._wait_jobs.remove(job)
        self._sync_timer()

    def _wait_state(self, task_id: str) -> Dict[str, Any]:
        info = self.context.manager.get_task_info(task_id)
        stopped = info is None or info.status == TaskStatus.STOPPED
        elapsed = None
        if info is not None and info.start_time is not None:
            elapsed = (self.context.clock.now() - info.start_time).total_seconds()
        reason = self._stop_reasons.get(task_id)
        return {'task_id': task_id, 'stopped': stopped, 'rounds': self._rounds(task_id),
                'elapsed_s': elapsed,
                'stop_reason': reason.value if reason is not None else None}

    def _check_wait(self, job: _WaitJob) -> None:
        if job.pending.done:
            self._drop_wait_job(job)
            return
        state = self._wait_state(job.task_id)
        if job.mode == UNTIL_STOPPED:
            reached = state['stopped']
        elif job.mode == 'rounds':
            reached = state['rounds'] >= job.target
        else:
            reached = state['elapsed_s'] is not None and state['elapsed_s'] >= job.target
        timed_out = job.deadline is not None and self.context.clock.now() >= job.deadline
        # 等轮数/时长时任务已经结束：再等也不会满足，按"未达到"回复
        if reached or timed_out or state['stopped']:
            self._drop_wait_job(job)
            state['reached'] = bool(reached)
            state['timed_out'] = bool(timed_out and not reached)
            job.pending.ok(state)

    def tick(self) -> None:
        """检查全部等待条件；每 WAIT_POLL_EVERY_TICKS 次轮询一次等待中的进程。"""
        for job in list(self._wait_jobs):
            try:
                self._check_wait(job)
            except Exception:
                logger.error("检查等待条件失败", exc_info=True)
                self._drop_wait_job(job)
                job.pending.error(ERR_INTERNAL, "检查等待条件失败，详见日志")
        self._ticks += 1
        if self.wait_service is not None and self.wait_service.has_waits() and (
                self._ticks % WAIT_POLL_EVERY_TICKS == 1):
            self.poll_start_waits()
        self._sync_timer()

    def poll_start_waits(self) -> None:
        """轮询一次等待启动的进程（出现即建任务并回复；超时经 failed 信号回复）。"""
        self.wait_service.poll()

    def _on_timer(self) -> None:
        try:
            self.tick()
        except Exception:
            logger.error("IPC 定时检查失败", exc_info=True)

    # ---------- 命令：导出 ----------

    def _cmd_export(self, conn, request):
        args = request.args
        task_id = _task_id(args)
        directory = _string(args, 'dir', required=True)
        if not is_absolute_path(directory):
            raise CommandError(ERR_INVALID_ARGS, MSG_PATH_NOT_ABSOLUTE.format(name='dir'))
        kinds = args.get('kinds', [EXPORT_KIND_CSV, EXPORT_KIND_REPORT, EXPORT_KIND_SUMMARY])
        if (not isinstance(kinds, list) or not kinds
                or not all(isinstance(k, str) and k in EXPORT_KINDS for k in kinds)):
            raise CommandError(ERR_INVALID_ARGS, MSG_EXPORT_KINDS)
        kinds = [k for k in EXPORT_KINDS if k in kinds]
        if _boolean(args, 'evidence'):
            raise CommandError(ERR_INVALID_ARGS, MSG_EVIDENCE_UNSUPPORTED)
        if EXPORT_KIND_REPORT in kinds and self.context.report_renderer is None:
            raise CommandError(ERR_INVALID_STATE, MSG_EXPORT_NO_RENDERER)
        if any(not run.finished for run in self._exports):
            raise CommandError(ERR_BUSY, MSG_EXPORT_BUSY)
        pending = self._new_pending(conn, request, CMD_EXPORT)
        # start_export 失败（含意外异常）时由 handle() 取消这条延迟回复
        run = self.start_export(task_id, directory, kinds, on_done=pending.ok,
                                on_failed=lambda code, message: pending.error(code, message))
        pending.on_cancel.append(run.cancel)
        return None

    def start_export(self, task_id: str, directory: str, kinds: Sequence[str], *,
                     on_done: Callable[[Dict[str, Any]], None],
                     on_failed: Callable[[str, str], None]) -> ExportRun:
        """开始一次导出（命令行 export 与无窗口模式的自动导出共用）；前置检查失败抛 CommandError。"""
        manager = self.context.manager
        task = self.context.db.get_task(task_id)
        if task is None:
            raise CommandError(ERR_NOT_FOUND, MSG_TASK_NOT_FOUND.format(task_id=task_id))
        if task.start_time is None:
            raise CommandError(ERR_INVALID_STATE, MSG_EXPORT_NO_DATA.format(task_id=task_id))
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise CommandError(ERR_INVALID_ARGS, MSG_EXPORT_DIR_FAILED.format(detail=exc)) from None
        if manager.is_maintenance_running() or not manager.begin_export():
            raise CommandError(ERR_BUSY, MSG_EXPORT_MAINTENANCE)
        run = ExportRun(self, task, directory, kinds, on_done, on_failed)
        self._exports.append(run)
        try:
            run.start()
        except Exception as exc:
            logger.error("启动命令行导出失败", exc_info=True)
            run._fail(ERR_INTERNAL, str(exc) or type(exc).__name__)
        return run

    def _exports_done(self, run: ExportRun) -> None:
        if run in self._exports:
            self._exports.remove(run)

    def _on_auto_export(self, task_id: str, directory: str) -> None:
        """无窗口模式：方案带"停止后自动导出"的任务结束后导出明细 CSV（与监控页一致）。"""
        try:
            self.start_export(
                task_id, directory, [EXPORT_KIND_CSV],
                on_done=lambda result: logger.info("自动导出完成: %s", result.get('paths')),
                on_failed=lambda code, message: logger.warning(
                    "自动导出未完成: task_id=%s %s", task_id, message))
        except CommandError as exc:
            logger.warning("自动导出未完成: task_id=%s %s", task_id, exc.message)
        except Exception:
            logger.error("自动导出失败: task_id=%s", task_id, exc_info=True)

    # ---------- 命令：判定 ----------

    def _cmd_verdict(self, conn, request):
        task_id = _task_id(request.args)
        task = self.context.db.get_task(task_id)
        if task is None:
            info = self.context.manager.get_task_info(task_id)
            if info is None:
                raise CommandError(ERR_NOT_FOUND, MSG_TASK_NOT_FOUND.format(task_id=task_id))
            metrics = list(info.metric_types)
        else:
            metrics = list(task.metric_types)
        running = sum(1 for p in self._pending if p.kind == CMD_VERDICT)
        if running >= MAX_CONCURRENT_VERDICTS:
            raise CommandError(ERR_BUSY, MSG_VERDICT_BUSY.format(limit=MAX_CONCURRENT_VERDICTS))
        reason = self._stop_reasons.get(task_id)
        before_condition = self._exited_before_condition(task_id, task, reason)
        pending = self._new_pending(conn, request, CMD_VERDICT)
        factory = self.context.analysis_worker_factory or TaskAnalysisWorker
        worker = factory(self.context.db, task_id, metrics, analysis=False)

        def done(analysis) -> None:
            try:
                result = compute_verdict(analysis, reason,
                                         stopped_before_condition=before_condition)
                result['task_id'] = task_id
                pending.ok(result)
            except CommandError as exc:
                pending.error(exc.code, exc.message)
            except Exception as exc:
                logger.error("计算判定失败", exc_info=True)
                pending.error(ERR_INTERNAL, MSG_ANALYSIS_FAILED.format(detail=exc))

        worker.result.connect(done)
        worker.failed.connect(lambda message: pending.error(
            ERR_INTERNAL, MSG_ANALYSIS_FAILED.format(detail=message)))
        pending.on_cancel.append(worker.cancel)
        if self.context.registry is not None:
            self.context.registry.register(worker, name=ANALYSIS_THREAD_NAME,
                                           cancel=worker.cancel, timeout_ms=THREAD_TIMEOUT_MS)
        worker.start()
        return None

    def _exited_before_condition(self, task_id: str, task, reason) -> bool:
        """被测进程是否在方案的停止条件达成之前退出（停止原因不是退出类时为假）。
        方案取库里存的（任务结束后自动打点服务已解除跟踪），没有再取内存里的；时长按
        墙钟（结束时刻 - 开始时刻），轮数为 kind='user' 的打点数。"""
        if not verdict_rules.is_target_exit(reason):
            return False
        from core.automation.plan import plan_from_json_or_none
        plan = plan_from_json_or_none(getattr(task, 'plan_json', None)) if task else None
        if plan is None:
            plan = self._plan_of(task_id)
        elapsed = None
        start = getattr(task, 'start_time', None)
        end = (getattr(task, 'end_time', None) or getattr(task, 'last_sample_at', None)
               if task is not None else None)
        if start is not None and end is not None:
            elapsed = (end - start).total_seconds()
        try:
            rounds = self._rounds(task_id)
        except Exception:
            logger.error("统计轮数失败: task_id=%s", task_id, exc_info=True)
            rounds = 0
        return verdict_rules.exited_before_condition(reason, plan, elapsed_s=elapsed,
                                                     rounds=rounds)

    # ---------- 命令：退出 ----------

    def _cmd_quit(self, conn, request):
        conn.send_ok(request.id, {'quitting': True})
        conn.flush()
        quit_fn = self.context.quit
        if quit_fn is not None:
            # 回包之后再走退出路径：真退出的收尾会阻塞界面线程（最多 30 秒）
            QTimer.singleShot(0, lambda: self._run_quit(quit_fn))
        return None

    @staticmethod
    def _run_quit(quit_fn: Callable[[], None]) -> None:
        try:
            quit_fn()
        except Exception:
            logger.error("执行退出失败", exc_info=True)

    # ---------- manager 信号 ----------

    def _on_task_stopped(self, task_id: str, reason) -> None:
        try:
            if isinstance(reason, StopReason):
                self._stop_reasons[task_id] = reason
            self._close_launched(task_id, reason)
            if task_id in self._unsaved_plans:
                self._save_plan(task_id)
                self._unsaved_plans.pop(task_id, None)
            for job in list(self._wait_jobs):
                if job.task_id == task_id:
                    self._check_wait(job)
        except Exception:
            logger.error("处理任务停止失败", exc_info=True)

    def _on_data_updated(self, task_id: str, _values=None) -> None:
        if task_id in self._unsaved_plans:
            try:
                self._save_plan(task_id)
            except Exception:
                logger.error("补存自动化方案失败", exc_info=True)
