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


def test_import_main_window_cold(perf, subprocess_env):
    """新解释器里 import ui.main_window（含 PyQt5 / qfluentwidgets / pyqtgraph 等全部依赖）"""
    samples = [_run_script(_IMPORT_SCRIPT, subprocess_env)['ms'] for _ in range(perf.repeat)]
    perf.check_median('import_main_window_cold', samples,
                      desc='新进程 import ui.main_window')
