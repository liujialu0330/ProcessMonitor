"""性能证据的实际阈值、隔离、锁和计时边界；使用小文件，不运行性能负载。"""
import json
import multiprocessing
from pathlib import Path
import time

import pytest

from tests.perf.artifacts import file_lock, write_json
from tests.perf.conftest import PerfHelper, PerfSession, SynthCache
from tests.perf import observations
from tests.perf import synth


def session(tmp_path, *, update=False, limits=None):
    current = PerfSession(update, cache_dir=tmp_path,
                          budgets={'calibration_ms': 100, 'budgets': {
                              name: {'budget': value} for name, value in (limits or {}).items()}},
                          metadata={'source_sha256': {'example.py': 'test'}})
    current._factor = 1.0
    current._calibration = 100.0
    return current


def test_scenario_target_is_the_reported_limit_and_observed_values_are_not_assertions(tmp_path):
    current = session(tmp_path, limits={'collect': 15.2, 'snapshot': 1, 'catalog': 26.4})
    helper = PerfHelper(current)
    with pytest.raises(AssertionError):
        helper.check('collect', 15.00001, target=15)
    helper.check('catalog', 50, target=100)
    current.record('snapshot', 500)
    results = json.loads(current.result_path.read_text(encoding='utf-8'))['results']
    assert results['collect']['limit'] == results['collect']['budget'] == 15
    assert results['collect']['value'] == 15.00001
    assert results['collect']['status'] == 'failed'
    assert results['collect']['baseline_budget'] == 15.2
    assert results['catalog']['limit'] == 100 and results['catalog']['status'] == 'passed'
    assert results['snapshot']['status'] == 'observed'
    assert results['snapshot']['limit'] is None and not results['snapshot']['assertion_enabled']


def test_both_write_percentiles_are_recorded_even_if_first_fails(tmp_path):
    current = session(tmp_path, limits={'p50': 2, 'p95': 6})
    with pytest.raises(AssertionError):
        PerfHelper(current).check_many([('p50', [3, 4, 3], '中位'), ('p95', [5, 5, 5], '尾部')])
    assert current.results['p50']['status'] == 'failed'
    assert current.results['p95']['status'] == 'passed'


def test_update_mode_records_without_claiming_assertion_passed(tmp_path):
    current = session(tmp_path, update=True, limits={'sample': 1})
    PerfHelper(current).check('sample', 100)
    assert current.results['sample']['status'] == 'recorded'
    assert not current.results['sample']['assertion_enabled']
    assert current.results['sample']['limit'] == 1


@pytest.mark.parametrize('override', ['0', '-1', 'inf', 'nan', 'bad'])
def test_invalid_machine_factor_cannot_be_cached(tmp_path, monkeypatch, override):
    current = session(tmp_path)
    current._factor = None
    monkeypatch.setenv('PERF_MACHINE_FACTOR', override)
    for _ in range(2):
        with pytest.raises(ValueError):
            _ = current.factor
    assert current._factor is None


def test_report_keeps_exact_limit_precision(tmp_path):
    current = session(tmp_path, limits={'example': 1.0000001})
    PerfHelper(current).check('example', 1.0000001)
    assert current.results['example']['budget'] == current.results['example']['limit'] == 1.0000001


def test_independent_run_history_survives_latest_alias_updates(tmp_path):
    first, second = session(tmp_path), session(tmp_path)
    first.record('a', 1)
    second.record('b', 2)
    first.finish(0)
    second.finish(1)
    assert first.run_id != second.run_id
    assert json.loads(first.result_path.read_text(encoding='utf-8'))['results']['a']['value'] == 1
    assert json.loads(second.result_path.read_text(encoding='utf-8'))['results']['b']['value'] == 2
    latest = json.loads((tmp_path / 'last_run.json').read_text(encoding='utf-8'))
    assert latest['run_id'] == second.run_id and latest['exit_code'] == 1
    assert latest['started_at'] and latest['finished_at']


def test_phase_observation_does_not_run_inside_timed_function(tmp_path, monkeypatch):
    current = session(tmp_path)
    events = []

    def observe():
        events.append('observation')
        return {'wall_s': len(events), 'process_cpu_s': len(events) / 2}

    monkeypatch.setattr(observations, 'snapshot', observe)
    try:
        phase = current.begin_phase('example')
        PerfHelper(current).time_ms(lambda: events.append('work'), repeat=3)
        current.end_phase(phase)
    finally:
        current.finish(0)
    assert events == ['observation', 'work', 'work', 'work', 'observation']
    result = json.loads(current.result_path.read_text(encoding='utf-8'))
    assert result['phases'][0]['wall_s'] == 4
    assert result['metadata']['source_sha256'] == {'example.py': 'test'}


def _cache_process(root, gate):
    cache = SynthCache(Path(root))
    gate.wait(10)

    def build(path):
        with (Path(root) / 'builds.txt').open('a', encoding='utf-8') as stream:
            stream.write('build\n')
        time.sleep(0.1)
        Path(path).write_bytes(b'complete')
        return {'rows': 7}

    path, info = cache._get('tiny', 'same', build)
    assert Path(path).read_bytes() == b'complete' and info['rows'] == 7


def _writer_process(path, gate, worker):
    gate.wait(10)
    for i in range(40):
        write_json(Path(path), {'worker': worker, 'sequence': i, 'text': '完整' * 100})


def _run_children(processes):
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()  # 只结束本用例自己创建且超时的辅助进程。
                process.join(5)


def test_two_processes_build_same_cache_once(tmp_path):
    context = multiprocessing.get_context('spawn')
    gate = context.Barrier(2)
    _run_children([context.Process(target=_cache_process, args=(str(tmp_path), gate))
                   for _ in range(2)])
    assert (tmp_path / 'builds.txt').read_text(encoding='utf-8').splitlines() == ['build']


def test_concurrent_json_writers_keep_complete_result_without_temp_collisions(tmp_path):
    context = multiprocessing.get_context('spawn')
    gate = context.Barrier(2)
    path = tmp_path / 'result.json'
    _run_children([context.Process(target=_writer_process, args=(str(path), gate, i))
                   for i in range(2)])
    value = json.loads(path.read_text(encoding='utf-8'))
    assert value['sequence'] == 39 and value['text'] == '完整' * 100
    assert not list(tmp_path.glob('*.tmp'))


def test_cache_failure_preserves_other_digests_and_retries(tmp_path):
    cache = SynthCache(tmp_path)

    def complete(path):
        Path(path).write_bytes(b'old')
        return {'rows': 1}

    previous, _ = cache._get('tiny', 'old', complete)

    def failing(path):
        Path(path).write_bytes(b'partial')
        raise RuntimeError('失败')

    with pytest.raises(RuntimeError):
        cache._get('tiny', 'new', failing)
    assert Path(previous).read_bytes() == b'old'
    assert not list(tmp_path.glob('*.building.db'))
    newer, _ = cache._get('tiny', 'new', complete)
    assert Path(newer).is_file() and Path(previous).is_file()


def test_lock_timeout_then_release(tmp_path):
    path = tmp_path / 'cache.lock'
    with file_lock(path):
        with pytest.raises(TimeoutError):
            with file_lock(path, timeout_s=0):
                pytest.fail('锁不应被第二个持有者取得')
    with file_lock(path, timeout_s=0):
        pass


def test_observations_are_read_only_metadata():
    before = observations.snapshot()
    assert before['wall_s'] > 0 and before['process_cpu_s'] >= 0
    assert before['pid'] > 0
    assert 'process' in before or 'observation_error' in before


def test_dataset_evidence_contains_reproducible_parameters(tmp_path, monkeypatch):
    current = session(tmp_path)
    spec = synth.TaskSpec('tiny', duration_s=10, seed=42, metrics=('num_handles',))

    def build(path, received):
        assert received is spec
        Path(path).write_bytes(b'complete')
        return {'rows': 10}

    monkeypatch.setattr(synth, 'build_task_db', build)
    SynthCache(tmp_path, current).task_db(spec)
    evidence, = current.datasets.values()
    assert evidence['params'] == spec.params()
    assert evidence['bytes'] == 8 and evidence['digest'] == spec.digest()


def test_unreadable_source_records_error_without_losing_other_fingerprints(tmp_path, monkeypatch):
    (tmp_path / 'main.py').write_text('example', encoding='utf-8')
    (tmp_path / 'config.py').write_text('locked', encoding='utf-8')
    read_bytes = Path.read_bytes

    def read(path):
        if path.name == 'config.py':
            raise PermissionError('sharing conflict')
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', read)
    evidence = observations.source_identity(tmp_path)
    assert 'main.py' in evidence['source_sha256']
    assert evidence['source_read_errors']['config.py'].startswith('PermissionError:')
