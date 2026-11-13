"""
tools/soak_probe.py：CSV 格式与列契约、目标退出 / 重新出现的事件、IPC 不通的事件、
停止条件（时长、STOP 文件、--on-exit exit）、追加不重复表头，以及对真实进程的读数。
探针的输出能被 soak_verdict 直接读懂（往返用例）。
"""
import csv
import os

import psutil
import pytest

from tools import soak_probe, soak_verdict


class FakeTime:
    """假时钟：sleep 只推进时间。"""

    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeProc:
    def __init__(self, pid):
        self.pid = pid


class FakeReader:
    """读数可编程的读取器；alive 为假时 is_running 为假、read 抛 NoSuchProcess。"""
    instances = []

    def __init__(self, proc):
        self.pid = proc.pid
        self.alive = True
        self.reads = 0
        FakeReader.instances.append(self)

    def is_running(self):
        return self.alive

    def read(self):
        if not self.alive:
            raise psutil.NoSuchProcess(self.pid)
        self.reads += 1
        return {'private_kb': 200_000.0 + self.reads, 'working_set_kb': 250_000.0,
                'handles': 300.0, 'gdi': 20.0, 'user': 15.0, 'threads': 30.0,
                'cpu_percent': 1.5}


@pytest.fixture(autouse=True)
def _reset_readers():
    FakeReader.instances = []


def read_rows(path):
    with open(path, newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def make_probe(tmp_path, clock, *, finder=None, status=None, **options):
    return soak_probe.SoakProbe(
        str(tmp_path), find_target=finder or (lambda: FakeProc(100)), make_reader=FakeReader,
        read_status=status, clock=clock.time, monotonic=clock.time, sleep=clock.sleep,
        log=lambda _text: None, **options)


def task_status(done=99, planned=100, missed=1):
    return lambda: {'tasks': [
        {'task_id': 'task-a', 'name': 'python.exe', 'status': 'running',
         'quality': {'planned_cycles': planned, 'done_cycles': done, 'missed_cycles': missed}},
        {'task_id': 'task-b', 'name': 'python.exe', 'status': 'running', 'quality': None},
    ]}


def events_of(tmp_path):
    return [row['event'] for row in read_rows(tmp_path / soak_probe.EVENTS_FILE)]


# ---------- CSV 契约 ----------

def test_column_contract():
    """列名与顺序是探针与判定之间的契约，改动必须同步 soak_verdict。"""
    assert soak_probe.PROBE_COLUMNS == (
        'timestamp', 'unix_ts', 'pid', 'alive', 'private_kb', 'working_set_kb', 'handles',
        'gdi', 'user', 'threads', 'cpu_percent', 'ipc_ok', 'tasks_running', 'planned_cycles',
        'done_cycles', 'missed_cycles')
    assert soak_probe.TASK_COLUMNS == (
        'timestamp', 'unix_ts', 'task_id', 'name', 'status', 'planned_cycles', 'done_cycles',
        'missed_cycles')
    assert soak_probe.EVENT_COLUMNS == ('timestamp', 'unix_ts', 'event', 'detail')
    for column in soak_verdict.REQUIRED_PROBE_COLUMNS:
        assert column in soak_probe.PROBE_COLUMNS
    for column in soak_verdict.REQUIRED_TASK_COLUMNS:
        assert column in soak_probe.TASK_COLUMNS


def test_probe_row_format(tmp_path):
    clock = FakeTime()
    probe = make_probe(tmp_path, clock, status=task_status(), hours=0.0)
    probe.sample_once()
    probe._probe.close()
    probe._tasks.close()
    probe._events.close()
    raw = (tmp_path / soak_probe.PROBE_FILE).read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')            # UTF-8 BOM，Excel 可直接打开
    [row] = read_rows(tmp_path / soak_probe.PROBE_FILE)
    assert list(row) == list(soak_probe.PROBE_COLUMNS)
    assert row['pid'] == '100' and row['alive'] == '1' and row['ipc_ok'] == '1'
    assert float(row['unix_ts']) == clock.now
    assert row['timestamp'].count(':') == 2
    assert row['private_kb'] == '200001' and row['handles'] == '300'
    assert row['cpu_percent'] == '1.5'
    # 各任务完成情况求和（没有 quality 的任务不计）
    assert (row['tasks_running'], row['planned_cycles'], row['done_cycles'],
            row['missed_cycles']) == ('2', '100', '99', '1')


def test_task_rows_are_written_per_task(tmp_path):
    clock = FakeTime()
    probe = make_probe(tmp_path, clock, status=task_status(), hours=0.0)
    probe.sample_once()
    clock.sleep(10)
    probe.sample_once()
    rows = read_rows(tmp_path / soak_probe.TASKS_FILE)
    assert [r['task_id'] for r in rows] == ['task-a', 'task-b'] * 2
    assert rows[0]['planned_cycles'] == '100' and rows[0]['done_cycles'] == '99'
    assert rows[1]['planned_cycles'] == ''           # 没有 quality 留空


def test_sum_task_cycles_ignores_missing_quality():
    assert soak_probe.sum_task_cycles([
        {'quality': {'planned_cycles': 10, 'done_cycles': 9, 'missed_cycles': 1}},
        {'quality': None}, {}]) == (10, 9, 1)


def test_append_does_not_repeat_the_header(tmp_path):
    clock = FakeTime()
    for _ in range(2):
        probe = make_probe(tmp_path, clock, hours=0.0)
        probe.sample_once()
        clock.sleep(10)
        for sink in (probe._probe, probe._tasks, probe._events):
            sink.close()
    text = (tmp_path / soak_probe.PROBE_FILE).read_text(encoding='utf-8-sig')
    assert text.count('timestamp,unix_ts') == 1
    assert len(read_rows(tmp_path / soak_probe.PROBE_FILE)) == 2


# ---------- 采样节奏与停止 ----------

def test_run_samples_on_the_interval_and_stops_at_hours(tmp_path):
    clock = FakeTime()
    probe = make_probe(tmp_path, clock, interval_s=10, hours=0.01)   # 36 秒
    samples = probe.run()
    rows = read_rows(tmp_path / soak_probe.PROBE_FILE)
    assert samples == len(rows) == 5
    gaps = {round(float(b['unix_ts']) - float(a['unix_ts'])) for a, b in zip(rows, rows[1:])}
    assert gaps == {10}
    assert events_of(tmp_path) == ['probe_start', 'target_found', 'probe_stop']


def test_stop_file_ends_the_run(tmp_path):
    clock = FakeTime()
    probe = make_probe(tmp_path, clock, interval_s=10)
    original_sleep = clock.sleep

    def sleep_then_stop(seconds):
        original_sleep(seconds)
        if clock.now > 1_800_000_000.0 + 25:
            (tmp_path / soak_probe.STOP_FILE).write_text('')

    probe._sleep = sleep_then_stop
    assert probe.run() == 4
    stop = read_rows(tmp_path / soak_probe.EVENTS_FILE)[-1]
    assert stop['event'] == 'probe_stop' and 'stop_file' in stop['detail']


def test_keyboard_interrupt_still_closes_with_a_stop_event(tmp_path):
    clock = FakeTime()
    probe = make_probe(tmp_path, clock, interval_s=10)

    def boom(_seconds):
        raise KeyboardInterrupt

    probe._sleep = boom
    probe.run()
    assert events_of(tmp_path)[-1] == 'probe_stop'


def test_a_late_wakeup_does_not_burst_catch_up_samples(tmp_path):
    """休眠唤醒后一次落后很多个间隔：只补一个点，不连续补采。"""
    clock = FakeTime()
    probe = make_probe(tmp_path, clock, interval_s=10, hours=0.02)   # 72 秒
    original_sleep = clock.sleep
    calls = {'n': 0}

    def sleep_with_nap(seconds):
        calls['n'] += 1
        original_sleep(seconds + (40 if calls['n'] == 2 else 0))

    probe._sleep = sleep_with_nap
    probe.run()
    rows = read_rows(tmp_path / soak_probe.PROBE_FILE)
    gaps = [round(float(b['unix_ts']) - float(a['unix_ts'])) for a, b in zip(rows, rows[1:])]
    assert max(gaps) == 50 and gaps.count(10) == len(gaps) - 1


# ---------- 目标退出 / 重新出现 ----------

def test_target_exit_is_recorded_and_probe_waits_for_a_new_instance(tmp_path):
    clock = FakeTime()
    candidates = [FakeProc(100), None, None, FakeProc(200)]

    def finder():
        return candidates.pop(0) if candidates else None

    probe = make_probe(tmp_path, clock, finder=finder, interval_s=10)
    probe.sample_once()                          # 找到 pid 100，读数
    FakeReader.instances[0].alive = False        # 工具崩溃
    probe.sample_once()                          # target_exited，找不到
    probe.sample_once()                          # 仍找不到，不重复记事件
    probe.sample_once()                          # 新实例 pid 200
    for sink in (probe._probe, probe._tasks, probe._events):
        sink.close()
    assert events_of(tmp_path) == ['target_found', 'target_exited', 'target_not_found',
                                   'target_found']
    rows = read_rows(tmp_path / soak_probe.PROBE_FILE)
    assert [(r['pid'], r['alive']) for r in rows] == [('100', '1'), ('', '0'), ('', '0'),
                                                      ('200', '1')]
    assert rows[1]['private_kb'] == ''


def test_exit_between_check_and_read_marks_the_row_dead(tmp_path):
    clock = FakeTime()
    probe = make_probe(tmp_path, clock)
    probe.sample_once()
    reader = FakeReader.instances[0]
    reader.is_running = lambda: True             # 检查时还在，读的瞬间退出
    reader.alive = False
    probe.sample_once()
    rows = read_rows(tmp_path / soak_probe.PROBE_FILE)
    assert rows[1]['alive'] == '0'
    for sink in (probe._probe, probe._tasks, probe._events):
        sink.close()


def test_on_exit_exit_ends_the_run_after_recording_the_exit(tmp_path):
    clock = FakeTime()
    probe = make_probe(tmp_path, clock, interval_s=10, on_exit='exit')
    original_sleep = clock.sleep
    ticks = {'n': 0}

    def sleep_then_kill(seconds):
        original_sleep(seconds)
        ticks['n'] += 1
        if ticks['n'] == 2:
            FakeReader.instances[0].alive = False

    probe._sleep = sleep_then_kill
    assert probe.run() == 3
    names = events_of(tmp_path)
    assert names == ['probe_start', 'target_found', 'target_exited', 'probe_stop']
    assert 'target_exited' in read_rows(tmp_path / soak_probe.EVENTS_FILE)[-1]['detail']
    assert read_rows(tmp_path / soak_probe.PROBE_FILE)[-1]['alive'] == '0'


def test_finder_errors_do_not_kill_the_probe(tmp_path):
    clock = FakeTime()

    def broken():
        raise RuntimeError("psutil 出错")

    probe = make_probe(tmp_path, clock, finder=broken)
    assert probe.sample_once() is True
    rows = read_rows(tmp_path / soak_probe.PROBE_FILE)
    assert rows[0]['alive'] == '0'
    for sink in (probe._probe, probe._tasks, probe._events):
        sink.close()


# ---------- IPC ----------

def test_ipc_loss_and_recovery_are_recorded_once_each(tmp_path):
    clock = FakeTime()
    results = [True, False, False, True, True]

    def status():
        if results.pop(0):
            return {'tasks': []}
        raise OSError("管道不通")

    probe = make_probe(tmp_path, clock, status=status)
    for _ in range(5):
        probe.sample_once()
        clock.sleep(10)
    for sink in (probe._probe, probe._tasks, probe._events):
        sink.close()
    names = events_of(tmp_path)
    assert names == ['target_found', 'ipc_lost', 'ipc_ok']
    rows = read_rows(tmp_path / soak_probe.PROBE_FILE)
    assert [r['ipc_ok'] for r in rows] == ['1', '0', '0', '1', '1']
    assert rows[1]['planned_cycles'] == ''           # 不通时不写任务数据


def test_ipc_failing_from_the_start_is_an_event(tmp_path):
    clock = FakeTime()

    def status():
        raise OSError("没有管道")

    probe = make_probe(tmp_path, clock, status=status)
    probe.sample_once()
    for sink in (probe._probe, probe._tasks, probe._events):
        sink.close()
    assert events_of(tmp_path) == ['target_found', 'ipc_lost']


# ---------- 真实进程 ----------

def test_real_reader_reads_this_process():
    reader = soak_probe.ProcessReader(psutil.Process())
    assert reader.is_running()
    values = reader.read()
    assert values['private_kb'] > 1000 and values['working_set_kb'] > 1000
    assert values['threads'] >= 1 and values['handles'] > 0
    assert values['cpu_percent'] is not None
    assert values['gdi'] is None or values['gdi'] >= 0


def test_real_run_against_this_process_writes_readable_samples(tmp_path):
    me = psutil.Process()
    probe = soak_probe.SoakProbe(str(tmp_path), interval_s=0.05, hours=0.00005,
                                 find_target=lambda: me, log=lambda _text: None)
    assert probe.run() >= 2
    rows = read_rows(tmp_path / soak_probe.PROBE_FILE)
    assert all(r['pid'] == str(os.getpid()) and r['alive'] == '1' for r in rows)
    assert all(float(r['private_kb']) > 0 for r in rows)


def test_find_by_name_matches_case_insensitively_and_excludes_pids():
    me = psutil.Process()
    found = soak_probe.find_by_name(me.name().upper())
    assert found is not None
    assert soak_probe.find_by_name("no_such_process_xyz.exe") is None
    assert soak_probe.find_by_name(me.name(), cmdline_contains="zzz_no_such_text") is None
    assert soak_probe.find_by_name(me.name(), cmdline_contains="pytest") is not None


def test_default_data_dir_is_the_installed_location(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert soak_probe.default_data_dir() == os.path.join(str(tmp_path), "进程监控助手", "data")


def test_pipe_path_follows_the_instance_name(tmp_path):
    from utils.instance_name import pipe_path, single_instance_name
    assert soak_probe.ipc_pipe_path(str(tmp_path)) == pipe_path(
        single_instance_name(str(tmp_path)))


def test_cli_argument_rules(tmp_path):
    with pytest.raises(SystemExit):
        soak_probe.main(['--out-dir', str(tmp_path), '--by-ipc'])          # 缺 --data-dir
    with pytest.raises(SystemExit):
        soak_probe.main(['--out-dir', str(tmp_path), '--pid', '1', '--name', 'x'])
    with pytest.raises(SystemExit):
        soak_probe.main(['--out-dir', str(tmp_path), '--interval', '0'])


# ---------- 往返：探针写、判定读 ----------

def test_verdict_reads_what_the_probe_writes(tmp_path):
    """用假时钟模拟 1 小时（10 秒一个点）平稳的探针记录，判定能直接读懂并通过。"""
    clock = FakeTime()
    reader_values = {'n': 0}

    class SteadyReader(FakeReader):
        def read(self):
            reader_values['n'] += 1
            wobble = (reader_values['n'] % 40) * 100.0          # 4 MB 内的锯齿
            return {'private_kb': 300_000.0 + wobble, 'working_set_kb': 350_000.0,
                    'handles': 300.0 + (reader_values['n'] % 3), 'gdi': 20.0, 'user': 15.0,
                    'threads': 30.0, 'cpu_percent': 0.4}

    state = {'done': 0}

    def status():
        state['done'] += 10
        return {'tasks': [{'task_id': 'task-a', 'name': 'x', 'status': 'running',
                           'quality': {'planned_cycles': state['done'] + 1,
                                       'done_cycles': state['done'], 'missed_cycles': 1}}]}

    probe = soak_probe.SoakProbe(
        str(tmp_path), interval_s=10, hours=1.0, find_target=lambda: FakeProc(7),
        make_reader=SteadyReader, read_status=status, clock=clock.time,
        monotonic=clock.time, sleep=clock.sleep, log=lambda _text: None)
    probe.run()
    report = soak_verdict.load_and_evaluate(str(tmp_path), hours=0.8)
    assert report.passed, soak_verdict.format_report(report)
