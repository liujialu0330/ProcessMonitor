"""
浸泡判定：读 soak_probe 的 CSV，用与分析页 / 自身监控同一套趋势判定
（core.analysis.trend.judge）判“进程监控助手自己”这次浸泡过没过

判定窗口 = 记录的最后 --hours 小时（缺省 20，即 24 小时浸泡里跳过前 4 小时的预热）。检查项：

1. 记录覆盖：记录总时长不少于窗口的 98%，窗口内有效样本不少于应有数量的 80%；
2. 私有字节：趋势判定不是“持续上涨”，且窗口内增长 ≤ --max-growth-mb（缺省 30 MB）。
   增长取两个口径里较大的：拟合直线在窗口内的增量、窗口末 5% 样本均值 − 首 5% 样本均值
   （后者能抓住阶梯式上涨，前者抓缓慢爬升）；
3. 句柄 / GDI / USER / 线程：趋势判定不是“持续上涨”；判成上涨但拟合增量不超过容差
   （句柄 10、GDI 5、USER 5、线程 3）时只提示、不算失败；数据不足算失败（无法证明平稳）；
4. 采样完成率：soak_tasks.csv 里每个任务最后一次记录的 done / planned 都 ≥ --min-completion
   （缺省 99.5%）；没有任务记录算失败；
5. 工具在浸泡期间没有退出：soak_events.csv 里没有 target_exited 事件（--allow-exit 可放宽，
   用于故意重启工具的演练）。

退出码：0 通过，1 未通过，2 输入错误（目录或 CSV 缺失 / 格式不对）。

用法：
    python tools\\soak_verdict.py C:\\soak\\run1                  （24 小时浸泡，窗口 20 小时）
    python tools\\soak_verdict.py C:\\soak\\run1 --hours 0.2      （15 分钟缩短演练）
"""
import argparse
import csv
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.analysis.trend import Judgement, Verdict, judge  # noqa: E402
from core.self_monitor import fit_input  # noqa: E402

PROBE_FILE = 'soak_probe.csv'
TASKS_FILE = 'soak_tasks.csv'
EVENTS_FILE = 'soak_events.csv'

DEFAULT_HOURS = 20.0
DEFAULT_MAX_GROWTH_MB = 30.0
DEFAULT_MIN_COMPLETION = 0.995
MIN_COVERAGE = 0.98            # 记录总时长 / 窗口
MIN_DENSITY = 0.80             # 有效样本数 / 应有样本数
EDGE_FRACTION = 0.05           # 首尾均值各取窗口两端的这一比例
EXIT_PASS, EXIT_FAIL, EXIT_INPUT = 0, 1, 2

# 计数类指标：(CSV 列, 显示名, 判成上涨但可容忍的拟合增量)
COUNT_METRICS = (
    ('handles', '句柄数', 10.0),
    ('gdi', 'GDI 对象数', 5.0),
    ('user', 'USER 对象数', 5.0),
    ('threads', '线程数', 3.0),
)
REQUIRED_PROBE_COLUMNS = ('unix_ts', 'alive', 'private_kb', 'handles', 'gdi', 'user', 'threads')
REQUIRED_TASK_COLUMNS = ('task_id', 'planned_cycles', 'done_cycles')

VERDICT_TEXT = {
    Verdict.RISING: "持续上涨", Verdict.STABLE: "平稳", Verdict.FLUCTUATING: "波动",
    Verdict.INSUFFICIENT: "数据不足",
}


class InputError(Exception):
    """输入文件缺失或格式不对（退出码 2）。"""


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    note: str = ''          # 通过但值得注意的提示


@dataclass
class SoakReport:
    window_hours: float
    checks: List[Check] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {'passed': self.passed, 'window_hours': self.window_hours,
                'checks': [vars(c) for c in self.checks], 'facts': self.facts}


# ---------- 读 CSV ----------

def _float(text: Optional[str]) -> Optional[float]:
    if text is None or text.strip() == '':
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_csv(path: str, required: Sequence[str]) -> List[Dict[str, str]]:
    if not os.path.isfile(path):
        raise InputError(f"找不到 {path}")
    with open(path, newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise InputError(f"{path} 缺少列：{', '.join(missing)}")
        return list(reader)


@dataclass(frozen=True)
class Sample:
    t: float
    alive: bool
    values: Dict[str, Optional[float]]


def parse_samples(rows: Sequence[Dict[str, str]]) -> List[Sample]:
    samples = []
    for row in rows:
        t = _float(row.get('unix_ts'))
        if t is None:
            continue
        samples.append(Sample(t, row.get('alive', '1').strip() != '0',
                              {c: _float(row.get(c)) for c in
                               ('private_kb', 'handles', 'gdi', 'user', 'threads',
                                'cpu_percent', 'working_set_kb')}))
    samples.sort(key=lambda s: s.t)
    return samples


# ---------- 窗口与判定 ----------

def window_of(samples: Sequence[Sample], hours: float) -> List[Sample]:
    """最后 hours 小时的样本。"""
    if not samples:
        return []
    since = samples[-1].t - hours * 3600.0
    return [s for s in samples if s.t >= since]


def series_of(window: Sequence[Sample], column: str) -> List[Tuple[float, float]]:
    return [(s.t, s.values[column]) for s in window
            if s.alive and s.values.get(column) is not None]


def sample_interval(window: Sequence[Sample]) -> float:
    """窗口内相邻样本间隔的中位数（秒），供趋势判定算跨度门槛；样本太少时按 10 秒。"""
    gaps = [b.t - a.t for a, b in zip(window, window[1:]) if b.t > a.t]
    return statistics.median(gaps) if gaps else 10.0


def judge_series(points: List[Tuple[float, float]], interval_s: float) -> Judgement:
    return judge(fit_input(points), interval_s)


def fit_increase(judgement: Judgement, span_s: float) -> float:
    """拟合直线在整个窗口内的增量；没有拟合（数据不足）为 0。"""
    return judgement.fit.slope_per_second * span_s if judgement.fit is not None else 0.0


def edge_growth(points: Sequence[Tuple[float, float]]) -> float:
    """窗口末 5% 样本均值 − 首 5% 样本均值（至少各取 1 个）。"""
    if len(points) < 2:
        return 0.0
    n = max(1, int(len(points) * EDGE_FRACTION))
    head = statistics.fmean(v for _, v in points[:n])
    tail = statistics.fmean(v for _, v in points[-n:])
    return tail - head


def judgement_text(judgement: Judgement) -> str:
    return f"{VERDICT_TEXT[judgement.verdict]}（{judgement.reason}）"


def check_coverage(samples: Sequence[Sample], window: Sequence[Sample], hours: float,
                   interval_s: float) -> Check:
    total_h = (samples[-1].t - samples[0].t) / 3600.0 if len(samples) > 1 else 0.0
    valid = sum(1 for s in window if s.alive and s.values.get('private_kb') is not None)
    span = window[-1].t - window[0].t if len(window) > 1 else 0.0
    expected = span / interval_s + 1 if interval_s > 0 else 0
    density = valid / expected if expected > 0 else 0.0
    detail = (f"记录 {total_h:.2f} 小时（窗口 {hours:g} 小时），窗口内有效样本 {valid} 个"
              f"（约占应有的 {density:.0%}）")
    ok = total_h >= hours * MIN_COVERAGE and density >= MIN_DENSITY
    if total_h < hours * MIN_COVERAGE:
        detail += f"；记录不足窗口的 {MIN_COVERAGE:.0%}"
    elif density < MIN_DENSITY:
        detail += f"；有效样本低于应有的 {MIN_DENSITY:.0%}"
    return Check("记录覆盖", ok, detail)


def check_private(window: Sequence[Sample], interval_s: float,
                  max_growth_mb: float) -> Tuple[Check, List[str]]:
    points = series_of(window, 'private_kb')
    judgement = judge_series(points, interval_s)
    span = points[-1][0] - points[0][0] if len(points) > 1 else 0.0
    fit_mb = fit_increase(judgement, span) / 1024.0
    edge_mb = edge_growth(points) / 1024.0
    growth_mb = max(fit_mb, edge_mb)
    facts = []
    if points:
        values = [v for _, v in points]
        facts.append(
            f"私有字节：首 {values[0] / 1024:.1f} MB，末 {values[-1] / 1024:.1f} MB，"
            f"最小 {min(values) / 1024:.1f} MB，最大 {max(values) / 1024:.1f} MB，"
            f"拟合斜率 {judgement.fit.slope_per_hour / 1024:+.2f} MB/小时"
            if judgement.fit is not None else
            f"私有字节：首 {values[0] / 1024:.1f} MB，末 {values[-1] / 1024:.1f} MB")
    detail = (f"判定 {judgement_text(judgement)}；窗口内增长 {growth_mb:+.1f} MB"
              f"（拟合 {fit_mb:+.1f}，首尾均值差 {edge_mb:+.1f}；上限 {max_growth_mb:g} MB）")
    ok = (judgement.verdict not in (Verdict.RISING, Verdict.INSUFFICIENT)
          and growth_mb <= max_growth_mb)
    return Check("私有字节", ok, detail), facts


def check_count(window: Sequence[Sample], column: str, title: str, tolerance: float,
                interval_s: float) -> Check:
    points = series_of(window, column)
    judgement = judge_series(points, interval_s)
    span = points[-1][0] - points[0][0] if len(points) > 1 else 0.0
    increase = fit_increase(judgement, span)
    detail = f"判定 {judgement_text(judgement)}；窗口内拟合增量 {increase:+.1f}"
    if judgement.verdict is Verdict.INSUFFICIENT:
        return Check(title, False, detail + "，数据不足无法证明平稳")
    if judgement.verdict is Verdict.RISING:
        if increase > tolerance:
            return Check(title, False, detail + f"，超出容差 {tolerance:g}")
        return Check(title, True, detail, note=f"判成上涨但增量在容差 {tolerance:g} 内，仅提示")
    return Check(title, True, detail)


def task_completion(task_rows: Sequence[Dict[str, str]]) -> Dict[str, Tuple[int, int, int]]:
    """每个任务最后一次记录的 (planned, done, missed)；planned 为空的行跳过。"""
    last: Dict[str, Tuple[float, Tuple[int, int, int]]] = {}
    for row in task_rows:
        t = _float(row.get('unix_ts')) or 0.0
        planned, done = _float(row.get('planned_cycles')), _float(row.get('done_cycles'))
        if planned is None or done is None:
            continue
        missed = _float(row.get('missed_cycles')) or 0.0
        key = row.get('task_id') or '?'
        if key not in last or t >= last[key][0]:
            last[key] = (t, (int(planned), int(done), int(missed)))
    return {key: value for key, (_, value) in last.items()}


def check_completion(task_rows: Sequence[Dict[str, str]], min_completion: float) -> Check:
    per_task = task_completion(task_rows)
    if not per_task:
        return Check("采样完成率", False, "没有任务的采样完成情况记录（探针没读到 status？）")
    parts, ok = [], True
    for task_id, (planned, done, missed) in sorted(per_task.items()):
        ratio = done / planned if planned > 0 else 1.0
        passed = ratio >= min_completion
        ok = ok and passed
        parts.append(f"{task_id[:8]} {ratio:.2%}（{done}/{planned}，缺采 {missed}）"
                     + ("" if passed else " 未达标"))
    total_planned = sum(p for p, _, _ in per_task.values())
    total_done = sum(d for _, d, _ in per_task.values())
    overall = total_done / total_planned if total_planned else 1.0
    return Check("采样完成率", ok,
                 f"合计 {overall:.2%}（门槛 {min_completion:.1%}，逐任务）：" + "；".join(parts))


def check_exits(event_rows: Sequence[Dict[str, str]], allow_exit: bool) -> Check:
    exits = [r for r in event_rows if r.get('event') == 'target_exited']
    if not exits:
        return Check("工具未退出", True, "浸泡期间没有 target_exited 事件")
    stamps = "、".join(r.get('timestamp', '?') for r in exits[:5])
    detail = f"记录到 {len(exits)} 次工具退出（{stamps}）"
    if allow_exit:
        return Check("工具未退出", True, detail, note="已用 --allow-exit 放宽")
    return Check("工具未退出", False, detail)


def cpu_fact(window: Sequence[Sample]) -> Optional[str]:
    values = [s.values['cpu_percent'] for s in window
              if s.alive and s.values.get('cpu_percent') is not None]
    if not values:
        return None
    return f"CPU：均值 {statistics.fmean(values):.2f}%，最大 {max(values):.1f}%（单核百分比）"


def evaluate(samples: Sequence[Sample], task_rows: Sequence[Dict[str, str]],
             event_rows: Sequence[Dict[str, str]], *, hours: float = DEFAULT_HOURS,
             max_growth_mb: float = DEFAULT_MAX_GROWTH_MB,
             min_completion: float = DEFAULT_MIN_COMPLETION,
             allow_exit: bool = False) -> SoakReport:
    """纯函数：样本与 CSV 行 → 判定报告。"""
    report = SoakReport(window_hours=hours)
    if not samples:
        report.checks.append(Check("记录覆盖", False, "探针 CSV 里没有样本"))
        return report
    window = window_of(samples, hours)
    interval_s = sample_interval(window)
    report.checks.append(check_coverage(samples, window, hours, interval_s))
    private, facts = check_private(window, interval_s, max_growth_mb)
    report.checks.append(private)
    report.facts.extend(facts)
    for column, title, tolerance in COUNT_METRICS:
        report.checks.append(check_count(window, column, title, tolerance, interval_s))
    report.checks.append(check_completion(task_rows, min_completion))
    report.checks.append(check_exits(event_rows, allow_exit))
    cpu = cpu_fact(window)
    if cpu:
        report.facts.append(cpu)
    return report


def load_and_evaluate(path: str, **options) -> SoakReport:
    """path 是探针输出目录，或其中的 soak_probe.csv。"""
    directory = os.path.dirname(os.path.abspath(path)) if os.path.isfile(path) else path
    probe_path = path if os.path.isfile(path) else os.path.join(directory, PROBE_FILE)
    samples = parse_samples(read_csv(probe_path, REQUIRED_PROBE_COLUMNS))
    task_path = os.path.join(directory, TASKS_FILE)
    event_path = os.path.join(directory, EVENTS_FILE)
    task_rows = read_csv(task_path, REQUIRED_TASK_COLUMNS) if os.path.isfile(task_path) else []
    event_rows = read_csv(event_path, ('event',)) if os.path.isfile(event_path) else []
    return evaluate(samples, task_rows, event_rows, **options)


# ---------- 输出 ----------

def format_report(report: SoakReport) -> str:
    lines = [f"浸泡判定（窗口：记录的最后 {report.window_hours:g} 小时）"]
    for check in report.checks:
        lines.append(f"  [{'通过' if check.passed else '失败'}] {check.name}：{check.detail}")
        if check.note:
            lines.append(f"         提示：{check.note}")
    for fact in report.facts:
        lines.append(f"  - {fact}")
    failed = [c.name for c in report.checks if not c.passed]
    lines.append("结论：通过" if report.passed else f"结论：未通过（{'、'.join(failed)}）")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='soak_verdict', description="读浸泡探针的 CSV，判进程监控助手自身这次浸泡过没过")
    parser.add_argument('path', help="探针输出目录（或其中的 soak_probe.csv）")
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS,
                        help=f"判定窗口：记录的最后多少小时（缺省 {DEFAULT_HOURS:g}）")
    parser.add_argument('--max-growth-mb', type=float, default=DEFAULT_MAX_GROWTH_MB,
                        help=f"窗口内私有字节增长上限（MB，缺省 {DEFAULT_MAX_GROWTH_MB:g}）")
    parser.add_argument('--min-completion', type=float, default=DEFAULT_MIN_COMPLETION,
                        help=f"每个任务的采样完成率下限（缺省 {DEFAULT_MIN_COMPLETION}）")
    parser.add_argument('--allow-exit', action='store_true',
                        help="允许工具在浸泡期间退出过（故意重启工具的演练用）")
    parser.add_argument('--json', action='store_true', help="输出 JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours 必须大于 0")
    try:
        report = load_and_evaluate(
            args.path, hours=args.hours, max_growth_mb=args.max_growth_mb,
            min_completion=args.min_completion, allow_exit=args.allow_exit)
    except InputError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return EXIT_PASS if report.passed else EXIT_FAIL


if __name__ == '__main__':
    sys.exit(main())
