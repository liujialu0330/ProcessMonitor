"""
本地并行跑测试（只用标准库，不依赖 pytest-xdist）。

把测试文件按上次的耗时均衡地分成 N 份，每份起一个独立的 pytest 子进程同时跑，最后汇总。
默认集单进程约 3.5 分钟；每个子进程各自有 QApplication、临时目录与数据目录，互不干扰，
16 核机器上 6 份并行约 1 分钟。用法（多余的参数原样传给 pytest）：

    python tests/run_parallel.py                 # 并行跑默认集（排除 perf / soak / slow）
    python tests/run_parallel.py -j 4
    python tests/run_parallel.py -m "not perf and not soak and not gui_real"   # 完整集（含 slow）
    python tests/run_parallel.py -k history

机器很忙（多个测试同时在跑）时，带真实时序的少数用例（线程、子进程、定时器）偶发超时；
判定"有没有偶发失败"以单进程 `python -m pytest tests/` 为准。每个子进程都带用例超时看门狗
（tests/watchdog.py）。耗时记在系统临时目录的 pm_test_shard_weights.json，下次据此分配。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS_PATH = os.path.join(tempfile.gettempdir(), 'pm_test_shard_weights.json')
DEFAULT_JOBS = min(6, max(2, (os.cpu_count() or 4) // 2))   # 16 核机器上是 6 份
NO_TESTS_COLLECTED = 5     # pytest 退出码：这一份里没有选中的用例（例如 -k 只命中别的文件）


def find_test_files() -> List[str]:
    """tests 目录下全部测试文件（相对项目根、正斜杠）"""
    pattern = os.path.join(ROOT, 'tests', '**', 'test_*.py')
    return sorted(os.path.relpath(path, ROOT).replace(os.sep, '/')
                  for path in glob.glob(pattern, recursive=True))


def partition(files: List[str], weights: Dict[str, float], jobs: int) -> List[List[str]]:
    """最长优先的贪心分配：按权重从大到小，每次放进当前总权重最小的一份。
    没有记录的文件按其大小估一个权重（字节数 / 20000 秒，数量级对得上即可）。"""
    def weight(path: str) -> float:
        if path in weights:
            return weights[path]
        try:
            return os.path.getsize(os.path.join(ROOT, path)) / 20000.0
        except OSError:
            return 1.0

    shards: List[List[str]] = [[] for _ in range(max(1, jobs))]
    loads = [0.0] * len(shards)
    for path in sorted(files, key=weight, reverse=True):
        index = loads.index(min(loads))
        shards[index].append(path)
        loads[index] += weight(path) + 0.5      # 0.5 秒：每个文件的导入与收集开销
    return [shard for shard in shards if shard]


def _load_weights() -> Dict[str, float]:
    try:
        with open(WEIGHTS_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_weights(xml_paths: List[str]) -> None:
    """从各份的 junit 结果累计每个文件的耗时，写回权重文件（只覆盖这次跑到的文件）"""
    weights = _load_weights()
    seen: Dict[str, float] = {}
    for path in xml_paths:
        try:
            tree = ET.parse(path)
        except (OSError, ET.ParseError):
            continue
        for case in tree.iter('testcase'):
            classname = case.get('classname') or ''
            module = classname.rsplit('.', 1)[0] if classname.rsplit('.', 1)[-1][:1].isupper() \
                else classname
            key = module.replace('.', '/') + '.py'
            seen[key] = seen.get(key, 0.0) + float(case.get('time') or 0.0)
    weights.update(seen)
    try:
        with open(WEIGHTS_PATH, 'w', encoding='utf-8') as f:
            json.dump(weights, f, ensure_ascii=False)
    except OSError:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="并行跑 pytest（多余的参数原样传给 pytest）")
    parser.add_argument('-j', '--jobs', type=int, default=DEFAULT_JOBS, help=f"并行份数（默认 {DEFAULT_JOBS}，按 CPU 核数的一半、最多 6）")
    args, pytest_args = parser.parse_known_args(argv)

    shards = partition(find_test_files(), _load_weights(), args.jobs)
    workdir = tempfile.mkdtemp(prefix='pm_parallel_')
    started = time.monotonic()
    running = []
    for index, files in enumerate(shards):
        out_path = os.path.join(workdir, f'shard{index}.txt')
        xml_path = os.path.join(workdir, f'shard{index}.xml')
        out = open(out_path, 'w', encoding='utf-8')
        command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                   f'--junitxml={xml_path}', *pytest_args, *files]
        proc = subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
        running.append((index, proc, out, out_path, xml_path, len(files)))

    worst = 0
    for index, proc, out, out_path, _xml, count in running:
        code = proc.wait()
        out.close()
        with open(out_path, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
        summary = next((line for line in reversed(lines)
                        if re.search(r'\d+ (passed|failed|error|skipped|deselected)', line)),
                       f"（无汇总行，退出码 {code}）")
        print(f"[份 {index + 1}/{len(running)}，{count} 个文件] {summary}")
        for line in lines:
            if line.startswith(('FAILED ', 'ERROR ')):
                print("  " + line)
        if code not in (0, NO_TESTS_COLLECTED):
            worst = max(worst, code)
            print(f"  完整输出：{out_path}")
    _save_weights([item[4] for item in running])
    print(f"并行 {len(running)} 份，总耗时 {time.monotonic() - started:.1f} 秒，"
          f"{'全部通过' if worst == 0 else '有失败'}")
    return worst


if __name__ == '__main__':
    sys.exit(main())
