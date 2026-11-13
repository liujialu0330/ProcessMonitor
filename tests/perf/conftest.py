"""
性能基准套件的 fixture 与预算机制（用法见同目录 README.md）。

- 合成库：session 级生成并缓存在 tests/perf/.cache/，缓存键含生成参数、生成器版本与
  data/db/schema.SCHEMA_VERSION，任一变化自动重建；
- 预算：budgets.json 记录基线机器上的预算（实测 × 1.5）与基线机器的校准值；运行时
  系数 = 本机校准值 / 基线校准值，比较用"预算 × 系数"；
- 记录：实测、实际比较阈值和阶段现场写入 .cache/runs/<run_id>/result.json；
  .cache/last_run.json 仅是兼容别名，终端汇总使用同一结果状态；
- 隔离：正式测量和同参数合成库生成分别使用跨进程锁，结果以独立临时文件原子替换；
- 回填：--perf-update-budgets（或环境变量 PERF_UPDATE_BUDGETS=1）时不做比较，会话
  结束用实测 × 1.5 回写 budgets.json，并把基线校准值换成本机的（未跑到的条目按校准值
  之比同步换算，保持口径一致）。只有显式要求时才写文件。
"""
import json
import math
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

from data.db.schema import SCHEMA_VERSION
from tests.perf import synth
from tests.perf.calibrate import calibrate_ms
from tests.perf.artifacts import file_lock, write_json as _write_json
from tests.perf import observations

PERF_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PERF_DIR.parent.parent
CACHE_DIR = PERF_DIR / '.cache'
BUDGETS_PATH = PERF_DIR / 'budgets.json'
LAST_RUN_PATH = CACHE_DIR / 'last_run.json'
# 回填预算时的宽松倍数：阶段 0 固化现状基线，后续阶段再收紧
SLACK = 1.5
# 每个用例的重复次数（取中位数）
REPEAT = 3

UPDATE_OPTION = '--perf-update-budgets'
UPDATE_ENV = 'PERF_UPDATE_BUDGETS'
# 手动指定机器系数（跳过校准换算）；使用时须在报告中保留实际值
FACTOR_ENV = 'PERF_MACHINE_FACTOR'


def pytest_addoption(parser):
    parser.addoption(UPDATE_OPTION, action='store_true', default=False,
                     help='性能基准：不与预算比较，会话结束用实测 × 1.5 回写 tests/perf/budgets.json')


def _update_requested(config) -> bool:
    # 本 conftest 不是根 conftest：从 tests/ 起跑时选项可能未注册，取不到按 False
    return (bool(config.getoption('perf_update_budgets', default=False))
            or os.environ.get(UPDATE_ENV) == '1')


def load_budgets() -> dict:
    if BUDGETS_PATH.exists():
        with open(BUDGETS_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {'calibration_ms': None, 'slack': SLACK, 'budgets': {}}


def _round(value: float) -> float:
    """预算取 3 位有效数字，便于人读"""
    if value == 0:
        return 0.0
    return float(f"{value:.3g}")


class PerfSession:
    """一次会话的校准、预算与实测记录"""

    def __init__(self, update: bool, *, cache_dir=None, budgets=None, metadata=None):
        self.update = update
        self.budgets = load_budgets() if budgets is None else budgets
        self.results: Dict[str, dict] = {}
        self._calibration: Optional[float] = None
        self._factor: Optional[float] = None
        self.cache_dir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
        self.run_id = datetime.now().strftime('%Y%m%dT%H%M%S') + f'-{os.getpid()}-{uuid.uuid4().hex[:12]}'
        self.result_path = self.cache_dir / 'runs' / self.run_id / 'result.json'
        self.started_at = observations.utc_now()
        self.finished_at = None
        self.exit_code = None
        self.metadata = observations.source_identity(PROJECT_ROOT) if metadata is None else metadata
        self.phases = []
        self.datasets = {}
        self._run_lock = None

    def ensure_exclusive(self) -> None:
        """同一缓存目录中的正式套件串行测量，等待发生在计时区间以外。"""
        if self._run_lock is None:
            lock = file_lock(self.cache_dir / '.locks' / 'suite.lock')
            lock.__enter__()
            self._run_lock = lock

    def begin_phase(self, name: str) -> dict:
        self.ensure_exclusive()
        phase = {'test': name, 'before': observations.snapshot(), 'after': None}
        self.phases.append(phase)
        self.write_last_run()
        return phase

    def end_phase(self, phase: dict) -> None:
        phase['after'] = observations.snapshot()
        phase['wall_s'] = phase['after']['wall_s'] - phase['before']['wall_s']
        phase['process_cpu_s'] = phase['after']['process_cpu_s'] - phase['before']['process_cpu_s']
        self.write_last_run()

    def finish(self, exit_code: int) -> None:
        self.finished_at = observations.utc_now()
        self.exit_code = int(exit_code)
        try:
            self.write_last_run()
        finally:
            if self._run_lock is not None:
                lock, self._run_lock = self._run_lock, None
                lock.__exit__(None, None, None)

    @property
    def calibration(self) -> float:
        if self._calibration is None:
            self._calibration = calibrate_ms()
        return self._calibration

    @property
    def factor(self) -> float:
        if self._factor is None:
            override = os.environ.get(FACTOR_ENV)
            baseline = self.budgets.get('calibration_ms')
            if override:
                factor = float(override)
            elif baseline:
                factor = self.calibration / float(baseline)
            else:
                factor = 1.0
            if not math.isfinite(factor) or factor <= 0:
                raise ValueError('性能机器系数必须是有限正数')
            self._factor = factor
        return self._factor

    def budget(self, name: str) -> Optional[float]:
        entry = self.budgets.get('budgets', {}).get(name)
        if entry is None:
            return None
        return float(entry['budget']) * self.factor

    def record(self, name: str, value: float, unit: str = 'ms', desc: str = '',
               samples: Optional[List[float]] = None, *, limit: Optional[float] = None,
               check: bool = False, limit_source: Optional[str] = None) -> None:
        status = ('observed' if not check else 'recorded' if self.update else
                  'missing_limit' if limit is None else 'passed' if value <= limit else 'failed')
        self.results[name] = {
            'value': value, 'unit': unit, 'desc': desc,
            'samples': list(samples) if samples is not None else None,
            # budget保留为兼容字段，但必须与实际比较值一致；历史基线另列。
            'budget': limit if check else None,
            'limit': limit if check else None,
            'limit_source': limit_source if check else None,
            'status': status, 'assertion_enabled': check and not self.update,
            'baseline_budget': self.budget(name),
        }
        self.write_last_run()

    def write_last_run(self) -> None:
        data = {
            'schema_version': 2, 'run_id': self.run_id,
            'time': datetime.now().isoformat(timespec='seconds'),
            'started_at': self.started_at, 'finished_at': self.finished_at,
            'exit_code': self.exit_code,
            'python': sys.version.split()[0],
            'platform': platform.platform(),
            'machine': platform.processor(),
            'calibration_ms': round(self._calibration, 2) if self._calibration is not None else None,
            'baseline_calibration_ms': self.budgets.get('calibration_ms'),
            'factor': self._factor,
            'results': self.results, 'phases': self.phases, 'datasets': self.datasets,
            'metadata': self.metadata,
        }
        _write_json(self.result_path, data)
        # 独立历史记录是权威证据；此别名兼容现有工具，不能作为唯一留档。
        with file_lock(self.cache_dir / '.locks' / 'last-result.lock'):
            _write_json(self.cache_dir / LAST_RUN_PATH.name, data)

    def write_budgets(self) -> None:
        """实测 × SLACK 回写；未测到的条目按新旧校准值之比换算，口径保持一致"""
        old_cal = self.budgets.get('calibration_ms')
        new_cal = round(self.calibration, 2)
        scale = (new_cal / float(old_cal)) if old_cal else 1.0
        entries = dict(self.budgets.get('budgets', {}))
        for name, entry in entries.items():
            if name not in self.results:
                entries[name] = {**entry, 'budget': _round(float(entry['budget']) * scale)}
        for name, res in self.results.items():
            entries[name] = {
                'budget': _round(res['value'] * SLACK),
                'measured': _round(res['value']),
                'unit': res['unit'],
                'desc': res['desc'] or entries.get(name, {}).get('desc', ''),
            }
        _write_json(BUDGETS_PATH, {
            '说明': ('性能预算（tests/perf/README.md）：budget = 基线机器实测 × slack；'
                     '运行时按 本机校准值 / calibration_ms 换算'),
            'calibration_ms': new_cal,
            'slack': SLACK,
            'updated': datetime.now().isoformat(timespec='seconds'),
            'platform': platform.platform(),
            'python': sys.version.split()[0],
            'budgets': dict(sorted(entries.items())),
        }, lock_dir=CACHE_DIR / '.locks')


_SESSION_KEY = pytest.StashKey[PerfSession]()


def _session(config) -> PerfSession:
    session = config.stash.get(_SESSION_KEY, None)
    if session is None:
        session = PerfSession(_update_requested(config))
        config.stash[_SESSION_KEY] = session
    return session


@pytest.fixture(scope='session')
def perf_session(request) -> PerfSession:
    return _session(request.config)


@pytest.fixture(scope='session')
def budget(perf_session) -> Callable[[str], Optional[float]]:
    """budget(name) -> 本机预算（基线预算 × 机器系数）；没有该条预算时为 None"""
    return perf_session.budget


@pytest.fixture(scope='session')
def record(perf_session) -> Callable[..., None]:
    """record(name, value, unit='ms', desc='')：把实测写入 .cache/last_run.json"""
    return perf_session.record


class PerfHelper:
    """用例里用的计时与比较工具"""

    # 每个用例的重复次数（取中位数）
    repeat = REPEAT

    def __init__(self, session: PerfSession):
        self.session = session

    def time_ms(self, fn: Callable[[], object], repeat: Optional[int] = None) -> List[float]:
        """执行 repeat 次（缺省 self.repeat），返回每次耗时（毫秒）"""
        samples = []
        for _ in range(repeat or self.repeat):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1000)
        return samples

    def check(self, name: str, value: float, unit: str = 'ms', desc: str = '',
              samples: Optional[List[float]] = None, *, target: Optional[float] = None) -> None:
        """记录实测并与本机预算比较；回填模式下只记录不比较"""
        session = self.session
        limit = session.budget(name) if target is None else target * session.factor
        session.record(name, value, unit, desc, samples, limit=limit, check=True,
                       limit_source='budgets.json' if target is None else 'scenario_target')
        shown = f"{limit:.1f}" if limit is not None else '—'
        print(f"\n[perf] {name}: 实测 {value:.1f} {unit} / 预算 {shown} {unit} / "
              f"系数 {session.factor:.2f}（{desc}）")
        if session.update:
            return
        if limit is None:
            pytest.fail(f"budgets.json 缺少 {name!r} 的预算；先用 {UPDATE_OPTION} 回填")
        assert value <= limit, (f"{name} 超出预算：实测 {value:.1f} {unit} > "
                                f"预算 {limit:.1f} {unit}（系数 {session.factor:.2f}）")

    def check_median(self, name: str, samples: List[float], unit: str = 'ms',
                     desc: str = '') -> float:
        value = statistics.median(samples)
        self.check(name, value, unit, desc, samples)
        return value

    def check_many(self, entries) -> None:
        """同一组样本的多个分位数均记录并检查，首项失败不会隐藏后项结果。"""
        failures = []
        for name, samples, desc in entries:
            try:
                self.check_median(name, samples, desc=desc)
            except AssertionError as exc:
                failures.append(str(exc))
        assert not failures, '\n'.join(failures)


@pytest.fixture(scope='session')
def perf(perf_session) -> PerfHelper:
    return PerfHelper(perf_session)


# ---------------------------------------------------------------- 合成库缓存


# 本会话新生成的合成库摘要（终端汇总里报告生成耗时与大小）
_GENERATED: Dict[str, dict] = {}


class SynthCache:
    """合成库缓存：文件名含场景名、参数摘要与 SCHEMA_VERSION，旁边的 .json 存生成摘要"""

    def __init__(self, root: Path, session: Optional[PerfSession] = None):
        self.root = root
        self.generated = _GENERATED
        self.session = session

    def _get(self, name: str, digest: str, builder: Callable[[str], dict], *, params=None):
        self.root.mkdir(parents=True, exist_ok=True)
        stem = f"{name}_{digest}_s{SCHEMA_VERSION}"
        db_path = self.root / f"{stem}.db"
        meta_path = self.root / f"{stem}.json"
        with file_lock(self.root / '.locks' / (stem + '.lock')):
            info = None
            if db_path.is_file() and meta_path.is_file():
                try:
                    with meta_path.open(encoding='utf-8') as stream:
                        candidate = json.load(stream)
                    if isinstance(candidate, dict):
                        info = candidate
                except (OSError, ValueError, UnicodeError):
                    pass
            if info is None:
                # 旧digest可能仍被其他运行读取，不按场景前缀删除；仅清本次临时文件。
                tmp_path = self.root / f'{stem}.{uuid.uuid4().hex}.building.db'
                try:
                    info = builder(str(tmp_path))
                    info['size_mb'] = round(tmp_path.stat().st_size / 1e6, 1)
                    os.replace(tmp_path, db_path)
                    _write_json(meta_path, info)
                finally:
                    for suffix in ('', '-wal', '-shm', '-journal'):
                        leftover = Path(str(tmp_path) + suffix)
                        if leftover.exists():
                            leftover.unlink()
                self.generated[name] = info
                print(f"\n[perf] 生成合成库 {name}: {info}")
        if self.session is not None:
            self.session.datasets[stem] = {'digest': digest, 'schema_version': SCHEMA_VERSION,
                                          'bytes': db_path.stat().st_size, 'info': info,
                                          'params': params}
        return str(db_path), info

    def task_db(self, spec: synth.TaskSpec):
        """(库路径, 生成摘要)：当前版本库，单个合成任务"""
        return self._get(spec.name, spec.digest(),
                         lambda path: synth.build_task_db(path, spec), params=spec.params())

    def v1_db(self, spec: synth.LegacySpec):
        """(库路径, 生成摘要)：v1 旧库"""
        return self._get(spec.name, spec.digest(),
                         lambda path: synth.build_v1_db(path, spec), params=spec.params())


@pytest.fixture(scope='session')
def synth_cache(tmp_path_factory, perf_session) -> SynthCache:
    # 生成期间把数据目录指向临时目录：合成库路径都是显式的，这里只是兜底，
    # 保证任何间接解析数据目录的代码都碰不到开发机的 data\
    with pytest.MonkeyPatch.context() as mp:
        import config
        mp.setenv(config.DATA_DIR_ENV, str(tmp_path_factory.mktemp('perf_appdata')))
        yield SynthCache(CACHE_DIR, perf_session)


@pytest.fixture(autouse=True)
def _observe_perf_phase(request):
    if request.node.get_closest_marker('perf') is None:
        yield
        return
    current = _session(request.config)
    phase = current.begin_phase(request.node.nodeid)
    try:
        yield
    finally:
        current.end_phase(phase)


@pytest.fixture(scope='session')
def subprocess_env(tmp_path_factory) -> dict:
    """子进程测量用的环境：离屏、UTF-8、数据目录指向临时目录、项目根在 sys.path 上"""
    import config
    env = dict(os.environ)
    env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    env['PYTHONIOENCODING'] = 'utf-8'
    env[config.DATA_DIR_ENV] = str(tmp_path_factory.mktemp('perf_subprocess'))
    env['PYTHONPATH'] = os.pathsep.join(
        p for p in (str(PROJECT_ROOT), env.get('PYTHONPATH', '')) if p)
    return env


# ---------------------------------------------------------------- 会话收尾


def pytest_sessionfinish(session, exitstatus):
    perf_session = session.config.stash.get(_SESSION_KEY, None)
    if perf_session is None:
        return
    try:
        if perf_session.update and perf_session.results:
            perf_session.write_budgets()
    finally:
        perf_session.finish(exitstatus)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    perf_session = config.stash.get(_SESSION_KEY, None)
    if perf_session is None or not perf_session.results:
        return
    tr = terminalreporter
    tr.section('性能基准（实测 / 预算 / 系数）')
    cal = f'{perf_session._calibration:.1f}' if perf_session._calibration is not None else '未执行'
    tr.write_line(f"本机校准 {cal} ms，基线校准 "
                  f"{perf_session.budgets.get('calibration_ms')} ms，系数 "
                  f"{perf_session.factor:.2f}")
    for name, res in perf_session.results.items():
        limit = res['budget']
        shown = f"{limit:.1f}" if limit is not None else '—'
        tr.write_line(f"  {name:<28s} {res['value']:>10.1f} / {shown:>10s} {res['unit']}"
                      f"  [{res['status']}] {res['desc']}")
    if perf_session.update:
        tr.write_line(f"已按实测 × {SLACK} 回写 {BUDGETS_PATH}")
    tr.write_line(f"本次完整证据：{perf_session.result_path}")
    for name, info in _GENERATED.items():
        tr.write_line(f"  本次生成合成库 {name}: {info}")
