"""
重启续接后的逐段判定、中途崩溃、基线可比性，以及重启 / 续采处的强制断点。

- 判定逐启动段做，整体取最差的一段（rising > insufficient > stable）：只看最后一段会把前面
  段里的泄漏、以及"最后一段太短什么也没测出来"都掩盖成"平稳"；
- 中途异常退出过（崩溃后被续接）整体为 target_exited，结论写次数、最近一次退出码与各段判定；
- 基线不在统计所在的启动段时不写"较基线"，改写"基线在第 N 段，不可比"；
- restart / recover 打点处图表不连线、不算速率，哪怕前后两点间隔不超过空洞阈值；
  rollup / series_gaps 仍与"按样本重算"逐项一致（强制断点只在分析层并入）。
"""
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import core.evidence as evidence
from core import verdict as verdict_rules
from core.analysis.series import connect_mask, merge_gaps, per_second_rates
from core.contracts import StopReason
from core.history_query import (TaskAnalysis, build_request, load_segments, run_history_query,
                                segment_spans)
from core.ipc.commands import compute_verdict
from data.models import DataPoint, Marker, MonitorTask
from data.repos.analytics_repo import AnalyticsRepo
from utils.metrics import MetricType

BASE = datetime(2026, 11, 6, 9, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
IO = MetricType.IO_READ_BYTES
POINTS = 700            # 1 秒一点：点数与跨度都过判定门槛（至少 30 点、10 分钟）
CRASH_LABEL = "异常退出 0xC0000017（内存不足）"

RISING = dict(points=POINTS, slope=10.0, level=100_000.0)
FLAT = dict(points=POINTS, slope=0.0, level=100_000.0)
SHORT = dict(points=5, slope=0.0, level=100_000.0)


def _seed(db, specs, *, gap=5.0, metrics=(MEM,), crashes=(), baseline_at=None,
          io_step=1000.0, interval=1.0):
    """按 specs 依次写若干启动段（段间隔 gap 秒），每段起点前 0.5 秒写 restart 打点。
    crashes：这些段（从 1 起）结束时写"异常退出"打点（在下一个 restart 之前）。
    baseline_at：第几段的开头写基线打点。返回 (task, 各段起点时刻)。"""
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=100, process_name='app.exe',
                       metric_types=list(metrics), interval=interval, start_time=BASE,
                       end_time=None, status='running')
    db.save_task(task)
    points, starts = [], []
    cursor = BASE
    io_total = 0.0
    for number, spec in enumerate(specs, start=1):
        starts.append(cursor)
        if number > 1:
            db.markers.add(task.task_id, cursor - timedelta(milliseconds=500),
                           f"第 {number} 次启动 PID {100 + number}", 'restart')
        if baseline_at == number:
            db.markers.add(task.task_id, cursor, "基线", 'baseline')
        for i in range(spec['points']):
            at = cursor + timedelta(seconds=i)
            if MEM in metrics:
                points.append(DataPoint(task.task_id, at, spec['level'] + spec['slope'] * i, MEM))
            if IO in metrics:
                io_total += io_step
                points.append(DataPoint(task.task_id, at, io_total, IO))
        end = cursor + timedelta(seconds=spec['points'] - 1)
        if number in crashes:
            db.markers.add(task.task_id, end + timedelta(milliseconds=100), CRASH_LABEL, 'crash')
        cursor = end + timedelta(seconds=gap)
    db.save_data_points(points)
    return db.tasks.get(task.task_id), starts


def _analysis(db, task, *, with_segments=True):
    snapshots = {m: run_history_query(db, build_request(task.task_id, m, 'all', analysis=True))
                 for m in task.metric_types}
    segments = (load_segments(db, task.task_id, task.metric_types, snapshots, {})
                if with_segments else [])
    return TaskAnalysis(task.task_id, 'all', snapshots, segments=segments)


# ========== 逐段判定 ==========

def test_last_segment_alone_would_hide_a_leak_in_an_earlier_segment(db):
    task, _ = _seed(db, [RISING, FLAT])
    analysis = _analysis(db, task)

    assert [s.verdict for s in analysis.segments] == ['rising', 'stable']
    assert analysis.segments[0].rising_metrics == (MEM,)
    # 最后一段的快照本身是平稳的……
    assert verdict_rules.metric_verdict(analysis.snapshots[MEM]) == 'stable'
    # ……但整体判定取最差的一段
    assert verdict_rules.overall_verdict(analysis) == 'rising'
    # 对照：没有分段信息时（旧结构）只剩最后一段，泄漏被掩盖
    assert verdict_rules.overall_verdict(_analysis(db, task, with_segments=False)) == 'stable'


def test_leak_in_last_segment_is_rising(db):
    task, _ = _seed(db, [FLAT, RISING])
    analysis = _analysis(db, task)
    assert [s.verdict for s in analysis.segments] == ['stable', 'rising']
    assert verdict_rules.overall_verdict(analysis) == 'rising'


def test_all_segments_stable_is_stable(db):
    task, _ = _seed(db, [FLAT, FLAT, FLAT])
    analysis = _analysis(db, task)
    assert [s.verdict for s in analysis.segments] == ['stable'] * 3
    assert verdict_rules.overall_verdict(analysis) == 'stable'
    assert verdict_rules.segments_summary(analysis).startswith("各段判定：第 1 段 平稳")


def test_too_short_last_segment_is_insufficient_not_stable(db):
    task, _ = _seed(db, [FLAT, SHORT])
    analysis = _analysis(db, task)
    assert [s.verdict for s in analysis.segments] == ['stable', 'insufficient']
    assert verdict_rules.overall_verdict(analysis) == 'insufficient'
    assert "第 2 段 本段数据不足" in verdict_rules.segments_summary(analysis)


def test_last_segment_without_any_sample_is_insufficient_never_not_judged(db):
    """重启打点已写、新进程还没有任何样本：统计范围里没有数据，是"本段数据不足"，
    不是 not_judged（not_judged 会被整体判定当成"没问题"忽略掉）"""
    task, starts = _seed(db, [FLAT])
    db.markers.add(task.task_id, starts[0] + timedelta(seconds=POINTS + 30),
                   "第 2 次启动 PID 200", 'restart')
    analysis = _analysis(db, task)
    snapshot = analysis.snapshots[MEM]

    assert snapshot.points and snapshot.stats is None
    assert verdict_rules.metric_verdict(snapshot) == 'insufficient'
    assert verdict_rules.metric_reason(snapshot) == verdict_rules.REASON_NO_STATS
    assert analysis.segments[-1].verdict == 'insufficient'
    assert verdict_rules.overall_verdict(analysis) == 'insufficient'


def test_cumulative_metric_with_stats_stays_not_judged():
    """累计量有统计但不判定趋势：仍是 not_judged（与"统计范围没数据"区分）"""
    snapshot = SimpleNamespace(judgement=None, points=[object()], stats={'count': 10},
                               value_class='rate')
    assert verdict_rules.metric_verdict(snapshot) == 'not_judged'
    assert verdict_rules.metric_reason(snapshot) is None


def test_single_segment_task_is_unchanged(db):
    task, _ = _seed(db, [RISING])
    analysis = _analysis(db, task)
    assert len(analysis.segments) == 1
    assert verdict_rules.overall_verdict(analysis) == 'rising'
    assert verdict_rules.segments_summary(analysis) == ""
    assert "segments" not in compute_verdict(analysis)


def test_segment_spans_split_at_restart_markers():
    marks = [Marker('t', BASE + timedelta(seconds=100), "重启", kind='restart'),
             Marker('t', BASE + timedelta(seconds=300), "重启", kind='restart'),
             Marker('t', BASE + timedelta(seconds=200), "续采", kind='recover')]
    spans = segment_spans(marks)
    assert [(n, s) for n, s, _e in spans] == [
        (1, None), (2, BASE + timedelta(seconds=100)), (3, BASE + timedelta(seconds=300))]
    assert spans[0][2] == BASE + timedelta(seconds=100) - timedelta(milliseconds=1)
    assert spans[-1][2] is None


# ========== 中途异常退出 ==========

def test_mid_run_crash_makes_the_task_target_exited_even_if_every_segment_is_stable(db):
    task, _ = _seed(db, [FLAT, FLAT], crashes=(1,))
    analysis = _analysis(db, task)

    assert verdict_rules.overall_verdict(analysis) == 'target_exited'
    summary = verdict_rules.segments_summary(analysis)
    assert summary.startswith("中途异常退出 1 次（最近一次 0xC0000017（内存不足））")
    assert "各段判定：第 1 段 平稳；第 2 段 平稳" in summary
    assert verdict_rules.crashes_payload(analysis)[0]['label'] == CRASH_LABEL


def test_mid_run_crash_summary_counts_and_names_the_latest(db):
    task, _ = _seed(db, [RISING, FLAT, FLAT], crashes=(1, 2))
    db.markers.add(task.task_id, BASE + timedelta(seconds=POINTS * 2 + 7),
                   "异常退出 0xC0000005（访问冲突）", 'crash')
    # 上面这条在第 2 段结束前后写入，位于第 2 次与第 3 次重启之间——也是中途退出
    analysis = _analysis(db, task)
    crashes = verdict_rules.crashes_of(analysis)
    assert len(crashes) == 3
    summary = verdict_rules.segments_summary(analysis)
    assert "中途异常退出 3 次（最近一次 0xC0000005（访问冲突））" in summary
    assert "第 1 段 持续上涨（私有字节）" in summary


def test_crash_without_following_restart_is_not_a_mid_run_crash(db):
    """最后一次退出（后面没有重启）由停止原因与停止条件一起判断，不算"中途" """
    task, _ = _seed(db, [FLAT, FLAT], crashes=(2,))
    analysis = _analysis(db, task)
    assert verdict_rules.crashes_of(analysis) == []
    assert verdict_rules.overall_verdict(analysis) == 'stable'


def test_compute_verdict_reports_segments_crashes_and_conclusion(db):
    task, _ = _seed(db, [FLAT, FLAT], crashes=(1,))
    result = compute_verdict(_analysis(db, task), StopReason.USER, stopped_before_condition=False)

    assert result['verdict'] == 'target_exited'
    assert [s['verdict'] for s in result['segments']] == ['stable', 'stable']
    assert result['segments'][1]['start'] is not None and result['segments'][1]['end'] is None
    assert result['mid_run_crashes'][0]['label'] == CRASH_LABEL
    assert result['conclusion'].startswith("中途异常退出 1 次（最近一次 0xC0000017（内存不足））")
    json.dumps(result, ensure_ascii=False)          # 可序列化


def test_compute_verdict_flags_the_rising_segment(db):
    task, _ = _seed(db, [RISING, FLAT])
    result = compute_verdict(_analysis(db, task))
    assert result['verdict'] == 'rising'
    assert result['segments'][0]['rising_names'] == ['私有字节']
    assert result['conclusion'] == "各段判定：第 1 段 持续上涨（私有字节）；第 2 段 平稳"


# ========== 命令行 ==========

def test_pmctl_prints_conclusion_and_maps_target_exited_to_30(db):
    import pmctl
    task, _ = _seed(db, [FLAT, FLAT], crashes=(1,))
    result = compute_verdict(_analysis(db, task))
    out = io.StringIO()
    cli = pmctl.Cli(None, None, out=out)
    cli._print_verdict('app.exe', result)
    text = out.getvalue()
    assert "中途异常退出 1 次（最近一次 0xC0000017（内存不足））" in text
    assert "各段判定：第 1 段 平稳；第 2 段 平稳" in text
    assert pmctl.exit_code_for_verdict(result['verdict']) == 30


def test_junit_records_a_failure_for_a_rising_earlier_segment(db):
    import pmctl
    task, _ = _seed(db, [RISING, FLAT])
    result = compute_verdict(_analysis(db, task))
    result['target'] = 'app.exe'
    suite = pmctl.build_junit([result]).getroot()
    assert int(suite.get('failures')) == 1
    names = [case.get('name') for case in suite.findall('testcase')]
    assert "第 1 段" in names


def test_junit_error_message_carries_the_crash_conclusion(db):
    import pmctl
    task, _ = _seed(db, [FLAT, FLAT], crashes=(1,))
    result = compute_verdict(_analysis(db, task))
    result['target'] = 'app.exe'
    suite = pmctl.build_junit([result]).getroot()
    node = suite.find('testcase').find('error')
    assert "中途异常退出 1 次" in node.get('message')


# ========== 证据包 ==========

def test_evidence_verdict_readme_and_segments(db):
    task, _ = _seed(db, [RISING, FLAT])
    analysis = _analysis(db, task)
    verdict = evidence.analysis_verdict(analysis)
    assert verdict['overall'] == 'rising'
    assert [s['verdict'] for s in verdict['segments']] == ['rising', 'stable']
    assert verdict['conclusion'] == "各段判定：第 1 段 持续上涨（私有字节）；第 2 段 平稳"
    # README 首行列出上涨的指标（最后一段的 metrics 里它是平稳的，名字来自各段）
    assert evidence.overall_line(verdict) == "结论：疑似泄漏——私有字节持续上涨"


def test_evidence_readme_for_mid_run_crash(db):
    task, _ = _seed(db, [FLAT, FLAT], crashes=(1,))
    verdict = evidence.analysis_verdict(_analysis(db, task))
    assert verdict['overall'] == 'target_exited'
    line = evidence.overall_line(verdict)
    assert line.startswith("结论：中途异常退出 1 次（最近一次 0xC0000017（内存不足））")
    assert "各段判定" in line and line.endswith("（以下各指标结论仅供参考）")


def test_evidence_package_computes_segments_when_the_analysis_has_none(db, tmp_path):
    """界面当前显示的分析结果不带分段判定：证据包按库补上，整体判定仍逐段做"""
    task, _ = _seed(db, [RISING, FLAT])
    png, detail, summary = (tmp_path / "r.png", tmp_path / "d.csv", tmp_path / "s.csv")
    for path in (png, detail, summary):
        path.write_bytes(b"x")
    out = tmp_path / "e.zip"
    evidence.build_evidence_package(
        db, task, _analysis(db, task, with_segments=False), report_png=str(png),
        detail_csv=str(detail), summary_csv=str(summary), out_path=str(out), log_path=None,
        clock=SimpleNamespace(now=lambda: BASE))
    with zipfile.ZipFile(out) as archive:
        payload = json.loads(archive.read('task.json').decode('utf-8'))
    assert payload['verdict']['overall'] == 'rising'
    assert [s['verdict'] for s in payload['verdict']['segments']] == ['rising', 'stable']


# ========== 基线可比性 ==========

def test_baseline_in_an_earlier_segment_is_not_comparable(db):
    task, _ = _seed(db, [FLAT, FLAT], baseline_at=1)
    snapshot = _analysis(db, task).snapshots[MEM]
    assert snapshot.segment_number == 2 and snapshot.baseline_segment_number == 1
    assert not snapshot.baseline_comparable
    assert snapshot.baseline_note == "基线在第 1 段，不可比"

    item = evidence.metric_verdict(MEM, snapshot)
    assert item['baseline'] is None and item['start_source'] == evidence.START_FIRST
    assert item['baseline_note'] == "基线在第 1 段，不可比"
    line = evidence.metric_line(item)
    assert "较基线" not in line and "基线在第 1 段，不可比" in line


def test_baseline_in_the_current_segment_is_comparable(db):
    task, _ = _seed(db, [FLAT, FLAT], baseline_at=2)
    snapshot = _analysis(db, task).snapshots[MEM]
    assert snapshot.baseline_comparable and snapshot.baseline_note == ""
    item = evidence.metric_verdict(MEM, snapshot)
    assert item['baseline'] is not None and item['baseline_note'] == ""
    assert "较基线" in evidence.metric_line(item)


def test_compute_verdict_hides_incomparable_baseline(db):
    task, _ = _seed(db, [FLAT, FLAT], baseline_at=1)
    entry = compute_verdict(_analysis(db, task))['metrics'][0]
    assert entry['baseline'] is None
    assert entry['baseline_note'] == "基线在第 1 段，不可比"


def test_no_restart_baseline_is_always_comparable(db):
    task, _ = _seed(db, [FLAT], baseline_at=1)
    snapshot = _analysis(db, task).snapshots[MEM]
    assert snapshot.baseline_comparable and snapshot.baseline_note == ""


# ========== 强制断点 ==========

def test_restart_marker_breaks_the_line_even_without_a_natural_gap(db):
    """前后两点只隔 1 个周期（没有超过空洞阈值），重启打点处仍断开"""
    task, starts = _seed(db, [FLAT, FLAT], gap=1.0)
    snapshot = _analysis(db, task, with_segments=False).snapshots[MEM]
    last_before = starts[1] - timedelta(seconds=1)
    assert db.analytics.gaps(task.task_id, 1.0, metric=MEM) == []      # 表里没有空洞
    assert (last_before, starts[1]) in snapshot.gaps                   # 分析层并入了断点


def test_recover_marker_is_a_forced_break_too(db):
    task, starts = _seed(db, [FLAT], gap=1.0)
    at = starts[0] + timedelta(seconds=100, milliseconds=300)
    db.markers.add(task.task_id, at, "续采", 'recover')
    snapshot = _analysis(db, task, with_segments=False).snapshots[MEM]
    assert (starts[0] + timedelta(seconds=100), starts[0] + timedelta(seconds=101)) in snapshot.gaps


def test_plain_markers_do_not_break_the_line(db):
    task, starts = _seed(db, [FLAT], gap=1.0)
    db.markers.add(task.task_id, starts[0] + timedelta(seconds=100, milliseconds=300), "第 1 轮", 'user')
    assert _analysis(db, task, with_segments=False).snapshots[MEM].gaps == []


def test_no_rate_is_computed_across_a_restart(db):
    """累计量：重启后计数器归零，跨边界的差（负值或巨大跳变）不能当成一段真实速率"""
    task, starts = _seed(db, [FLAT, FLAT], gap=1.0, metrics=(IO,))
    # 把第二段的累计值整体压低：模拟重启后计数器从头计
    with db._factory.connect() as conn:
        conn.execute("UPDATE samples SET value = value - 600000 WHERE ts_ms >= ?",
                     (int(starts[1].timestamp() * 1000),))
    snapshot = run_history_query(db, build_request(task.task_id, IO, 'all', analysis=True))
    boundary = starts[1]
    assert snapshot.rate_series
    assert boundary not in {p.timestamp for p in snapshot.rate_series}
    assert all(p.value >= 0 for p in snapshot.rate_series)


def test_break_pairs_only_reads_samples_and_leaves_gap_tables_consistent(db):
    task, starts = _seed(db, [FLAT, FLAT], gap=1.0)
    pairs = db.analytics.break_pairs(task.task_id, [starts[1] - timedelta(milliseconds=500)])
    assert pairs == [(starts[1] - timedelta(seconds=1), starts[1])]
    # 范围不含两端之一：不算
    assert db.analytics.break_pairs(task.task_id, [starts[1] - timedelta(milliseconds=500)],
                                    since=starts[1]) == []
    # series_gaps 表读出的空洞与按样本重算的逐项相同（强制断点不写任何表）
    raw = AnalyticsRepo(db._factory, path='raw')
    assert db.analytics.gaps(task.task_id, 1.0) == raw.gaps(task.task_id, 1.0) == []


def test_merge_gaps_and_connect_mask():
    t = [BASE + timedelta(seconds=i) for i in range(6)]
    natural = [(t[1], t[2])]
    forced = [(t[3], t[4]), (t[1], t[2])]
    merged = merge_gaps(natural, forced)
    assert merged == [(t[1], t[2]), (t[3], t[4])]
    assert connect_mask(t, merged) == [True, False, True, False, True, True]
    rates = per_second_rates([(x, float(i)) for i, x in enumerate(t)], merged)
    assert [x for x, _ in rates] == [t[1], t[3], t[5]]
