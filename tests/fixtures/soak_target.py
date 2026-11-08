"""
浸泡用被测负载程序（只用标准库，占用很小，可以连续跑几十个小时）

三种模式，供“真实 24 小时浸泡”与缩短演练里让本工具监控：
- leak：每个节拍泄漏内存与句柄，速率可配置（工具链正确性检查：它必须被判为“持续上涨”）；
- stable：私有字节按锯齿波动（涨到振幅后整体释放），长期不涨（必须被判为平稳）；
- restart：运行一段时间后退出，可选崩溃退出码，由 --supervise 的监督进程在退出后重新拉起。

被测进程自己重启后，本工具按“进程名 + 创建时间晚于旧进程退出、且父进程不是旧进程”认续接，
所以重启必须由外部（监督进程或脚本）拉起，不能是旧进程派生的子进程。

用法示例：
    python soak_target.py --mode leak --leak-mb-per-min 3 --leak-handles-per-min 60
    python soak_target.py --mode stable --amplitude-mb 16 --period-s 60
    python soak_target.py --mode restart --supervise --lifetime-s 120 --crash-every 3 \\
        --pid-file restart.pid

--pid-file 写入当前工作进程的 PID（监督模式下每次拉起新进程都会改写），脚本据此建任务。
--duration-s 给出时，工作进程运行这么久后正常退出（测试与演练用）；缺省 0 = 一直跑。
"""
import argparse
import os
import subprocess
import sys
import time
from typing import Callable, List, Optional, Sequence

MODE_LEAK = 'leak'
MODE_STABLE = 'stable'
MODE_RESTART = 'restart'
MODES = (MODE_LEAK, MODE_STABLE, MODE_RESTART)

MB = 1024 * 1024
DEFAULT_TICK_S = 1.0
DEFAULT_LEAK_MB_PER_MIN = 3.0
DEFAULT_LEAK_HANDLES_PER_MIN = 60.0
DEFAULT_LEAK_MAX_MB = 1024.0
DEFAULT_AMPLITUDE_MB = 16.0
DEFAULT_PERIOD_S = 60.0
DEFAULT_LIFETIME_S = 120.0
DEFAULT_RESTART_DELAY_S = 3.0
# 默认崩溃退出码：STATUS_ACCESS_VIOLATION（0xC0000005），被本工具的退出码取证识别为异常退出
DEFAULT_CRASH_CODE = 0xC0000005
# 监督进程等工作进程的轮询间隔
_SUPERVISE_POLL_S = 0.2


def to_exit_code(value: int) -> int:
    """退出码规整为 32 位有符号整数：os._exit 只收 int 范围内的值，0xC0000005 要换成负数形式，
    进程退出后 Windows 读到的仍是 0xC0000005。"""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def _parse_int(text: str) -> int:
    return int(text, 0)


# ---------- 负载 ----------

class Workload:
    """一个负载：step(dt) 每个节拍调用一次，dt 为距上次调用的秒数。"""

    def step(self, dt: float) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return type(self).__name__


def _touched(num_bytes: int) -> bytearray:
    """分配并写满，保证提交的内存真的算进私有字节与工作集（不依赖按需清零页）。"""
    return bytearray(b'\x01') * num_bytes


def _make_handle() -> object:
    """造一个永不关闭的内核句柄（Windows 事件对象；其他平台退回 /dev/null 文件对象）。"""
    if sys.platform == 'win32':
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_wchar_p]
        handle = kernel32.CreateEventW(None, 1, 0, None)
        if not handle:
            raise OSError("CreateEventW 失败")
        return handle
    return open(os.devnull, 'rb')


class LeakWorkload(Workload):
    """每个节拍按速率泄漏内存与句柄（都不释放）。内存累计到 max_mb 后不再增长，
    防止长时间运行吃光机器；max_mb <= 0 表示不限。"""

    def __init__(self, mb_per_min: float = DEFAULT_LEAK_MB_PER_MIN,
                 handles_per_min: float = DEFAULT_LEAK_HANDLES_PER_MIN,
                 max_mb: float = DEFAULT_LEAK_MAX_MB,
                 make_handle: Callable[[], object] = _make_handle):
        self.mb_per_min = max(0.0, mb_per_min)
        self.handles_per_min = max(0.0, handles_per_min)
        self.max_bytes = int(max_mb * MB) if max_mb > 0 else 0
        self._make_handle = make_handle
        self._chunks: List[bytearray] = []
        self._handles: List[object] = []
        self._owed_bytes = 0.0
        self._owed_handles = 0.0
        self.leaked_bytes = 0

    @property
    def leaked_handles(self) -> int:
        return len(self._handles)

    def step(self, dt: float) -> None:
        self._owed_bytes += self.mb_per_min * MB / 60.0 * dt
        self._owed_handles += self.handles_per_min / 60.0 * dt
        # 内存按整 64 KB 的块分配（太碎会让分配器开销盖过泄漏本身）
        block = 64 * 1024
        while self._owed_bytes >= block:
            if self.max_bytes and self.leaked_bytes + block > self.max_bytes:
                self._owed_bytes = 0.0
                break
            self._chunks.append(_touched(block))
            self.leaked_bytes += block
            self._owed_bytes -= block
        while self._owed_handles >= 1.0:
            self._handles.append(self._make_handle())
            self._owed_handles -= 1.0

    def describe(self) -> str:
        return (f"leak 每分钟 {self.mb_per_min:g} MB / {self.handles_per_min:g} 个句柄"
                f"（内存上限 {self.max_bytes // MB if self.max_bytes else '无'} MB）")


class StableWorkload(Workload):
    """锯齿：每个周期内从 0 线性涨到振幅，周期结束整体释放；长期均值不变。"""

    def __init__(self, amplitude_mb: float = DEFAULT_AMPLITUDE_MB,
                 period_s: float = DEFAULT_PERIOD_S):
        self.amplitude_blocks = max(0, int(amplitude_mb))
        self.period_s = max(1.0, period_s)
        self._chunks: List[bytearray] = []
        self._elapsed = 0.0

    @property
    def held_mb(self) -> int:
        return len(self._chunks)

    def step(self, dt: float) -> None:
        self._elapsed += dt
        phase = (self._elapsed % self.period_s) / self.period_s
        want = int(self.amplitude_blocks * phase)
        if want < len(self._chunks):
            self._chunks.clear()
        while len(self._chunks) < want:
            self._chunks.append(_touched(MB))

    def describe(self) -> str:
        return f"stable 锯齿振幅 {self.amplitude_blocks} MB / 周期 {self.period_s:g} s"


class RestartWorkload(Workload):
    """只做一点点事，由 run() 的 lifetime 到点后退出。"""

    def __init__(self, lifetime_s: float = DEFAULT_LIFETIME_S, exit_code: int = 0):
        self.lifetime_s = lifetime_s
        self.exit_code = exit_code
        self._ballast = _touched(MB)

    def step(self, dt: float) -> None:
        self._ballast[0] = (self._ballast[0] + 1) & 0xFF

    def describe(self) -> str:
        return f"restart 存活 {self.lifetime_s:g} s 后以退出码 {self.exit_code} 退出"


def build_workload(args: argparse.Namespace) -> Workload:
    if args.mode == MODE_LEAK:
        return LeakWorkload(args.leak_mb_per_min, args.leak_handles_per_min, args.leak_max_mb)
    if args.mode == MODE_STABLE:
        return StableWorkload(args.amplitude_mb, args.period_s)
    return RestartWorkload(args.lifetime_s, args.exit_code)


def run_workload(workload: Workload, *, tick_s: float, duration_s: float = 0.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> int:
    """按节拍跑负载，直到 duration_s（<= 0 为不限）。返回执行的节拍数。节拍锚定在网格上，
    不因 step 耗时漂移；落后超过一个节拍时从当前时刻重新对齐。"""
    start = clock()
    deadline = start
    last = start
    ticks = 0
    while True:
        now = clock()
        workload.step(now - last)
        last = now
        ticks += 1
        if duration_s > 0 and now - start >= duration_s:
            return ticks
        deadline += tick_s
        now = clock()
        if now > deadline + tick_s:
            deadline = now + tick_s
        wait = deadline - now
        if wait > 0:
            sleep(wait)


# ---------- 参数 ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='soak_target', description="浸泡用被测负载：leak / stable / restart")
    parser.add_argument('--mode', choices=MODES, required=True, help="负载模式")
    parser.add_argument('--tick-s', type=float, default=DEFAULT_TICK_S, help="节拍间隔（秒）")
    parser.add_argument('--duration-s', type=float, default=0.0,
                        help="运行多久后正常退出（秒；0 = 一直跑）")
    parser.add_argument('--pid-file', help="把工作进程的 PID 写进这个文件")
    leak = parser.add_argument_group('leak')
    leak.add_argument('--leak-mb-per-min', type=float, default=DEFAULT_LEAK_MB_PER_MIN,
                      help="每分钟泄漏多少 MB 内存")
    leak.add_argument('--leak-handles-per-min', type=float, default=DEFAULT_LEAK_HANDLES_PER_MIN,
                      help="每分钟泄漏多少个句柄")
    leak.add_argument('--leak-max-mb', type=float, default=DEFAULT_LEAK_MAX_MB,
                      help="内存泄漏累计上限（MB；0 = 不限）")
    stable = parser.add_argument_group('stable')
    stable.add_argument('--amplitude-mb', type=float, default=DEFAULT_AMPLITUDE_MB,
                        help="锯齿振幅（MB）")
    stable.add_argument('--period-s', type=float, default=DEFAULT_PERIOD_S,
                        help="锯齿周期（秒）")
    restart = parser.add_argument_group('restart')
    restart.add_argument('--lifetime-s', type=float, default=DEFAULT_LIFETIME_S,
                         help="每个工作进程的存活时间（秒）")
    restart.add_argument('--exit-code', type=_parse_int, default=0,
                         help="退出时的退出码（可写 0xC0000005 之类的十六进制）")
    restart.add_argument('--crash-every', type=int, default=0,
                         help="监督模式：每第 N 次拉起的进程用崩溃退出码退出（0 = 从不崩溃）")
    restart.add_argument('--crash-code', type=_parse_int, default=DEFAULT_CRASH_CODE,
                         help="崩溃退出码（默认 0xC0000005）")
    restart.add_argument('--supervise', action='store_true',
                         help="监督模式：循环拉起工作进程，工作进程退出后等 --restart-delay-s 再拉起")
    restart.add_argument('--restart-delay-s', type=float, default=DEFAULT_RESTART_DELAY_S,
                         help="监督模式：重新拉起前的等待（秒）")
    restart.add_argument('--max-runs', type=int, default=0,
                         help="监督模式：最多拉起多少次（0 = 不限，测试用）")
    return parser


def validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.tick_s <= 0:
        parser.error("--tick-s 必须大于 0")
    if args.supervise and args.mode != MODE_RESTART:
        parser.error("--supervise 只能与 --mode restart 一起使用")
    if args.mode == MODE_RESTART and args.lifetime_s <= 0:
        parser.error("--lifetime-s 必须大于 0")
    if args.crash_every < 0:
        parser.error("--crash-every 不能为负")


# ---------- 运行 ----------

def write_pid_file(path: Optional[str], pid: Optional[int] = None) -> None:
    """原子写 PID（先写临时文件再替换），读的一方不会读到半截。"""
    if not path:
        return
    tmp = f"{path}.tmp"
    with open(tmp, 'w', encoding='ascii') as handle:
        handle.write(str(os.getpid() if pid is None else pid))
    os.replace(tmp, path)


def exit_code_for_run(run_index: int, args: argparse.Namespace) -> int:
    """监督模式下第 run_index 次（从 1 起）拉起的工作进程的退出码。"""
    if args.crash_every > 0 and run_index % args.crash_every == 0:
        return args.crash_code
    return args.exit_code


def worker_command(args: argparse.Namespace, exit_code: int, pid_file: Optional[str]) -> List[str]:
    command = [sys.executable, os.path.abspath(__file__), '--mode', MODE_RESTART,
               '--tick-s', f"{args.tick_s:g}", '--lifetime-s', f"{args.lifetime_s:g}",
               '--exit-code', str(exit_code)]
    if pid_file:
        command += ['--pid-file', pid_file]
    return command


def supervise(args: argparse.Namespace, *, popen=subprocess.Popen,
              sleep: Callable[[float], None] = time.sleep) -> int:
    """循环拉起 restart 工作进程。工作进程是监督进程的子进程，而不是旧工作进程的子进程，
    所以本工具认它为“重启”。返回拉起的次数。"""
    runs = 0
    while args.max_runs <= 0 or runs < args.max_runs:
        runs += 1
        code = exit_code_for_run(runs, args)
        proc = popen(worker_command(args, code, args.pid_file))
        while proc.poll() is None:
            sleep(_SUPERVISE_POLL_S)
        if args.max_runs > 0 and runs >= args.max_runs:
            break
        sleep(max(0.0, args.restart_delay_s))
    return runs


def run(args: argparse.Namespace) -> int:
    if args.supervise:
        supervise(args)
        return 0
    write_pid_file(args.pid_file)
    workload = build_workload(args)
    print(f"soak_target pid={os.getpid()} {workload.describe()}", flush=True)
    duration = args.duration_s
    if args.mode == MODE_RESTART:
        duration = args.lifetime_s if duration <= 0 else min(duration, args.lifetime_s)
    run_workload(workload, tick_s=args.tick_s, duration_s=duration)
    if args.mode == MODE_RESTART and args.exit_code != 0:
        sys.stdout.flush()
        # 模拟崩溃：不做任何清理，直接带崩溃码退出
        os._exit(to_exit_code(args.exit_code))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate(args, parser)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
