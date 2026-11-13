"""
pmctl：进程监控助手的命令行控制端（只用标准库）

经本地命名管道（与主程序的单实例管道同名）驱动正在运行的进程监控助手；GUI 不在时以
--background 拉起一个无窗口实例（--no-launch 可禁止）。打包后是与主程序同目录的
pmctl.exe（console 程序），开发时用 python pmctl.py。

退出码：
  0 / 10 / 20 / 30  verdict 与 run 的判定：平稳 / 持续上涨 / 数据不足 / 被测进程异常退出
  2                 参数错误（含服务端回 bad_request / unknown_command / invalid_args）
  3                 连接失败（找不到程序、拉起超时、连接断开、心跳超时、身份校验失败、
                    拒绝访问、设置里关闭了命令行控制）
  1                 其他错误（任务不存在、状态不允许、权限不足、服务端超时或内部错误）

本文件不得导入 PyQt5 / psutil / ui 包：pmctl.exe 只打包标准库与 core.ipc 的客户端。
"""
import argparse
import json
import os
import socket
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from core.ipc import client as ipc
from core.ipc.client import (
    DEFAULT_IDLE_EXIT_S, EXIT_CONNECTION, EXIT_ERROR, EXIT_OK, EXIT_USAGE, ClientError,
    ServerError, exit_code_for_verdict, most_severe,
)

PROG = "pmctl"
DEFAULT_TIMEOUT_S = 30.0
# run：停止条件之外再多等多久（采集收尾、落库）
RUN_STOP_MARGIN_S = 60.0
# run：等待超时后发 stop，再等任务真正结束的上限（秒）
RUN_STOP_GRACE_S = 60.0
# run 出错或 Ctrl+C 时停止已开始的任务，每条 stop 的等待上限（秒）
CLEANUP_STOP_TIMEOUT_S = 5.0
JUNIT_SUITE_NAME = "进程监控助手"
JUNIT_TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%S'
EXPORT_KINDS_DEFAULT = "csv,report,summary"

# ---------- 文案 ----------
DESCRIPTION = "进程监控助手命令行：开始/打点/停止监控、等待、导出报告与泄漏判定。"
EPILOG = ("退出码：0 平稳；10 持续上涨；20 数据不足；30 被测进程异常退出；"
          "2 参数错误；3 连接失败；1 其他错误。")
HELP = {
    'data_dir': "数据目录（决定连接哪个实例；与主程序的 --data-dir 相同）",
    'timeout': f"普通命令的等待上限（秒，默认 {DEFAULT_TIMEOUT_S:g}）",
    'json': "原样输出服务端的 JSON 响应",
    'no_launch': "程序没在运行时不自动以无窗口模式拉起",
    'pid': "被测进程 PID",
    'name': "被测进程名（如 notepad.exe；唯一匹配时使用）",
    'tree': "进程树：监控 --pid（或 --launch 启动的程序）及其全部子孙进程，根进程退出任务才结束",
    'group_name': ("同名进程组：监控全部名为该名字的进程（如 chrome.exe，不要求唯一；当前没有也能开始，"
                   "成员出现后自动纳入，归零不结束任务）"),
    'metrics': "指标键，逗号分隔（缺省沿用程序里上次选择的指标）",
    'interval': "采集间隔（秒）",
    'note': "任务备注",
    'plan': "自动化方案 JSON 文件（定时打点、日志触发、停止条件等）",
    'wait_start': "进程还没启动时等它出现再开始",
    'wait_timeout': "等待进程启动的上限（秒）",
    'launch': "先启动这个程序（完整路径）再监控它",
    'launch_args': "启动参数（整行写在一个参数里，如 --args \"-x 1 --mode test\"；以 - 开头时写成 --args=-x）",
    'launch_cwd': "工作目录（缺省为程序所在目录）",
    'close_on_stop': "任务因停止命令或停止条件结束后关闭被测程序（先请求关闭窗口，5 秒不退再结束进程）",
    'task': "任务 ID",
    'task_or_all': "任务 ID，或 all（全部运行中的任务，默认）",
    'label': "打点名称（缺省按序列：基线 / 第 N 轮）",
    'until': "等待条件：stopped、rounds>=N 或 elapsed>=S",
    'wait_timeout_s': "等待上限（秒），到点仍未满足时 reached=false",
    'dir': "导出目录",
    'kinds': f"导出内容，逗号分隔：csv、report、summary（默认 {EXPORT_KINDS_DEFAULT}）",
    'rounds_every': "每隔多少秒自动打一轮点",
    'stop_after': "运行多少秒后自动停止",
    'rounds': "打满多少轮后自动停止",
    'out': "导出目录（不给则不导出）",
    'evidence_flag': "导出证据包 zip（报告图片、明细与摘要 CSV、task.json、README、日志摘录），此时 --kinds 无效",
    'evidence_dir': "每个任务结束后另导出一个证据包 zip 到这个目录（不给则不导出证据包）",
    'junit': "把判定写成 JUnit XML（每个指标一个用例）",
    'idle_exit': (f"自动拉起的无窗口实例空闲多少秒后自动退出（默认 {DEFAULT_IDLE_EXIT_S:g}；"
                  "0 为不退出）"),
}
CMD_HELP = {
    'ping': "检查程序是否在运行", 'status': "列出任务", 'start': "开始监控",
    'marker': "打点", 'pause': "暂停任务", 'resume': "恢复任务", 'stop': "停止任务",
    'wait': "等待任务满足条件", 'export': "导出明细 CSV / 报告图片 / 摘要",
    'verdict': "泄漏判定（退出码 0/10/20/30）", 'quit': "退出程序",
    'run': "一条命令跑完：开始 → 自动打点 → 停止 → 导出 → 判定",
}
MSG_ERROR = "错误：{message}"
MSG_PLAN_READ = "无法读取方案文件 {path}：{detail}"
MSG_RUN_NEEDS_TARGET = "run 需要至少一个 --pid、--name、--group-name 或 --launch"
MSG_TREE_NEEDS_ROOT = "--tree 需要与 --pid 或 --launch 一起使用（它们是进程树的根）"
MSG_TREE_NO_NAME = "--tree 不能与 --name / --group-name 一起使用"
GROUP_TEXTS = {'tree': "进程树", 'name': "同名进程组"}
MSG_RUN_NEEDS_STOP = ("run 需要停止条件：--stop-after、--rounds，或方案文件里的 stop_after_seconds / "
                      "stop_after_rounds（否则会一直等下去）")
MSG_RUN_TIMEOUT = "{label}：等待超时（{seconds:g} 秒），已停止任务"
MSG_RUN_TIMEOUT_TAG = "超时停止"
MSG_RUN_CLEANUP = "已停止本次开始的任务：{tasks}"
MSG_TARGET_EXIT_AFTER = "  被测进程在停止条件达成后退出（{reason}），不影响判定"
MSG_EXIT_CODE = "  被测进程退出码：{text}"
MSG_CONCLUSION = "  {text}"
MSG_SEGMENT_CASE = "第 {number} 段"
MSG_WAITING_RESTART = "等待被测进程重启（已续接 {count}/{limit} 次）"
MSG_WAITING = "{message}"
MSG_LAUNCH_OPTION_ONLY = "--args、--cwd、--close-on-stop 只能与 --launch 一起使用"
MSG_LAUNCH_NO_WAIT = "--launch 由本次启动程序，不能与 --wait 一起使用"
MSG_VERDICT_TEXT = {
    'stable': "平稳", 'rising': "持续上涨", 'insufficient': "数据不足",
    'target_exited': "被测进程已退出",
}


class UsageError(ClientError):
    exit_code = EXIT_USAGE


# ---------- 参数 ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION, epilog=EPILOG)
    parser.add_argument('--data-dir', help=HELP['data_dir'])
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT_S, help=HELP['timeout'])
    parser.add_argument('--json', action='store_true', help=HELP['json'])
    parser.add_argument('--no-launch', action='store_true', help=HELP['no_launch'])
    parser.add_argument('--idle-exit', type=float, default=DEFAULT_IDLE_EXIT_S, metavar='秒',
                        help=HELP['idle_exit'])
    sub = parser.add_subparsers(dest='command', metavar='命令')
    sub.required = True

    sub.add_parser('ping', help=CMD_HELP['ping'])
    sub.add_parser('status', help=CMD_HELP['status'])

    start = sub.add_parser('start', help=CMD_HELP['start'])
    target = start.add_mutually_exclusive_group()
    target.add_argument('--pid', type=int, help=HELP['pid'])
    target.add_argument('--name', help=HELP['name'])
    target.add_argument('--group-name', metavar='进程名', help=HELP['group_name'])
    target.add_argument('--launch', metavar='EXE', help=HELP['launch'])
    start.add_argument('--tree', action='store_true', help=HELP['tree'])
    _add_task_options(start)
    _add_launch_options(start)
    start.add_argument('--wait', action='store_true', help=HELP['wait_start'])
    start.add_argument('--wait-timeout', type=float, help=HELP['wait_timeout'])

    marker = sub.add_parser('marker', help=CMD_HELP['marker'])
    marker.add_argument('--task', default='all', help=HELP['task_or_all'])
    marker.add_argument('--label', help=HELP['label'])
    marker.add_argument('--group', help='仅给该测试方案组内的运行中任务共享打点')

    for name in ('pause', 'resume', 'stop'):
        p = sub.add_parser(name, help=CMD_HELP[name])
        p.add_argument('--task', required=True, help=HELP['task'])

    wait = sub.add_parser('wait', help=CMD_HELP['wait'])
    wait.add_argument('--task', required=True, help=HELP['task'])
    wait.add_argument('--until', default='stopped', help=HELP['until'])
    wait.add_argument('--timeout-s', type=float, help=HELP['wait_timeout_s'])

    export = sub.add_parser('export', help=CMD_HELP['export'])
    export.add_argument('--task', required=True, help=HELP['task'])
    export.add_argument('--dir', required=True, help=HELP['dir'])
    export.add_argument('--kinds', default=EXPORT_KINDS_DEFAULT, help=HELP['kinds'])
    export.add_argument('--evidence', action='store_true', help=HELP['evidence_flag'])

    verdict = sub.add_parser('verdict', help=CMD_HELP['verdict'])
    verdict.add_argument('--task', required=True, help=HELP['task'])
    verdict.add_argument('--vs-reference', action='store_true', help='与同场景参考任务比较每轮增量')

    sub.add_parser('quit', help=CMD_HELP['quit'])

    run = sub.add_parser('run', help=CMD_HELP['run'])
    run.add_argument('--pid', type=int, action='append', default=[], help=HELP['pid'])
    run.add_argument('--name', action='append', default=[], help=HELP['name'])
    run.add_argument('--group-name', action='append', default=[], metavar='进程名',
                     help=HELP['group_name'])
    run.add_argument('--launch', metavar='EXE', help=HELP['launch'])
    run.add_argument('--tree', action='store_true', help=HELP['tree'])
    _add_task_options(run)
    _add_launch_options(run)
    run.add_argument('--wait', action='store_true', help=HELP['wait_start'])
    run.add_argument('--wait-timeout', type=float, help=HELP['wait_timeout'])
    run.add_argument('--rounds-every', type=int, help=HELP['rounds_every'])
    stop = run.add_mutually_exclusive_group()
    stop.add_argument('--stop-after', type=int, help=HELP['stop_after'])
    stop.add_argument('--rounds', type=int, help=HELP['rounds'])
    run.add_argument('--out', help=HELP['out'])
    run.add_argument('--kinds', default=EXPORT_KINDS_DEFAULT, help=HELP['kinds'])
    run.add_argument('--evidence', metavar='DIR', help=HELP['evidence_dir'])
    run.add_argument('--junit', help=HELP['junit'])
    return parser


def _add_task_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--metrics', help=HELP['metrics'])
    parser.add_argument('--interval', type=float, help=HELP['interval'])
    parser.add_argument('--note', help=HELP['note'])
    parser.add_argument('--plan', help=HELP['plan'])
    parser.add_argument('--scenario', help='测试场景（用于参考基线与任务筛选）')
    parser.add_argument('--build', help='被测版本（不填时取被测文件版本）')


def _add_launch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--args', dest='launch_args', metavar='参数', help=HELP['launch_args'])
    parser.add_argument('--cwd', dest='launch_cwd', metavar='DIR', help=HELP['launch_cwd'])
    parser.add_argument('--close-on-stop', action='store_true', help=HELP['close_on_stop'])


def launch_payload(args) -> Optional[Dict[str, Any]]:
    """--launch / --args / --cwd / --close-on-stop → start 的 launch 参数（路径转成绝对路径）；
    没给 --launch 时为 None，此时不允许出现其余三项。"""
    exe = getattr(args, 'launch', None)
    extras = (getattr(args, 'launch_args', None) is not None
              or getattr(args, 'launch_cwd', None) is not None
              or getattr(args, 'close_on_stop', False))
    if exe is None:
        if extras:
            raise UsageError(MSG_LAUNCH_OPTION_ONLY)
        return None
    from core.process_launcher import split_args
    payload: Dict[str, Any] = {'exe': os.path.abspath(exe),
                               'args': split_args(getattr(args, 'launch_args', None))}
    if getattr(args, 'launch_cwd', None):
        payload['cwd'] = os.path.abspath(args.launch_cwd)
    if getattr(args, 'close_on_stop', False):
        payload['close_on_stop'] = True
    return payload


def check_tree_options(args, *, has_root: bool, has_name: bool) -> None:
    """--tree 的组合校验：必须有根（--pid / --launch），不能和按名的目标混用。"""
    if not getattr(args, 'tree', False):
        return
    if has_name:
        raise UsageError(MSG_TREE_NO_NAME)
    if not has_root:
        raise UsageError(MSG_TREE_NEEDS_ROOT)


def _split(text: Optional[str]) -> Optional[List[str]]:
    if text is None:
        return None
    return [item.strip() for item in text.split(',') if item.strip()]


def _read_plan(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    try:
        # 同时限制旧自动化 JSON 与完整测试方案，避免异常大文件耗尽客户端内存。
        max_bytes = 1024 * 1024
        with open(path, 'rb') as f:
            raw = f.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError('方案文件不能超过 1 MB')
        data = json.loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise UsageError(MSG_PLAN_READ.format(path=path, detail=exc)) from None
    if not isinstance(data, dict):
        raise UsageError(MSG_PLAN_READ.format(path=path, detail="不是 JSON 对象"))
    return data


def _start_args(args, *, pid: Optional[int] = None, name: Optional[str] = None,
                launch: Optional[Dict[str, Any]] = None,
                plan: Optional[Dict[str, Any]] = None,
                target_kind: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if target_kind is not None:
        payload['target_kind'] = target_kind
    if pid is not None:
        payload['pid'] = pid
    if name is not None:
        payload['name'] = name
    if launch is not None:
        payload['launch'] = launch
    metrics = _split(args.metrics)
    if metrics is not None:
        payload['metrics'] = metrics
    if args.interval is not None:
        payload['interval'] = args.interval
    if args.note is not None:
        payload['note'] = args.note
    for key in ('scenario', 'build'):
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    if plan:
        payload['plan'] = plan
    if getattr(args, 'wait', False) and launch is None:
        payload['wait'] = True
        if args.wait_timeout is not None:
            payload['wait_timeout_s'] = args.wait_timeout
    return payload


# ---------- 连接 ----------

def resolve_instance_name(data_dir: Optional[str]) -> str:
    """与主程序相同的规则解析数据目录（命令行 → 环境变量 → 便携 → 默认），再算实例名。"""
    from app.paths import AppPaths
    from utils.instance_name import single_instance_name
    argv = [sys.argv[0] if sys.argv else PROG]
    if data_dir:
        argv += ['--data-dir', data_dir]
    return single_instance_name(AppPaths.resolve(argv=argv).data_dir)


def default_session_factory(args, err=None):
    name = resolve_instance_name(args.data_dir)
    launch = None if args.no_launch else ipc.launch_command(
        os.path.abspath(args.data_dir) if args.data_dir else None,
        idle_exit_s=getattr(args, 'idle_exit', DEFAULT_IDLE_EXIT_S))
    stream = err or sys.stderr

    def on_waiting(message: str) -> None:
        print(MSG_WAITING.format(message=message), file=stream, flush=True)

    return ipc.open_session(name, launch=launch, on_waiting=on_waiting)


def run_wait_limit(plan: Dict[str, Any]) -> Optional[float]:
    """run 等待任务停止的上限（秒）：时长 + RUN_STOP_MARGIN_S，或 轮数 × 定时打点间隔 +
    RUN_STOP_MARGIN_S，二者都有取小；只有轮数而没有定时打点（靠日志或手动打点凑轮数）时
    无法估计，返回 None（不设上限）。"""
    limits = []
    seconds = plan.get('stop_after_seconds')
    if seconds:
        limits.append(float(seconds) + RUN_STOP_MARGIN_S)
    rounds, every = plan.get('stop_after_rounds'), plan.get('timed_marker_seconds')
    if rounds and every:
        limits.append(float(rounds) * float(every) + RUN_STOP_MARGIN_S)
    return min(limits) if limits else None


def has_stop_condition(plan: Dict[str, Any]) -> bool:
    return plan.get('stop_after_seconds') is not None or plan.get('stop_after_rounds') is not None


# ---------- JUnit ----------

def build_junit(results: Sequence[Dict[str, Any]], elapsed_s: float = 0.0, *,
                timestamp: Optional[datetime] = None,
                hostname: Optional[str] = None) -> ET.ElementTree:
    """判定结果 → JUnit XML（Surefire / Ant 结构）：根为 testsuite（name、tests、failures、
    errors、skipped、time、timestamp、hostname），子元素依次为 properties、testcase…、
    system-out、system-err；每个任务的每个指标一个 testcase（classname、name、time）；
    持续上涨 → failure，数据不足或无判定 → skipped，被测进程异常退出 → error。
    等待超时后由 pmctl 停止的任务，其用例的 system-out 与套件属性里写明"超时停止"。"""
    when = timestamp or datetime.now()
    suite = ET.Element('testsuite', name=JUNIT_SUITE_NAME)
    properties = ET.SubElement(suite, 'properties')
    ET.SubElement(properties, 'property', name='tool', value=JUNIT_SUITE_NAME)
    timed_out = [str(item.get('target') or item.get('task_id') or '') for item in results
                 if item.get('timeout_stopped')]
    if timed_out:
        ET.SubElement(properties, 'property', name='stopped_by_timeout',
                      value='; '.join(timed_out))
    counts = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    for item in results:
        target = item.get('target') or item.get('task_id') or ''
        overall = item.get('verdict')
        metrics = item.get('metrics') or [{'key': 'verdict', 'name': '判定', 'verdict': overall}]
        # 进程组任务：每个成员的每个指标也是一个用例（成员持续上涨同样算失败）
        metrics = list(metrics) + [metric for group in item.get('members') or []
                                   for metric in group.get('metrics') or []]
        # 重启续接过的任务：持续上涨可能只出在更早的启动段（最后一段的各指标都不涨），
        # 每个上涨的段单独记一个用例，CI 才不会在退出码为 20 时看到全部通过
        for segment in item.get('segments') or []:
            if segment.get('verdict') == ipc.VERDICT_RISING:
                metrics.append({
                    'key': f"segment_{segment.get('number')}",
                    'name': MSG_SEGMENT_CASE.format(number=segment.get('number')),
                    'verdict': ipc.VERDICT_RISING,
                    'reason': 'rising',
                    'rising': "、".join(segment.get('rising_names') or [])})
        message_target_exited = item.get('conclusion') or MSG_VERDICT_TEXT['target_exited']
        for metric in metrics:
            counts['tests'] += 1
            case = ET.SubElement(suite, 'testcase', classname=str(target),
                                 name=str(metric.get('name') or metric.get('key')),
                                 time='0.000')
            detail = _metric_detail(metric)
            if item.get('timeout_stopped'):
                detail = f"{MSG_RUN_TIMEOUT_TAG}; {detail}" if detail else MSG_RUN_TIMEOUT_TAG
            verdict = metric.get('verdict')
            if overall == ipc.VERDICT_TARGET_EXITED:
                counts['errors'] += 1
                node = ET.SubElement(case, 'error', type='target_exited',
                                     message=message_target_exited)
                node.text = detail
            elif verdict == ipc.VERDICT_RISING:
                counts['failures'] += 1
                node = ET.SubElement(case, 'failure', type='rising',
                                     message=MSG_VERDICT_TEXT['rising'])
                node.text = detail
            elif verdict not in ('stable', 'fluctuating'):
                counts['skipped'] += 1
                ET.SubElement(case, 'skipped', message=MSG_VERDICT_TEXT.get(
                    verdict, "无趋势判定"))
            props = ET.SubElement(case, 'system-out')
            props.text = detail
    ET.SubElement(suite, 'system-out').text = (
        f"{MSG_RUN_TIMEOUT_TAG}：{'; '.join(timed_out)}" if timed_out else '')
    ET.SubElement(suite, 'system-err').text = ''
    for key, value in counts.items():
        suite.set(key, str(value))
    suite.set('time', f"{max(elapsed_s, 0.0):.3f}")
    suite.set('timestamp', when.strftime(JUNIT_TIMESTAMP_FORMAT))
    suite.set('hostname', hostname or socket.gethostname() or 'localhost')
    return ET.ElementTree(suite)


def _metric_detail(metric: Dict[str, Any]) -> str:
    parts = [f"{key}={metric.get(key)}" for key in
             ('key', 'verdict', 'reason', 'samples', 'first', 'last', 'baseline',
              'rate_per_hour', 'rate_per_round', 'rounds', 'rising') if key in metric]
    return "; ".join(parts)


def write_junit(path: str, results: Sequence[Dict[str, Any]], elapsed_s: float = 0.0) -> None:
    tree = build_junit(results, elapsed_s)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tree.write(path, encoding='utf-8', xml_declaration=True)


# ---------- 执行 ----------

class Cli:
    def __init__(self, args, session_factory: Callable, out=None, err=None):
        self.args = args
        self._factory = session_factory
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.client = None

    def emit(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def session(self):
        if self.client is None:
            self.client = self._factory(self.args)
        return self.client

    def _reset_session(self) -> None:
        """丢弃当前连接（中途被打断的请求可能还有回复在路上），下次 session() 重连。"""
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def call(self, cmd: str, payload: Optional[Dict[str, Any]] = None, *,
             timeout: Optional[float] = -1.0) -> Dict[str, Any]:
        if timeout == -1.0:
            timeout = self.args.timeout
        response = self.session().request(cmd, payload or {}, timeout=timeout)
        if self.args.json:
            # 机器可读流保持 ASCII 子集，冻结程序在 GBK 重定向下也可按 UTF-8 解析。
            self.emit(json.dumps(response.raw, ensure_ascii=True))
        if not response.ok:
            raise ServerError(response)
        return response.result or {}

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass

    # ---- 命令 ----

    def run_command(self) -> int:
        command = self.args.command.replace('-', '_')
        return getattr(self, f"cmd_{command}")()

    def cmd_ping(self) -> int:
        result = self.call('ping')
        if not self.args.json:
            mode = "无窗口模式" if result.get('headless') else "有窗口"
            self.emit(f"进程监控助手 {result.get('app_version')}（PID {result.get('pid')}，{mode}）")
        return EXIT_OK

    def cmd_status(self) -> int:
        result = self.call('status')
        if not self.args.json:
            tasks = result.get('tasks') or []
            if not tasks:
                self.emit("没有任务")
            for task in tasks:
                state = "已暂停" if task.get('paused') else task.get('status')
                if task.get('waiting_restart'):
                    state = MSG_WAITING_RESTART.format(count=task.get('restart_count') or 0,
                                                       limit=task.get('restart_limit') or 0)
                group = GROUP_TEXTS.get(task.get('target_kind'))
                target = (f"{task.get('name')}（{group}）" if task.get('target_kind') == 'name'
                          else f"{task.get('name')}（{group + '，' if group else ''}"
                               f"PID {task.get('pid')}）")
                line = f"{task.get('task_id')}  {target}  {state}  第 {task.get('rounds')} 轮"
                quality = (task.get('quality') or {}).get('text')
                if quality:
                    line += f"  {quality}"
                self.emit(line)
        return EXIT_OK

    def cmd_start(self) -> int:
        raw_plan = _read_plan(self.args.plan)
        if raw_plan is not None and 'targets' in raw_plan:
            return self._start_template(raw_plan)
        if not any((self.args.pid is not None, self.args.name, self.args.group_name,
                    self.args.launch)):
            raise UsageError('start 需要指定目标，或使用包含目标的 --plan 测试方案')
        launch = launch_payload(self.args)
        if launch is not None and self.args.wait:
            raise UsageError(MSG_LAUNCH_NO_WAIT)
        group_name = self.args.group_name
        check_tree_options(self.args, has_root=self.args.pid is not None or launch is not None,
                           has_name=self.args.name is not None or group_name is not None)
        if group_name is not None:
            target_kind, name = 'name', group_name
        else:
            target_kind, name = ('tree' if self.args.tree else None), self.args.name
        payload = _start_args(self.args, pid=self.args.pid, name=name, launch=launch,
                              plan=raw_plan, target_kind=target_kind)
        result = self.call('start', payload, timeout=None if self.args.wait else self.args.timeout)
        if not self.args.json:
            self.emit(result.get('task_id', ''))
        return EXIT_OK

    def _start_template(self, data) -> int:
        from core.plans import TestPlan
        if any((self.args.pid is not None, self.args.name, self.args.group_name,
                self.args.launch, self.args.tree, self.args.metrics, self.args.interval is not None,
                self.args.note is not None, self.args.wait, self.args.wait_timeout is not None,
                self.args.scenario is not None, self.args.build is not None,
                self.args.launch_args is not None, self.args.launch_cwd is not None,
                self.args.close_on_stop)):
            raise UsageError('完整测试方案已包含目标和配置，请修改方案文件后运行')
        try:
            plan = TestPlan.from_dict(data)
        except ValueError as exc:
            raise UsageError(str(exc)) from None
        result = self.call('start_plan', {'plan': plan.to_dict()},
                           timeout=None if any(t.wait for t in plan.targets) else self.args.timeout)
        for item in result.get('results') or []:
            index = item.get('index')
            if item.get('ok'):
                if not self.args.json:
                    self.emit(f"{index}. {(item.get('result') or {}).get('task_id', '')}")
            else:
                message = (item.get('error') or {}).get('message', '未知错误')
                print(f'目标 {index} 未开始：{message}', file=self.err, flush=True)
        group_id = result.get('group_id')
        if group_id and not self.args.json:
            self.emit(f'共享打点组：{group_id}（pmctl marker --group {group_id}）')
        return EXIT_ERROR if result.get('failed') else EXIT_OK

    def cmd_marker(self) -> int:
        payload = {'task_id': self.args.task}
        if self.args.group:
            if self.args.task != 'all':
                raise UsageError('--group 与 --task 不能同时使用')
            payload['group_id'] = self.args.group
        if self.args.label:
            payload['label'] = self.args.label
        result = self.call('marker', payload)
        if not self.args.json:
            for marker in result.get('markers') or []:
                self.emit(f"{marker.get('task_id')}  {marker.get('label')}  {marker.get('timestamp')}")
        return EXIT_OK

    def _simple(self, cmd: str) -> int:
        self.call(cmd, {'task_id': self.args.task})
        return EXIT_OK

    def cmd_pause(self) -> int:
        return self._simple('pause')

    def cmd_resume(self) -> int:
        return self._simple('resume')

    def cmd_stop(self) -> int:
        return self._simple('stop')

    def cmd_wait(self) -> int:
        payload = {'task_id': self.args.task, 'until': self.args.until}
        if self.args.timeout_s is not None:
            payload['timeout_s'] = self.args.timeout_s
        result = self.call('wait', payload, timeout=None)
        if not self.args.json:
            self.emit("已满足" if result.get('reached') else "未满足")
        return EXIT_OK if result.get('reached') else EXIT_ERROR

    def cmd_export(self) -> int:
        payload = {'task_id': self.args.task, 'dir': os.path.abspath(self.args.dir),
                   'kinds': _split(self.args.kinds) or []}
        if self.args.evidence:
            payload['evidence'] = True
        result = self.call('export', payload, timeout=None)
        if not self.args.json:
            for path in result.get('paths') or []:
                self.emit(path)
        return EXIT_OK

    def cmd_verdict(self) -> int:
        payload = {'task_id': self.args.task}
        if self.args.vs_reference:
            payload['vs_reference'] = True
        result = self.call('verdict', payload, timeout=None)
        verdict = result.get('verdict')
        if not self.args.json:
            self._print_verdict(self.args.task, result)
        if self.args.vs_reference:
            comparison = result.get('reference', {})
            status = comparison.get('status', 'incomparable')
            if not self.args.json:
                self.emit({'regression': '较参考发生回归', 'comparable': '未发现较参考回归',
                           'incomparable': '与参考不可比'}.get(status, status))
                if comparison.get('reason'):
                    self.emit(comparison['reason'])
            if verdict == 'target_exited':
                return 30
            return {'regression': 10, 'comparable': 0, 'incomparable': 20}.get(status, 20)
        return exit_code_for_verdict(verdict)

    def cmd_quit(self) -> int:
        self.call('quit')
        return EXIT_OK

    def _print_verdict(self, target: str, result: Dict[str, Any]) -> None:
        verdict = result.get('verdict')
        text = MSG_VERDICT_TEXT.get(verdict, verdict)
        if result.get('timeout_stopped'):
            text = f"{text}（{MSG_RUN_TIMEOUT_TAG}）"
        self.emit(f"{target}：{text}")
        if result.get('conclusion'):
            # 重启续接 / 中途异常退出：次数、最近一次退出码与各段判定
            self.emit(MSG_CONCLUSION.format(text=result['conclusion']))
        exit_info = result.get('target_exit') or {}
        if exit_info and not exit_info.get('before_stop_condition', True):
            self.emit(MSG_TARGET_EXIT_AFTER.format(reason=exit_info.get('reason')))
        if exit_info.get('exit_text'):
            self.emit(MSG_EXIT_CODE.format(text=exit_info['exit_text']))
        segment = result.get('segment') or {}
        if segment.get('note'):
            self.emit(f"  {segment['note']}")
        for metric in result.get('metrics') or []:
            self.emit(self._metric_line(metric))
        for group in result.get('members') or []:
            self.emit(f"  成员 {group.get('key')}：{MSG_VERDICT_TEXT.get(group.get('verdict'), group.get('verdict'))}")
            for metric in group.get('metrics') or []:
                self.emit("  " + self._metric_line(metric))

    @staticmethod
    def _metric_line(metric: Dict[str, Any]) -> str:
        line = (f"  {metric.get('name')}：{metric.get('verdict')}"
                f"（每小时 {metric.get('rate_per_hour')}，每轮 {metric.get('rate_per_round')}）")
        if metric.get('baseline_note'):
            # 基线在别的启动段：不写"较基线"，只写它不可比
            line += f"，{metric['baseline_note']}"
        return line

    def cmd_run(self) -> int:
        """开始 → 等停止条件 → （超时则停止）→ 导出 → 判定。

        - 必须有停止条件（--stop-after / --rounds / 方案里的），否则退出码 2；
        - 等待上限见 run_wait_limit；到点仍未停止 → 发 stop、等它结束，再照常导出与判定，
          退出码按判定，输出与 JUnit 写明"超时停止"；
        - 中途出错（如第二个目标 start 失败）或 Ctrl+C：停掉本次已开始、尚未结束的任务再退出。
        """
        args = self.args
        targets = ([('pid', pid) for pid in args.pid] + [('name', name) for name in args.name]
                   + [('group_name', name) for name in args.group_name])
        launch = launch_payload(args)
        if launch is not None:
            targets.append(('launch', launch))
        if not targets:
            raise UsageError(MSG_RUN_NEEDS_TARGET)
        check_tree_options(args, has_root=bool(args.pid) or launch is not None,
                           has_name=bool(args.name) or bool(args.group_name))
        plan = dict(_read_plan(args.plan) or {})
        if args.rounds_every is not None:
            plan['timed_marker_seconds'] = args.rounds_every
        if args.stop_after is not None:
            plan['stop_after_seconds'] = args.stop_after
        if args.rounds is not None:
            plan['stop_after_rounds'] = args.rounds
        if not has_stop_condition(plan):
            raise UsageError(MSG_RUN_NEEDS_STOP)
        began = time.monotonic()
        started: List[Any] = []
        finished = set()
        try:
            for kind, value in targets:
                if kind == 'group_name':
                    target = {'name': value, 'target_kind': 'name'}
                else:
                    target = {kind: value}
                    if args.tree:
                        target['target_kind'] = 'tree'
                payload = _start_args(args, plan=plan, **target)
                result = self.call('start', payload, timeout=None if args.wait else args.timeout)
                label = f"{result.get('name')}（PID {result.get('pid')}）"
                started.append((label, result['task_id']))
                if not args.json:
                    self.emit(f"已开始：{label} {result['task_id']}")
            wait_timeout = run_wait_limit(plan)
            results = []
            for label, task_id in started:
                timed_out = self._run_wait(label, task_id, wait_timeout)
                finished.add(task_id)
                if args.out:
                    exported = self.call('export', {'task_id': task_id,
                                                    'dir': os.path.abspath(args.out),
                                                    'kinds': _split(args.kinds) or []},
                                         timeout=None)
                    if not args.json:
                        for path in exported.get('paths') or []:
                            self.emit(f"已导出：{path}")
                if args.evidence:
                    packed = self.call('export', {'task_id': task_id,
                                                  'dir': os.path.abspath(args.evidence),
                                                  'evidence': True}, timeout=None)
                    if not args.json:
                        for path in packed.get('paths') or []:
                            self.emit(f"已导出证据包：{path}")
                verdict = self.call('verdict', {'task_id': task_id}, timeout=None)
                verdict['target'] = label
                if timed_out:
                    verdict['timeout_stopped'] = True
                results.append(verdict)
                if not args.json:
                    self._print_verdict(label, verdict)
        except BaseException as exc:
            leftover = [task_id for _, task_id in started if task_id not in finished]
            if leftover:
                self._stop_tasks(leftover, reconnect=not isinstance(exc, ServerError))
            raise
        if args.junit:
            write_junit(args.junit, results, time.monotonic() - began)
        worst = most_severe([r.get('verdict') for r in results])
        return exit_code_for_verdict(worst)

    def _run_wait(self, label: str, task_id: str, limit: Optional[float]) -> bool:
        """等任务停止；到上限仍未停止则发 stop 并再等它结束。返回是否为超时停止。"""
        payload: Dict[str, Any] = {'task_id': task_id, 'until': 'stopped'}
        if limit is not None:
            payload['timeout_s'] = limit
        result = self.call('wait', payload, timeout=None)
        if result.get('reached', True) or result.get('stopped'):
            return False
        if not self.args.json:
            self.emit(MSG_RUN_TIMEOUT.format(label=label, seconds=limit or 0))
        try:
            self.call('stop', {'task_id': task_id})
        except ServerError as exc:
            # 恰好在此刻自己结束了（invalid_state）：照常等它收尾
            if exc.code != 'invalid_state':
                raise
        self.call('wait', {'task_id': task_id, 'until': 'stopped',
                           'timeout_s': RUN_STOP_GRACE_S}, timeout=None)
        return True

    def _stop_tasks(self, task_ids: Sequence[str], *, reconnect: bool) -> None:
        """尽力停止给定任务（run 出错或被中断时调用）；任何失败都忽略。"""
        if reconnect:
            self._reset_session()
        stopped = []
        for task_id in task_ids:
            try:
                response = self.session().request('stop', {'task_id': task_id},
                                                  timeout=CLEANUP_STOP_TIMEOUT_S)
            except (Exception, KeyboardInterrupt):
                break
            if response.ok:
                stopped.append(task_id)
        if stopped:
            print(MSG_RUN_CLEANUP.format(tasks="、".join(stopped)), file=self.err, flush=True)


def main(argv: Optional[Sequence[str]] = None, *, session_factory: Optional[Callable] = None,
         out=None, err=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    factory = session_factory or (lambda a: default_session_factory(a, err=err))
    cli = Cli(args, factory, out=out, err=err)
    try:
        return cli.run_command()
    except ServerError as exc:
        print(MSG_ERROR.format(message=f"[{exc.code}] {exc}"), file=cli.err, flush=True)
        return exc.exit_code
    except ClientError as exc:
        print(MSG_ERROR.format(message=exc), file=cli.err, flush=True)
        return exc.exit_code
    except KeyboardInterrupt:
        return EXIT_CONNECTION
    finally:
        cli.close()


if __name__ == '__main__':
    sys.exit(main())
