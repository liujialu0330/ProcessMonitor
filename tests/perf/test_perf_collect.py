"""
采集与启动路径：collect_metrics、进程列表冷/热、主窗口模块冷导入。

冷启动类测量在子进程里做（每次一个全新解释器），只计被测调用本身，不含解释器启动；
子进程的数据目录指向临时目录。
"""
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.process_collector import ProcessCollector
from tests.perf.synth import LEAK_METRICS

pytestmark = pytest.mark.perf

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 固定为泄漏排查预设的 5 项（不随界面默认预设变化，保持基线口径）
METRICS = list(LEAK_METRICS)
COLLECT_CALLS = 200
SUBPROCESS_TIMEOUT = 120

_CATALOG_SCRIPT = '''
import json, time
from core import process_catalog
t = time.perf_counter(); n = len(process_catalog.list_processes())
cold = (time.perf_counter() - t) * 1000
t = time.perf_counter(); process_catalog.list_processes()
hot = (time.perf_counter() - t) * 1000
print(json.dumps({"cold": cold, "hot": hot, "count": n}))
'''

_IMPORT_SCRIPT = '''
import json, time
t = time.perf_counter()
import ui.main_window
print(json.dumps({"ms": (time.perf_counter() - t) * 1000}))
'''


def _run_script(script: str, env: dict) -> dict:
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, text=True, encoding='utf-8', timeout=SUBPROCESS_TIMEOUT)
    assert result.returncode == 0, result.stderr
    # 只取最后一行：导入 qfluentwidgets 会先打印一行广告
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_collect_metrics_leak_preset(perf):
    """对本进程采泄漏预设 5 项，每轮 200 次取中位数（微秒），3 轮再取中位数"""
    collector = ProcessCollector(os.getpid())
    assert collector.collect_metrics(METRICS)
    medians = []
    for _ in range(perf.repeat):
        calls = []
        for _ in range(COLLECT_CALLS):
            begin = time.perf_counter()
            collector.collect_metrics(METRICS)
            calls.append((time.perf_counter() - begin) * 1e6)
        medians.append(statistics.median(calls))
    perf.check_median('collect_leak_preset_us', medians, unit='us',
                      desc=f'collect_metrics 泄漏预设 5 项（本进程，{COLLECT_CALLS} 次中位数）')


def test_list_processes_cold_and_hot(perf, subprocess_env):
    """子进程内第一次调用（冷）与紧接着的第二次（热）"""
    runs = [_run_script(_CATALOG_SCRIPT, subprocess_env) for _ in range(perf.repeat)]
    assert all(r['count'] > 0 for r in runs)
    count = runs[-1]['count']
    perf.check_median('list_processes_cold', [r['cold'] for r in runs],
                      desc=f'process_catalog.list_processes 冷（新进程首次，{count} 个进程）')
    perf.check_median('list_processes_hot', [r['hot'] for r in runs],
                      desc=f'process_catalog.list_processes 热（同进程第二次，{count} 个进程）')


# ---------------------------------------------------------------- 快照路径
# 以下条目在 budgets.json 回填前只记录、不按预算比较（PerfSession.record），改为按
# 2.0.0 规划的目标值断言：目标 × 机器系数。

# 5 任务 1 秒周期的采集 CPU 目标（规划 §6）：单次快照 + 5 × 每任务从快照取值
COLLECT_5_TASKS_TARGET_MS_PER_S = 15.0
# 进程列表冷启动目标（规划 §3 A-5）
LIST_COLD_TARGET_MS = 100.0
SNAPSHOT_CALLS = 50
TASKS = 5

_CATALOG_SNAPSHOT_SCRIPT = '''
import json, time
from core import process_catalog
from core.snapshot_hub import SnapshotHub
t = time.perf_counter(); hub = SnapshotHub(); n = len(process_catalog.list_processes(hub))
cold = (time.perf_counter() - t) * 1000
print(json.dumps({"cold": cold, "count": n, "available": hub.available()}))
'''


def _record(perf, name, samples, unit, desc):
    value = statistics.median(samples)
    perf.session.record(name, value, unit, desc, samples)
    print(f"\n[perf] {name}: 实测 {value:.1f} {unit}（{desc}；仅记录，未入预算）")
    return value


def test_collect_snapshot_path(perf):
    """单次全系统快照耗时、每任务从快照取泄漏预设 5 项的耗时，换算 5 任务每秒采集开销"""
    from core.snapshot_hub import SnapshotHub
    from core.system_snapshot import NtProcessSnapshotter

    snapper = NtProcessSnapshotter()
    if not snapper.available():
        pytest.skip(f"全系统快照不可用：{snapper.unavailable_reason}")
    snapshot_ms = _record(
        perf, 'snapshot_once_ms', perf.time_ms(snapper.snapshot, repeat=SNAPSHOT_CALLS), 'ms',
        f'NtQuerySystemInformation 全系统快照（{SNAPSHOT_CALLS} 次中位数，'
        f'{len(snapper.snapshot().processes)} 个进程）')

    # 快照年龄上限放宽到 1 小时：只计"从已有快照取值 + GDI/USER 逐进程调用"
    collector = ProcessCollector(os.getpid(), hub=SnapshotHub(snapper), max_age_s=3600)
    assert collector.collect_metrics(METRICS)
    assert collector.last_source == 'snapshot'
    medians = []
    for _ in range(perf.repeat):
        calls = []
        for _ in range(COLLECT_CALLS):
            begin = time.perf_counter()
            collector.collect_metrics(METRICS)
            calls.append((time.perf_counter() - begin) * 1e6)
        medians.append(statistics.median(calls))
    per_task_us = _record(perf, 'collect_leak_preset_snapshot_us', medians, 'us',
                          f'快照路径 collect_metrics 泄漏预设 5 项（不含取快照，'
                          f'{COLLECT_CALLS} 次中位数）')

    per_second = snapshot_ms + TASKS * per_task_us / 1000
    perf.session.record('collect_5_tasks_ms_per_s', per_second, 'ms',
                        '5 任务 1 秒周期采集开销 = 单次快照 + 5 × 每任务取值')
    limit = COLLECT_5_TASKS_TARGET_MS_PER_S * perf.session.factor
    print(f"\n[perf] collect_5_tasks_ms_per_s: {per_second:.2f} ms/s（目标 {limit:.1f}）")
    assert per_second <= limit


def test_list_processes_cold_snapshot(perf, subprocess_env):
    """新进程里经快照首次列出进程（含快照器自检）"""
    runs = [_run_script(_CATALOG_SNAPSHOT_SCRIPT, subprocess_env) for _ in range(perf.repeat)]
    if not runs[-1]['available']:
        pytest.skip("全系统快照不可用")
    assert all(r['count'] > 0 for r in runs)
    value = _record(perf, 'list_processes_cold_snapshot', [r['cold'] for r in runs], 'ms',
                    f"快照路径 list_processes 冷（新进程首次，含自检，{runs[-1]['count']} 个进程）")
    assert value <= LIST_COLD_TARGET_MS * perf.session.factor


def test_import_main_window_cold(perf, subprocess_env):
    """新解释器里 import ui.main_window（含 PyQt5 / qfluentwidgets / pyqtgraph 等全部依赖）"""
    samples = [_run_script(_IMPORT_SCRIPT, subprocess_env)['ms'] for _ in range(perf.repeat)]
    perf.check_median('import_main_window_cold', samples,
                      desc='新进程 import ui.main_window')
