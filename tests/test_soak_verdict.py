"""
tools/soak_verdict.py：用合成 CSV 表驱动检查浸泡判定（平稳通过、内存 / 句柄超标失败、
完成率不足失败、工具退出失败、记录不足失败）以及命令行退出码。
"""
import csv
import json
import math
import random

import pytest

from tools import soak_probe, soak_verdict

T0 = 1_800_000_000.0
STEP_S = 10.0
KB_PER_MB = 1024.0

# 合成数据长 0.6 小时，判定窗口取 0.5 小时（>= 趋势判定要求的 10 分钟跨度）
DATA_HOURS = 0.6
WINDOW_HOURS = 0.5


def _noise(seed):
    rng = random.Random(seed)
    return lambda: rng.uniform(-1.0, 1.0)


def stable_private(i, n):
    """300 MB 上下 ±4 MB 起伏，不涨。"""
    return 300 * KB_PER_MB + 4 * KB_PER_MB * math.sin(i / 9.0)


def ramp(total_mb):
    """整个记录期间线性涨 total_mb（起伏只有 ±0.5 MB，趋势拟合很干净）。"""
    return lambda i, n: (300 * KB_PER_MB + 0.5 * KB_PER_MB * math.sin(i / 9.0)
                         + total_mb * KB_PER_MB * i / max(1, n - 1))


def step_up(total_mb):
    """进入窗口后不久一次性涨 total_mb，之后持平（阶梯式上涨）。"""
    return lambda i, n: stable_private(i, n) + (total_mb * KB_PER_MB if i > n * 0.2 else 0.0)


def flat(value):
    return lambda i, n: value


def noisy_flat(value, seed=3):
    noise = _noise(seed)
    return lambda i, n: value + round(noise())


def handle_ramp(total):
    return lambda i, n: 300 + total * i / max(1, n - 1)


def write_probe_csv(directory, *, private=stable_private, handles=None, gdi=None, user=None,
                    threads=None, hours=DATA_HOURS):
    handles = handles or noisy_flat(300, 1)
    gdi = gdi or flat(20)
    user = user or flat(15)
    threads = threads or noisy_flat(30, 2)
    n = int(hours * 3600 / STEP_S)
    path = directory / soak_probe.PROBE_FILE
    with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(soak_probe.PROBE_COLUMNS)
        for i in range(n):
            row = dict.fromkeys(soak_probe.PROBE_COLUMNS, '')
            row.update(unix_ts=T0 + i * STEP_S, pid=1234, alive=1,
                       private_kb=private(i, n), working_set_kb=private(i, n) + 5000,
                       handles=handles(i, n), gdi=gdi(i, n), user=user(i, n),
                       threads=threads(i, n), cpu_percent=0.5, ipc_ok=1)
            writer.writerow([row[c] for c in soak_probe.PROBE_COLUMNS])
    return n


def write_tasks_csv(directory, tasks):
    """tasks: {task_id: (planned, done, missed)}，每个任务写开头与结尾两行（累计值）。"""
    path = directory / soak_probe.TASKS_FILE
    with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(soak_probe.TASK_COLUMNS)
        for task_id, (planned, done, missed) in tasks.items():
            writer.writerow([T0, T0, task_id, 'python.exe', 'running', 10, 10, 0])
            writer.writerow([T0 + 3600, T0 + 3600, task_id, 'python.exe', 'running',
                             planned, done, missed])


def write_events_csv(directory, events):
    path = directory / soak_probe.EVENTS_FILE
    with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(soak_probe.EVENT_COLUMNS)
        for stamp, name in events:
            writer.writerow([stamp, T0, name, ''])


GOOD_TASKS = {'task-a': (36000, 35990, 10), 'task-b': (36000, 36000, 0)}


def evaluate(directory, **options):
    options.setdefault('hours', WINDOW_HOURS)
    return soak_verdict.load_and_evaluate(str(directory), **options)


def failed_names(report):
    return [c.name for c in report.checks if not c.passed]


def make_run(directory, *, tasks=GOOD_TASKS, events=(), **probe_options):
    write_probe_csv(directory, **probe_options)
    if tasks is not None:
        write_tasks_csv(directory, tasks)
    write_events_csv(directory, events)


# (用例名, 探针参数, 任务, 事件, 期望失败的检查项)
CASES = [
    pytest.param({}, GOOD_TASKS, (), [], id="平稳通过"),
    pytest.param({'private': ramp(60)}, GOOD_TASKS, (), ["私有字节"], id="内存涨 60MB 失败"),
    pytest.param({'private': ramp(20)}, GOOD_TASKS, (), ["私有字节"],
                 id="内存只涨 20MB 但判定持续上涨仍失败"),
    pytest.param({'private': step_up(45)}, GOOD_TASKS, (), ["私有字节"], id="阶梯式涨 45MB 失败"),
    pytest.param({}, {'task-a': (36000, 35000, 1000)}, (), ["采样完成率"], id="完成率 97.2% 失败"),
    pytest.param({}, {'task-a': (36000, 35820, 180), 'task-b': (36000, 36000, 0)}, (), [],
                 id="完成率恰好 99.5% 通过"),
    pytest.param({'handles': handle_ramp(250)}, GOOD_TASKS, (), ["句柄数"], id="句柄涨 250 失败"),
    pytest.param({'gdi': handle_ramp(80)}, GOOD_TASKS, (), ["GDI 对象数"], id="GDI 上涨失败"),
    pytest.param({'user': handle_ramp(80)}, GOOD_TASKS, (), ["USER 对象数"], id="USER 上涨失败"),
    pytest.param({'threads': handle_ramp(60)}, GOOD_TASKS, (), ["线程数"], id="线程上涨失败"),
    pytest.param({'handles': handle_ramp(4)}, GOOD_TASKS, (), [], id="句柄涨 4 个在容差内通过"),
    pytest.param({}, GOOD_TASKS, (("t", 'target_exited'),), ["工具未退出"], id="工具退出过失败"),
    pytest.param({}, None, (), ["采样完成率"], id="没有任务记录失败"),
    pytest.param({'hours': 0.3}, GOOD_TASKS, (), ["记录覆盖"], id="记录比窗口短失败"),
]


@pytest.mark.parametrize("probe_options,tasks,events,expected_failed", CASES)
def test_verdict_table(tmp_path, probe_options, tasks, events, expected_failed):
    make_run(tmp_path, tasks=tasks, events=events, **probe_options)
    report = evaluate(tmp_path)
    assert failed_names(report) == expected_failed
    assert report.passed == (not expected_failed)


def test_allow_exit_lets_a_restarted_tool_pass(tmp_path):
    make_run(tmp_path, events=(("t", 'target_exited'),))
    report = evaluate(tmp_path, allow_exit=True)
    assert report.passed
    exits = next(c for c in report.checks if c.name == "工具未退出")
    assert exits.note


def test_small_handle_rise_is_reported_as_a_note(tmp_path):
    make_run(tmp_path, handles=handle_ramp(4))
    report = evaluate(tmp_path)
    handles = next(c for c in report.checks if c.name == "句柄数")
    assert handles.passed and "容差" in handles.note


def test_private_growth_limit_is_configurable(tmp_path):
    make_run(tmp_path, private=ramp(20))
    # 趋势判定仍是持续上涨，所以放宽增长上限也不能让它通过
    report = evaluate(tmp_path, max_growth_mb=100)
    assert failed_names(report) == ["私有字节"]
    assert "持续上涨" in report.checks[1].detail


def test_window_ignores_warmup_growth(tmp_path):
    """前面预热阶段涨得很厉害、窗口里已经持平：只看窗口，应通过。"""
    n = int(DATA_HOURS * 3600 / STEP_S)

    def warmup_then_flat(i, _n):
        return stable_private(i, n) + 200 * KB_PER_MB * min(i, 30) / 30
    make_run(tmp_path, private=warmup_then_flat)
    assert evaluate(tmp_path, hours=0.5).passed


def test_gaps_from_sleep_do_not_break_trend(tmp_path):
    """休眠造成的样本空洞（时间戳跳变）不影响趋势拟合：按墙钟时间算。"""
    make_run(tmp_path)
    path = tmp_path / soak_probe.PROBE_FILE
    with open(path, newline='', encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))
    kept = [r for i, r in enumerate(rows) if not 100 <= i < 108]
    with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=soak_probe.PROBE_COLUMNS)
        writer.writeheader()
        writer.writerows(kept)
    assert evaluate(tmp_path).passed


def test_dead_rows_are_not_counted_as_samples(tmp_path):
    """alive=0 的行（工具不在时的占位）不参与趋势，也不算有效样本。"""
    samples = [soak_verdict.Sample(T0 + i * STEP_S, i % 2 == 0, {'private_kb': 1e5})
               for i in range(10)]
    points = soak_verdict.series_of(samples, 'private_kb')
    assert len(points) == 5


def test_no_samples_fails_cleanly():
    report = soak_verdict.evaluate([], [], [], hours=1)
    assert not report.passed and failed_names(report) == ["记录覆盖"]


def test_every_check_is_reported(tmp_path):
    make_run(tmp_path)
    names = [c.name for c in evaluate(tmp_path).checks]
    assert names == ["记录覆盖", "私有字节", "句柄数", "GDI 对象数", "USER 对象数", "线程数",
                     "采样完成率", "工具未退出"]


# ---------- 命令行 ----------

def test_cli_exit_codes_and_text(tmp_path, capsys):
    make_run(tmp_path)
    assert soak_verdict.main([str(tmp_path), '--hours', str(WINDOW_HOURS)]) == 0
    assert "结论：通过" in capsys.readouterr().out

    bad = tmp_path / "bad"
    bad.mkdir()
    make_run(bad, private=ramp(60))
    assert soak_verdict.main([str(bad), '--hours', str(WINDOW_HOURS)]) == 1
    assert "结论：未通过（私有字节）" in capsys.readouterr().out


def test_cli_accepts_the_csv_path_and_json(tmp_path, capsys):
    make_run(tmp_path)
    code = soak_verdict.main([str(tmp_path / soak_probe.PROBE_FILE), '--hours',
                              str(WINDOW_HOURS), '--json'])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data['passed'] is True and len(data['checks']) == 8


def test_cli_input_errors_exit_2(tmp_path, capsys):
    assert soak_verdict.main([str(tmp_path)]) == 2          # 目录里没有 CSV
    broken = tmp_path / soak_probe.PROBE_FILE
    broken.write_text("a,b\n1,2\n", encoding='utf-8')
    assert soak_verdict.main([str(tmp_path)]) == 2          # 缺列
    assert "缺少列" in capsys.readouterr().err


def test_cli_rejects_non_positive_hours(tmp_path):
    with pytest.raises(SystemExit) as info:
        soak_verdict.main([str(tmp_path), '--hours', '0'])
    assert info.value.code == 2
