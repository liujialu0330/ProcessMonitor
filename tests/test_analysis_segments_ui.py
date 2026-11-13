"""
分析页接分段判定：整体判定与 pmctl、证据包同一口径

- 视图模型：多个启动段的任务在后台线程逐段判定（load_segments），合计与成员快照都回来后再
  发布；没有重启的任务不查库；失败沿用上一次的结果；
- 结论条：中途崩溃写"被测进程中途异常退出 N 次（最近一次 访问冲突 0xC0000005）"，胶囊取最严重
  的级别；多段附"共 3 段，第 2 段持续上涨"，悬停逐段列出；
- 摘要表"基线""较基线"遵守 baseline_comparable：不可比写"—"，悬停是 baseline_note；
- metric_reason 的 no_stats_in_range 有界面文案；
- 报告图片 / 复制文本里的整体结论行与结论条同一口径。
"""
import dataclasses
import logging
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import tests.test_segment_verdict as seg
from core import verdict as verdict_rules
from core.analysis.trend import Judgement, LinearFit, Verdict
from core.history_query import (
    HistorySnapshot, SegmentAnalysis, TaskAnalysis, build_request, load_segments)
from core.monitor_manager import MonitorManager
from data.models import Marker
from tests.doubles import FakeClock
from tests.factories import make_analysis_page, make_services
from tests.helpers import SyncHistoryRunner, process_events_until
from tests.test_segment_verdict import BASE, FLAT, MEM, RISING, SHORT, _analysis, _seed
from ui import formatting as fmt
from ui.viewmodels.analysis_vm import (
    AnalysisViewModel, SegmentsBundle, SegmentsWorkerRunner)
from ui.widgets.analysis.report_sheet import build_report_text

ACCESS_VIOLATION_LABEL = "异常退出 0xC0000005（访问冲突）"


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=3)))


@pytest.fixture(autouse=True)
def _access_violation(monkeypatch):
    monkeypatch.setattr(seg, 'CRASH_LABEL', ACCESS_VIOLATION_LABEL)


class RecordingSegmentsRunner:
    """分段判定执行器替身：同步执行并记下请求；hold=True 时只记下，release() 才执行。"""

    def __init__(self, db, hold=False):
        self.db, self.hold = db, hold
        self.requests, self.pending = [], []

    def __call__(self, request, on_done):
        from core.workers.segments_worker import load_segments_bundle
        self.requests.append(request)
        if self.hold:
            self.pending.append((request, on_done))
        else:
            on_done(load_segments_bundle(self.db, request))

    def release(self):
        from core.workers.segments_worker import load_segments_bundle
        pending, self.pending = self.pending, []
        for request, on_done in pending:
            on_done(load_segments_bundle(self.db, request))


def make_vm(db, manager, services, segments_runner=None):
    return AnalysisViewModel(db, manager, services, worker_runner=SyncHistoryRunner(db),
                             segments_runner=segments_runner, defer=lambda fn: fn())


class Recorder:
    def __init__(self, vm):
        self.analyses = []
        vm.analysis_ready.connect(self.analyses.append)


# ================================================================ 视图模型


def test_vm_publishes_segments_consistent_with_pmctl_and_evidence(qapp, db, manager, services):
    """界面的整体判定不再与 pmctl / 证据包不一致：最后一段平稳、更早一段持续上涨，整体是上涨"""
    task, _ = _seed(db, [RISING, FLAT])
    runner = RecordingSegmentsRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()

    analysis = rec.analyses[-1]
    assert [s.verdict for s in analysis.segments] == ['rising', 'stable']
    assert verdict_rules.overall_verdict(analysis) == 'rising'
    # 与 pmctl / 证据包用的 load_segments 逐项一致
    reference = _analysis(db, task)
    assert analysis.segments == reference.segments
    assert verdict_rules.overall_verdict(analysis) == verdict_rules.overall_verdict(reference)
    assert vm.last_analysis is analysis and len(runner.requests) == 1


def test_vm_without_restart_builds_single_segment_without_querying(qapp, db, manager, services):
    seg_task, _ = _seed(db, [FLAT])
    runner = RecordingSegmentsRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    analysis = rec.analyses[-1]
    assert [s.number for s in analysis.segments] == [1]
    assert runner.requests == []


def test_vm_publishes_only_after_segments_come_back(qapp, db, manager, services):
    _seed(db, [RISING, FLAT])
    runner = RecordingSegmentsRunner(db, hold=True)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()

    assert rec.analyses == [] and vm.has_pending_query and vm.is_busy
    runner.release()
    assert len(rec.analyses) == 1 and not vm.has_pending_query and not vm.is_busy
    assert rec.analyses[0].segments


def test_stale_segments_result_is_dropped(qapp, db, manager, services):
    _seed(db, [RISING, FLAT])
    runner = RecordingSegmentsRunner(db, hold=True)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    vm.set_range('1h')              # 作废在途批次，重新发起
    runner.release()                # 第一批的分段结果迟到
    assert len(rec.analyses) == 1 and rec.analyses[0].range_key == '1h'


def test_segments_load_failure_keeps_previous_segments(qapp, db, manager, services, caplog):
    _seed(db, [RISING, FLAT])
    runner = RecordingSegmentsRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    first = rec.analyses[-1].segments
    assert first

    def failing(request, on_done):
        on_done(SegmentsBundle(request, error="读库失败"))

    vm._segments_runner = failing
    with caplog.at_level(logging.WARNING):
        vm.refresh()
    assert rec.analyses[-1].segments == first
    assert any("逐段判定加载失败" in r.getMessage() for r in caplog.records)


def test_custom_range_in_an_earlier_segment_requeries_the_last_segment(
        qapp, db, manager, services):
    """统计落在更早的一段时，这批快照不能当作最后一段的依据：最后一段也重查"""
    task, starts = _seed(db, [RISING, FLAT])
    runner = RecordingSegmentsRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    assert runner.requests[-1].reuse_last is True

    vm.set_custom_range(starts[0] + timedelta(seconds=10), starts[0] + timedelta(seconds=300))
    assert runner.requests[-1].reuse_last is False
    assert [s.verdict for s in rec.analyses[-1].segments] == ['rising', 'stable']


def test_segments_are_computed_on_a_background_thread(qapp, db, manager, services, monkeypatch):
    _seed(db, [RISING, FLAT])
    import core.workers.segments_worker as worker_module
    seen = []
    real = worker_module.load_segments

    def spy(*args, **kwargs):
        seen.append(threading.current_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(worker_module, 'load_segments', spy)
    runner = SegmentsWorkerRunner(db, services.threads)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    assert process_events_until(lambda: rec.analyses, timeout=10)
    assert seen and all(t is not threading.main_thread() for t in seen)
    assert [s.verdict for s in rec.analyses[-1].segments] == ['rising', 'stable']
    process_events_until(lambda: services.threads.entries() == [], timeout=10)


def test_member_view_keeps_segments(qapp, db, manager, services):
    vm = make_vm(db, manager, services)
    snapshot = SimpleNamespace(points=[1])
    segments = [SegmentAnalysis(1, None, None, 'stable')]
    vm.last_analysis = TaskAnalysis(
        't', 'all', {MEM: snapshot}, member_snapshots={f"{MEM}@chrome.exe": snapshot},
        segments=segments)
    vm.current_member = 'chrome.exe'
    assert vm.view_analysis().segments == segments


# ================================================================ 摘要表


def _snap(*, comparable, baseline_only=True, segment_count=2):
    judgement = Judgement(Verdict.STABLE, LinearFit(0.0, 0.0, 0.0, 0.0), 'constant', 0.0)
    stats = {'count': 100, 'min': 1.0, 'max': 9.0, 'avg': 5.0, 'first': 4000.0,
             'first_ts': BASE, 'last': 5000.0, 'last_ts': BASE + timedelta(minutes=20),
             'slope_per_hour': None, 'span_seconds': 1200.0}
    from core.history_query import BaselineInfo
    return HistorySnapshot(
        request=build_request("t", MEM, 'all', baseline_only=baseline_only), stats=stats,
        interval_seconds=1.0, judgement=judgement, points=[object()],
        baseline=BaselineInfo(Marker(1, "t", BASE, "基线", 'baseline'), 1000.0, 5),
        segment_number=2, segment_count=segment_count,
        baseline_segment_number=2 if comparable else 1)


def test_incomparable_baseline_is_dashes_with_the_note_as_tooltip():
    row = fmt.build_summary_row(MEM, _snap(comparable=False))
    assert row.text('baseline') == "—" and row.text('delta') == "—"
    assert row.tooltips['baseline'] == row.tooltips['delta'] == "基线在第 1 段，不可比"
    assert row.text('current') != "—"
    table_row = fmt.build_table_row(MEM, _snap(comparable=False))
    assert table_row.text('baseline') == "—" and table_row.text('delta') == "—"
    assert not fmt._baseline_in_use(_snap(comparable=False))


def test_comparable_baseline_still_drives_the_row():
    snapshot = _snap(comparable=True)
    row = fmt.build_summary_row(MEM, snapshot)
    assert fmt._baseline_in_use(snapshot)
    assert row.text('baseline') != "—" and "+" in row.text('delta')
    assert row.tooltips['baseline'].startswith("基线打点之后")


def test_baseline_only_off_ignores_an_incomparable_baseline():
    """"只统计基线之后"关掉时基线只是参考，不在统计起点上：与段无关，照旧显示统计首值"""
    snapshot = _snap(comparable=False, baseline_only=False)
    row = fmt.build_summary_row(MEM, snapshot)
    assert row.text('baseline') != "—" and row.text('delta') != "—"
    assert not snapshot.baseline_foreign


def test_baseline_less_snapshot_is_never_foreign():
    snapshot = dataclasses.replace(_snap(comparable=False), baseline=None)
    assert snapshot.baseline_comparable and not snapshot.baseline_foreign
    assert fmt.build_summary_row(MEM, snapshot).text('baseline') != "—"


def test_incomparable_baseline_does_not_feed_sorting_or_overlay_reference():
    from ui.viewmodels.history_vm import relative_rate
    from ui.widgets.analysis.trend_panel import overlay_reference
    snapshot = dataclasses.replace(
        _snap(comparable=False),
        judgement=Judgement(Verdict.RISING, LinearFit(1.0 / 3600.0, 0.0, 0.9, 0.0), 'rising', 0.9),
        band=[], buckets=[SimpleNamespace(timestamp=BASE, value=4321.0)])
    # 参考值取统计首值（4000），而不是另一个进程里的基线（1000）
    assert relative_rate(snapshot) == pytest.approx(1.0 / 4000.0)
    assert overlay_reference(snapshot) == 4321.0
    comparable = dataclasses.replace(snapshot, baseline_segment_number=2)
    assert relative_rate(comparable) == pytest.approx(1.0 / 1000.0)
    assert overlay_reference(comparable) == 1000.0


# ================================================================ no_stats_in_range 文案


def test_no_stats_in_range_has_its_own_text(db):
    task, starts = _seed(db, [FLAT])
    db.markers.add(task.task_id, starts[0] + timedelta(seconds=seg.POINTS + 30),
                   "第 2 次启动 PID 200", 'restart')
    snapshot = _analysis(db, task).snapshots[MEM]
    assert verdict_rules.metric_reason(snapshot) == verdict_rules.REASON_NO_STATS
    assert fmt.metric_reason_text(snapshot) == "所选范围内没有可统计的数据"
    row = fmt.build_table_row(MEM, snapshot)
    assert row.note == "所选范围内没有可统计的数据" and row.dimmed
    # 与"整个范围都没有数据"区分
    assert fmt.metric_reason_text(None) == "所选范围内没有该指标的数据"
    empty = HistorySnapshot(request=build_request("t", MEM, 'all'))
    assert fmt.metric_reason_text(empty) == "所选范围内没有该指标的数据"
    assert fmt.build_table_row(MEM, empty).note == "所选范围内没有该指标的数据"
    # 结论条：全部数据不足时的主句同样区分
    banner = fmt.verdict_summary([(MEM, snapshot)])
    assert banner.verdict == "数据不足" and banner.sentence == "数据不足：所选范围内没有可统计的数据"
    assert fmt.verdict_summary([(MEM, None)]).sentence == "数据不足：所选范围内没有数据"


# ================================================================ 结论条


def test_banner_for_mid_run_crash_and_segments(db):
    task, _ = _seed(db, [RISING, FLAT, FLAT], crashes=(1,))
    analysis = _analysis(db, task)
    base = fmt.verdict_summary([(MEM, analysis.snapshots[MEM])])
    assert base.verdict == "平稳"
    overall = verdict_rules.overall_verdict(analysis)
    assert overall == 'target_exited'

    summary = fmt.overall_summary(base, analysis, overall)
    assert summary.verdict == "异常退出"
    assert summary.sentence.startswith(
        "被测进程中途异常退出 1 次（最近一次 访问冲突 0xC0000005）；共 3 段，第 1 段持续上涨")
    assert summary.sentence.endswith("最后一段：" + base.sentence)
    lines = summary.tooltip.split("\n")
    assert "第 1 段 持续上涨（私有字节）" in lines
    assert "第 2 段 平稳" in lines and "第 3 段 平稳" in lines
    assert any("中途异常退出：访问冲突 0xC0000005" in line for line in lines)
    # 纯文本摘要（命令行 / 证据包 README 用）仍是 core 的写法
    assert verdict_rules.segments_summary(analysis).split("；")[0] == (
        "中途异常退出 1 次（最近一次 0xC0000005（访问冲突））")


def test_banner_pill_takes_the_worst_segment_without_a_crash(db):
    task, _ = _seed(db, [RISING, FLAT])
    analysis = _analysis(db, task)
    base = fmt.verdict_summary([(MEM, analysis.snapshots[MEM])])
    assert base.verdict == "平稳"                       # 当前段本身平稳
    summary = fmt.overall_summary(base, analysis, verdict_rules.overall_verdict(analysis))
    assert summary.verdict == "持续上涨"
    assert summary.sentence.startswith("共 2 段，第 1 段持续上涨；最后一段：")

    short_task, _ = _seed(db, [FLAT, SHORT])
    short = _analysis(db, short_task)
    base = fmt.verdict_summary([(MEM, short.snapshots[MEM])])
    summary = fmt.overall_summary(base, short, verdict_rules.overall_verdict(short))
    assert summary.verdict == "数据不足" and "第 2 段数据不足" in summary.sentence


def test_banner_is_untouched_for_a_task_without_restarts(db):
    task, _ = _seed(db, [FLAT])
    analysis = _analysis(db, task)
    base = fmt.verdict_summary([(MEM, analysis.snapshots[MEM])])
    assert fmt.overall_summary(base, analysis,
                               verdict_rules.overall_verdict(analysis)) is base


def test_segment_digest_wording():
    def segment(number, verdict):
        return SimpleNamespace(number=number, verdict=verdict)

    assert fmt.segment_digest([segment(1, 'stable'), segment(2, 'rising'), segment(3, 'stable')]) \
        == "共 3 段，第 2 段持续上涨"
    assert fmt.segment_digest([segment(1, 'rising'), segment(2, 'rising')]) \
        == "共 2 段，第 1、2 段持续上涨"
    assert fmt.segment_digest([segment(1, 'stable'), segment(2, 'insufficient')]) \
        == "共 2 段，第 2 段数据不足"
    assert fmt.segment_digest([segment(1, 'stable'), segment(2, 'stable')]) \
        == "共 2 段，各段均平稳"
    many = [segment(i, 'rising') for i in range(1, 6)]
    assert fmt.segment_digest(many) == "共 5 段，5 段持续上涨"


def test_crash_label_text():
    marker = SimpleNamespace(label="异常退出 0xC0000005（访问冲突）")
    assert fmt.crash_label_text(marker) == "访问冲突 0xC0000005"
    assert fmt.crash_label_text(SimpleNamespace(label="异常退出 0xC1234567（x）")) \
        == "未知异常 0xC1234567"
    assert fmt.crash_label_text(SimpleNamespace(label="异常退出")) == ""


def test_early_exit_without_crash_never_reads_stable(db):
    task, _ = _seed(db, [FLAT])
    analysis = _analysis(db, task)
    base = fmt.verdict_summary([(MEM, analysis.snapshots[MEM])])
    summary = fmt.overall_summary(base, analysis, 'target_exited')
    assert summary.verdict == "提前退出"
    assert summary.sentence.startswith("被测进程在停止条件达成之前退出，测试未完成；")

    rising = _analysis(db, _seed(db, [RISING])[0])
    base = fmt.verdict_summary([(MEM, rising.snapshots[MEM])])
    assert fmt.overall_summary(base, rising, 'target_exited').verdict == "持续上涨"


def test_task_overall_verdict_follows_core(db):
    task, _ = _seed(db, [RISING, FLAT])
    analysis = _analysis(db, task)
    assert fmt.task_overall_verdict(task, analysis) == 'rising'
    # 记录里的停止原因是被测进程退出、且没有停止条件：与 pmctl 一样是 target_exited
    exited = dataclasses.replace(task, stop_reason='process_exited', end_time=BASE + timedelta(
        seconds=2000))
    assert fmt.task_overall_verdict(exited, analysis) == 'target_exited'
    user = dataclasses.replace(task, stop_reason='user')
    assert fmt.task_overall_verdict(user, analysis) == 'rising'


# ================================================================ 分析页


@pytest.fixture
def make_page(qapp, db, manager, services):
    pages = []

    def build():
        page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                                  defer=lambda fn: fn())
        page.resize(1280, 1200)
        pages.append(page)
        return page

    yield build
    for page in pages:
        page.auto_refresh.stop()
        process_events_until(lambda: services.threads.entries() == [], timeout=10)
        page.close()


def test_page_banner_and_table_for_a_crashed_multi_segment_task(make_page, db):
    task, _ = _seed(db, [RISING, FLAT], crashes=(1,), baseline_at=1)
    page = make_page()
    page.show()
    page.select_task(task.task_id)

    banner = page.verdict_banner
    assert banner.verdict() == "异常退出"
    assert banner.sentence().startswith(
        "被测进程中途异常退出 1 次（最近一次 访问冲突 0xC0000005）；共 2 段，第 1 段持续上涨")
    assert "第 1 段 持续上涨（私有字节）" in banner.sentence_label.toolTip()
    assert banner.pill.toolTip() == banner.sentence_label.toolTip()
    row = page.summary_table.rows()[0]
    assert row.text('baseline') == "—" and row.text('delta') == "—"
    assert row.tooltips['delta'] == "基线在第 1 段，不可比"
    assert page.vm.last_analysis.segments


def test_report_text_has_the_same_overall_line(make_page, db, services):
    task, _ = _seed(db, [RISING, FLAT], crashes=(1,), baseline_at=1)
    page = make_page()
    page.select_task(task.task_id)
    text = build_report_text(page.vm.current_task, page.vm.last_analysis, "全部", BASE)
    assert text.overall_line == (
        "整体判定：异常退出。" + page.verdict_banner.sentence())
    assert text.conclusions[0] == text.overall_line
    assert text.conclusion_rows[0].note == "基线在第 1 段，不可比"
    assert text.conclusion_rows[0].cells[0] == "—" and text.conclusion_rows[0].cells[2] == "—"


def test_report_has_no_overall_line_for_a_plain_task(make_page, db):
    task, _ = _seed(db, [FLAT])
    page = make_page()
    page.select_task(task.task_id)
    text = build_report_text(page.vm.current_task, page.vm.last_analysis, "全部", BASE)
    assert text.overall_line == "" and not text.conclusions[0].startswith("整体判定")


def test_report_image_renders_with_the_overall_line(make_page, db):
    task, _ = _seed(db, [RISING, FLAT], crashes=(1,), baseline_at=1)
    page = make_page()
    page.select_task(task.task_id)
    image = page.render_report()
    assert not image.isNull() and image.width() == 1600
