"""
性能基准套件的 fixture 与预算机制（用法见同目录 README.md）。

- 合成库：session 级生成并缓存在 tests/perf/.cache/，缓存键含生成参数、生成器版本与
  data/db/schema.SCHEMA_VERSION，任一变化自动重建；
- 预算：budgets.json 记录基线机器上的预算（实测 × 1.5）与基线机器的校准值；运行时
  系数 = 本机校准值 / 基线校准值，比较用"预算 × 系数"；
- 记录：每个用例的实测写入 .cache/last_run.json（每次记录即落盘），会话结束在终端
  汇总"实测 / 预算 / 系数"；
- 回填：--perf-update-budgets（或环境变量 PERF_UPDATE_BUDGETS=1）时不做比较，会话
  结束用实测 × 1.5 回写 budgets.json，并把基线校准值换成本机的（未跑到的条目按校准值
  之比同步换算，保持口径一致）。只有显式要求时才写文件。
"""
import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

from data.db.schema import SCHEMA_VERSION
from tests.perf import synth
from tests.perf.calibrate import calibrate_ms

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
# 手动指定机器系数（跳过校准换算），如 CI 上需要临时放宽时使用
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


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def _round(value: float) -> float:
    """预算取 3 位有效数字，便于人读"""
    if value == 0:
        return 0.0
    return float(f"{value:.3g}")


class PerfSession:
    """一次会话的校准、预算与实测记录"""

    def __init__(self, update: bool):
        self.update = update
        self.budgets = load_budgets()
        self.results: Dict[str, dict] = {}
        self._calibration: Optional[float] = None
        self._factor: Optional[float] = None

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
                self._factor = float(override)
            elif baseline:
                self._factor = self.calibration / float(baseline)
            else:
                self._factor = 1.0
        return self._factor

    def budget(self, name: str) -> Optional[float]:
        entry = self.budgets.get('budgets', {}).get(name)
        if entry is None:
            return None
        return float(entry['budget']) * self.factor

    def record(self, name: str, value: float, unit: str = 'ms', desc: str = '',
               samples: Optional[List[float]] = None) -> None:
        self.results[name] = {
            'value': round(value, 3), 'unit': unit, 'desc': desc,
            'samples': [round(s, 3) for s in samples] if samples else None,
            'budget': (round(self.budget(name), 3)
                       if self.budget(name) is not None else None),
        }
        self.write_last_run()

    def write_last_run(self) -> None:
        _write_json(LAST_RUN_PATH, {
            'time': datetime.now().isoformat(timespec='seconds'),
            'python': sys.version.split()[0],
            'platform': platform.platform(),
            'machine': platform.processor(),
            'calibration_ms': round(self.calibration, 2),
            'baseline_calibration_ms': self.budgets.get('calibration_ms'),
            'factor': round(self.factor, 3),
            'results': self.results,
        })

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
        })


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
              samples: Optional[List[float]] = None) -> None:
        """记录实测并与本机预算比较；回填模式下只记录不比较"""
        session = self.session
        session.record(name, value, unit, desc, samples)
        limit = session.budget(name)
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


@pytest.fixture(scope='session')
def perf(perf_session) -> PerfHelper:
    return PerfHelper(perf_session)


# ---------------------------------------------------------------- 合成库缓存


# 本会话新生成的合成库摘要（终端汇总里报告生成耗时与大小）
_GENERATED: Dict[str, dict] = {}


class SynthCache:
    """合成库缓存：文件名含场景名、参数摘要与 SCHEMA_VERSION，旁边的 .json 存生成摘要"""

    def __init__(self, root: Path):
        self.root = root
        self.generated = _GENERATED

    def _get(self, name: str, digest: str, builder: Callable[[str], dict]):
        self.root.mkdir(parents=True, exist_ok=True)
        stem = f"{name}_{digest}_s{SCHEMA_VERSION}"
        db_path = self.root / f"{stem}.db"
        meta_path = self.root / f"{stem}.json"
        if db_path.exists() and meta_path.exists():
            with open(meta_path, encoding='utf-8') as f:
                return str(db_path), json.load(f)
        # 参数或版本变了：删掉同场景的旧缓存再生成；先写临时名，成功后再改名，
        # 中途中断不会留下半成品被当作缓存
        for stale in self.root.glob(f"{name}_*"):
            stale.unlink()
        tmp_path = self.root / f"{stem}.building.db"
        info = builder(str(tmp_path))
        os.replace(tmp_path, db_path)
        info['size_mb'] = round(db_path.stat().st_size / 1e6, 1)
        _write_json(meta_path, info)
        self.generated[name] = info
        print(f"\n[perf] 生成合成库 {name}: {info}")
        return str(db_path), info

    def task_db(self, spec: synth.TaskSpec):
        """(库路径, 生成摘要)：当前版本库，单个合成任务"""
        return self._get(spec.name, spec.digest(),
                         lambda path: synth.build_task_db(path, spec))

    def v1_db(self, spec: synth.LegacySpec):
        """(库路径, 生成摘要)：v1 旧库"""
        return self._get(spec.name, spec.digest(),
                         lambda path: synth.build_v1_db(path, spec))


@pytest.fixture(scope='session')
def synth_cache(tmp_path_factory) -> SynthCache:
    # 生成期间把数据目录指向临时目录：合成库路径都是显式的，这里只是兜底，
    # 保证任何间接解析数据目录的代码都碰不到开发机的 data\
    with pytest.MonkeyPatch.context() as mp:
        import config
        mp.setenv(config.DATA_DIR_ENV, str(tmp_path_factory.mktemp('perf_appdata')))
        yield SynthCache(CACHE_DIR)


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
    if perf_session is None or not perf_session.results:
        return
    if perf_session.update:
        perf_session.write_budgets()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    perf_session = config.stash.get(_SESSION_KEY, None)
    if perf_session is None or not perf_session.results:
        return
    tr = terminalreporter
    tr.section('性能基准（实测 / 预算 / 系数）')
    tr.write_line(f"本机校准 {perf_session.calibration:.1f} ms，基线校准 "
                  f"{perf_session.budgets.get('calibration_ms')} ms，系数 "
                  f"{perf_session.factor:.2f}")
    for name, res in perf_session.results.items():
        limit = res['budget']
        shown = f"{limit:.1f}" if limit is not None else '—'
        tr.write_line(f"  {name:<28s} {res['value']:>10.1f} / {shown:>10s} {res['unit']}"
                      f"  {res['desc']}")
    if perf_session.update:
        tr.write_line(f"已按实测 × {SLACK} 回写 {BUDGETS_PATH}")
    tr.write_line(f"本次实测已写入 {LAST_RUN_PATH}")
    for name, info in _GENERATED.items():
        tr.write_line(f"  本次生成合成库 {name}: {info}")
