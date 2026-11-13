"""
tests/fixtures/soak_target.py：三种模式的参数解析、负载的累积行为、节拍循环、监督重启，
以及真实子进程的短时间运行（退出码、PID 文件）。
"""
import os
import subprocess
import sys

import pytest

from tests.fixtures import soak_target as target

SCRIPT = os.path.abspath(target.__file__)


def parse(*argv):
    parser = target.build_parser()
    args = parser.parse_args(list(argv))
    target.validate(args, parser)
    return args


# ---------- 参数 ----------

def test_leak_defaults():
    args = parse('--mode', 'leak')
    assert (args.leak_mb_per_min, args.leak_handles_per_min, args.leak_max_mb) == (
        target.DEFAULT_LEAK_MB_PER_MIN, target.DEFAULT_LEAK_HANDLES_PER_MIN,
        target.DEFAULT_LEAK_MAX_MB)
    assert isinstance(target.build_workload(args), target.LeakWorkload)


def test_leak_rates_are_configurable():
    args = parse('--mode', 'leak', '--leak-mb-per-min', '12', '--leak-handles-per-min', '7',
                 '--leak-max-mb', '0', '--tick-s', '0.5')
    workload = target.build_workload(args)
    assert (workload.mb_per_min, workload.handles_per_min, workload.max_bytes) == (12, 7, 0)
    assert args.tick_s == 0.5


def test_stable_parameters():
    args = parse('--mode', 'stable', '--amplitude-mb', '8', '--period-s', '30')
    workload = target.build_workload(args)
    assert (workload.amplitude_blocks, workload.period_s) == (8, 30)


def test_restart_parameters_accept_hex_exit_codes():
    args = parse('--mode', 'restart', '--lifetime-s', '90', '--exit-code', '0xC0000005',
                 '--crash-every', '3', '--supervise', '--pid-file', 'x.pid')
    assert args.exit_code == 0xC0000005 and args.crash_every == 3 and args.supervise
    assert isinstance(target.build_workload(args), target.RestartWorkload)


@pytest.mark.parametrize("argv", [
    [],                                                       # 缺 --mode
    ['--mode', 'bogus'],
    ['--mode', 'leak', '--tick-s', '0'],
    ['--mode', 'leak', '--supervise'],                        # 监督只属于 restart
    ['--mode', 'restart', '--lifetime-s', '0'],
    ['--mode', 'restart', '--crash-every', '-1'],
])
def test_invalid_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as info:
        parse(*argv)
    assert info.value.code == 2


# ---------- 负载 ----------

class Handles:
    def __init__(self):
        self.made = 0

    def __call__(self):
        self.made += 1
        return object()


def test_leak_accumulates_memory_and_handles_without_releasing():
    handles = Handles()
    leak = target.LeakWorkload(mb_per_min=60, handles_per_min=120, max_mb=0, make_handle=handles)
    for _ in range(5):
        leak.step(1.0)
    assert leak.leaked_bytes == 5 * target.MB          # 1 MB/s，按 64 KB 块对齐后恰好整数
    assert leak.leaked_handles == handles.made == 10    # 2 个/s
    before = leak.leaked_bytes
    leak.step(0.0)
    assert leak.leaked_bytes == before                  # 没有释放


def test_leak_carries_fractional_amounts_between_ticks():
    handles = Handles()
    leak = target.LeakWorkload(mb_per_min=0, handles_per_min=30, max_mb=0, make_handle=handles)
    for _ in range(10):
        leak.step(0.3)          # 每次 0.15 个，累计 1.5 个
    assert handles.made == 1


def test_leak_memory_stops_at_the_cap():
    leak = target.LeakWorkload(mb_per_min=6000, handles_per_min=0, max_mb=3,
                               make_handle=Handles())
    for _ in range(10):
        leak.step(1.0)
    assert leak.leaked_bytes <= 3 * target.MB
    assert leak.leaked_bytes >= 3 * target.MB - 64 * 1024


def test_leak_real_handles_are_kernel_objects():
    """默认的句柄泄漏在 Windows 上是真实的内核句柄：进程句柄数跟着涨。"""
    import psutil
    proc = psutil.Process()
    before = proc.num_handles()
    leak = target.LeakWorkload(mb_per_min=0, handles_per_min=600, max_mb=0)
    leak.step(1.0)     # 10 个
    after = proc.num_handles()
    assert leak.leaked_handles == 10 and after - before >= 10


def test_stable_is_a_sawtooth_that_resets():
    stable = target.StableWorkload(amplitude_mb=10, period_s=10)
    held = []
    for _ in range(30):                  # 三个周期
        stable.step(1.0)
        held.append(stable.held_mb)
    assert max(held) <= 10
    assert max(held) >= 8
    drops = [b < a for a, b in zip(held, held[1:])]
    assert sum(drops) == 3               # 三个周期各在周期边界处整体释放一次
    assert held[-1] == 0                 # 第 30 秒正好是周期边界，回到谷底


def test_stable_does_not_trend_over_many_periods():
    stable = target.StableWorkload(amplitude_mb=6, period_s=6)
    peaks = []
    for period in range(5):
        peak = 0
        for _ in range(6):
            stable.step(1.0)
            peak = max(peak, stable.held_mb)
        peaks.append(peak)
    assert len(set(peaks)) == 1


def test_run_workload_ticks_on_a_fixed_grid():
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    class Counter(target.Workload):
        def __init__(self):
            self.dts = []

        def step(self, dt):
            self.dts.append(dt)
            now[0] += 0.02          # step 自己耗时 20 ms，不能让节拍漂移

    workload = Counter()
    ticks = target.run_workload(workload, tick_s=0.5, duration_s=2.0,
                                clock=lambda: now[0], sleep=sleep)
    assert ticks == 5
    assert workload.dts[0] == 0.0
    assert all(abs(dt - 0.5) < 0.05 for dt in workload.dts[1:])


# ---------- 退出码与监督 ----------

def test_to_exit_code_wraps_ntstatus_into_int_range():
    assert target.to_exit_code(0xC0000005) == -1073741819
    assert target.to_exit_code(0xC0000005) & 0xFFFFFFFF == 0xC0000005
    assert target.to_exit_code(3) == 3
    assert target.to_exit_code(0) == 0


def test_exit_code_for_run_crashes_every_nth_run():
    args = parse('--mode', 'restart', '--crash-every', '3', '--supervise')
    codes = [target.exit_code_for_run(i, args) for i in range(1, 7)]
    assert codes == [0, 0, target.DEFAULT_CRASH_CODE, 0, 0, target.DEFAULT_CRASH_CODE]
    never = parse('--mode', 'restart')
    assert {target.exit_code_for_run(i, never) for i in range(1, 7)} == {0}


class FakePopen:
    launched = []

    def __init__(self, command, **_kwargs):
        FakePopen.launched.append(command)
        self.polls = 1

    def poll(self):
        self.polls -= 1
        return 0 if self.polls < 0 else None


def test_supervisor_relaunches_workers_with_the_planned_exit_codes(tmp_path):
    FakePopen.launched = []
    args = parse('--mode', 'restart', '--supervise', '--lifetime-s', '5', '--crash-every', '2',
                 '--max-runs', '4', '--restart-delay-s', '0.5', '--pid-file',
                 str(tmp_path / 'r.pid'))
    sleeps = []
    runs = target.supervise(args, popen=FakePopen, sleep=sleeps.append)
    assert runs == 4 and len(FakePopen.launched) == 4
    codes = [command[command.index('--exit-code') + 1] for command in FakePopen.launched]
    assert codes == ['0', str(target.DEFAULT_CRASH_CODE), '0', str(target.DEFAULT_CRASH_CODE)]
    for command in FakePopen.launched:
        assert '--supervise' not in command           # 工作进程不是监督进程
        assert command[command.index('--pid-file') + 1] == str(tmp_path / 'r.pid')
        assert command[command.index('--lifetime-s') + 1] == '5'
    assert sleeps.count(0.5) == 3                       # 每两次拉起之间等一次，最后一次后不等


def test_write_pid_file_is_atomic_and_replaces(tmp_path):
    path = tmp_path / 'p.pid'
    target.write_pid_file(str(path), 111)
    target.write_pid_file(str(path), 222)
    assert path.read_text() == '222'
    assert not (tmp_path / 'p.pid.tmp').exists()
    target.write_pid_file(None)             # 没给路径不报错


# ---------- 真实子进程 ----------

def run_target(*argv, timeout=20):
    return subprocess.run([sys.executable, SCRIPT, *argv], capture_output=True, timeout=timeout)


def test_real_leak_run_exits_cleanly_and_writes_its_pid(tmp_path):
    pid_file = tmp_path / 'leak.pid'
    result = run_target('--mode', 'leak', '--tick-s', '0.05', '--duration-s', '0.4',
                        '--leak-mb-per-min', '120', '--leak-handles-per-min', '600',
                        '--pid-file', str(pid_file))
    assert result.returncode == 0
    assert pid_file.read_text().isdigit()


def test_real_stable_run_exits_cleanly():
    result = run_target('--mode', 'stable', '--tick-s', '0.05', '--duration-s', '0.4',
                        '--amplitude-mb', '4', '--period-s', '1')
    assert result.returncode == 0


def test_real_restart_run_exits_with_the_crash_code():
    result = run_target('--mode', 'restart', '--tick-s', '0.05', '--lifetime-s', '0.3',
                        '--exit-code', '0xC0000005')
    assert result.returncode & 0xFFFFFFFF == 0xC0000005


def test_real_restart_run_exits_normally_without_a_code():
    result = run_target('--mode', 'restart', '--tick-s', '0.05', '--lifetime-s', '0.3')
    assert result.returncode == 0


def test_real_supervisor_restarts_the_worker(tmp_path):
    pid_file = tmp_path / 'r.pid'
    result = run_target('--mode', 'restart', '--supervise', '--tick-s', '0.05',
                        '--lifetime-s', '0.3', '--restart-delay-s', '0.1', '--max-runs', '2',
                        '--crash-every', '2', '--pid-file', str(pid_file))
    assert result.returncode == 0
    assert pid_file.read_text().isdigit()
