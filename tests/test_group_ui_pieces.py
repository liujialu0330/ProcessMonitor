"""
进程组任务界面的零散部件：

- 存储预估的进程组口径（按成员数估算，每种成员另加一份）；
- 文案函数：目标类型标签、成员摘要与悬停、成员下拉项、成员结论行与折叠区标题、组结论条、
  成员视图结论条、上限占比"—"；
- 任务卡片：目标类型标签、成员摘要行、刷新、超限提示、结束后收起、pid 卡片不变；
- 任务面板按快照建出进程组卡片；
- 进程选择器：按名取值（含补全 .exe）与占位文字；
- 成员曲线加载：load_member_bundle / MemberSnapshotsWorker（成员概况、取消、失败转 error）。
"""
from datetime import datetime, timedelta

import pytest

from core.analysis.trend import Judgement, LinearFit, RateText, Verdict
from core.contracts import StopReason, TaskInfo, TaskStatus
from core.group_aggregator import MemberSnapshot
from core.history_query import HistorySnapshot, TaskAnalysis, build_request
from core.storage_estimate import (
    GROUP_MAX_MEMBERS, GROUP_TYPICAL_MEMBERS, estimate, estimate_tooltip, format_estimate)
from core.workers.member_snapshots_worker import (
    MemberBundle, MemberRequest, MemberSnapshotsWorker, load_member_bundle)
from data.errors import StorageError
from tests.doubles import FakeProcessQuery
from tests.helpers import dispose_widget, process_events_until
from ui.components.task_card import TaskCard
from ui.formatting import (
    GROUP_USAGE_NOTE, MEMBER_BANNER_PREFIX, MEMBER_TOTAL_OPTION, MEMBERS_EMPTY_TEXT,
    MEMBERS_TRUNCATED_TEXT, MemberVerdictRow, VerdictSummary, build_member_rows,
    build_table_row, group_verdict_summary, member_option_text, member_option_tooltip,
    member_rows_header, member_view_summary, members_summary_text, members_tooltip,
    target_identity_text, task_pid_part, task_title_text, verdict_summary)
from ui.widgets.monitor.process_picker import (
    ERROR_GROUP_NEEDS_NAME, ERROR_WAIT_NEEDS_NAME, ProcessPicker, ResolveError)
from ui.widgets.monitor.task_board import TaskBoard
from utils.metrics import MetricType, member_series_key

MEM = MetricType.MEMORY_PRIVATE
GDI = MetricType.NUM_GDI_OBJECTS
COUNT = 'process_count'
T0 = datetime(2026, 11, 6, 9, 0, 0)


# ---------------------------------------------------------------- 存储预估


def test_group_estimate_adds_one_series_set_per_member():
    base = estimate(5, 1)
    group = estimate(5, 1, GROUP_TYPICAL_MEMBERS)
    assert group.rows_per_hour == base.rows_per_hour * (1 + GROUP_TYPICAL_MEMBERS)
    assert group.bytes_12h == base.bytes_12h * (1 + GROUP_TYPICAL_MEMBERS)
    assert estimate(5, 1, 0) == base                                  # 默认口径不变
    assert estimate(0, 1, 3).rows_per_hour == 0


def test_group_estimate_text_says_per_member():
    text = format_estimate(estimate(5, 1, 3), 3)
    assert text == "按 3 种成员估算（每种成员另加一份）：每小时约 72,000 行 · 12 小时约 16 MB"
    assert format_estimate(estimate(5, 1)) == "每小时约 18,000 行 · 12 小时约 4 MB"   # pid 文案不变
    assert format_estimate(estimate(0, 1, 3), 3) == ""


def test_group_tooltip_explains_and_gives_upper_bound():
    tip = estimate_tooltip(5, 1, 3)
    assert "按成员数估算，每种成员另加一份" in tip
    assert f"上限 {GROUP_MAX_MEMBERS} 种" in tip
    assert "成员" not in estimate_tooltip(5, 1)


# ---------------------------------------------------------------- 文案


def test_target_identity_texts():
    assert target_identity_text('pid', 1234, "a.exe") == "PID 1234"
    assert target_identity_text(None, 1234, "a.exe") == "PID 1234"
    assert target_identity_text('tree', 1234, "a.exe") == "进程树 · 根 PID 1234"
    # 进程名已在身份行最前面，同名进程的标签不再重复它
    assert target_identity_text('name', 0, "chrome.exe") == "同名进程"


def _task(kind, pid=1234, name="chrome.exe"):
    return TaskInfo(task_id="t", pid=pid, process_name=name, metric_types=(MEM,), interval=1.0,
                    note="", status=TaskStatus.PENDING, start_time=None, end_time=None,
                    create_time=None, is_paused=False, is_stopping=False, target_kind=kind)


def test_task_pid_part_and_title():
    assert task_pid_part(_task('pid')) == "PID 1234"
    assert task_pid_part(_task('tree')) == "进程树 · 根 PID 1234"
    assert task_pid_part(_task('name', pid=0)) == "同名进程"
    assert task_title_text(_task('pid')) == "chrome.exe（PID 1234）"
    assert task_title_text(_task('tree')) == "chrome.exe（进程树 · 根 PID 1234）"
    assert task_title_text(_task('name', pid=0)) == "chrome.exe（同名进程）"


def _m(key, count, name=None):
    return MemberSnapshot(key, name or key, count)


def test_members_summary_and_tooltip():
    members = [_m('app.exe', 1, "App.exe"), _m('worker.exe', 5), _m('gone.exe', 0)]
    assert members_summary_text(members) == "成员 3 种 · 共 6 个进程"
    assert members_summary_text(members, 'tree') == "成员 3 种 · 共 6 个进程"
    assert members_summary_text([]) == MEMBERS_EMPTY_TEXT
    assert members_tooltip(members).splitlines() == [
        "worker.exe × 5", "App.exe × 1", "gone.exe × 0（当前没有实例）"]


def test_name_group_members_summary_has_no_member_kinds():
    """同名进程永远只有一种成员名：不写"成员 1 种"，只写"共 N 个进程"。"""
    assert members_summary_text([_m('chrome.exe', 37)], 'name') == "共 37 个进程"
    assert members_summary_text([_m('chrome.exe', 0)], 'name') == "共 0 个进程"
    assert members_summary_text([], 'name') == "共 0 个进程"
    assert "成员" not in members_summary_text([_m('chrome.exe', 1)], 'name')
    assert "同名进程" in members_tooltip([_m('chrome.exe', 3)], 'name')


def test_member_option_texts():
    assert member_option_text("chrome.exe", 37) == "chrome.exe（37）"
    assert member_option_text("chrome.exe", None) == "chrome.exe"
    assert "出现过 37 个实例" in member_option_tooltip("chrome.exe", 37)
    assert "出现过" not in member_option_tooltip("chrome.exe", None)
    assert MEMBER_TOTAL_OPTION == "合计（默认）"


def _snap(metric, verdict, *, points=True, rate=None):
    judgement = Judgement(verdict, LinearFit(1.0, 0.0, 0.95, 0.01), 'rising', 0.9)
    stats = {'count': 100, 'min': 1.0, 'max': 9.0, 'avg': 5.0, 'first': 1.0, 'first_ts': T0,
             'last': 9.0, 'last_ts': T0 + timedelta(minutes=20), 'slope_per_hour': None,
             'span_seconds': 1200.0}
    return HistorySnapshot(
        request=build_request("t", metric, 'all'), stats=stats, interval_seconds=1.0,
        judgement=judgement, rate=rate,
        points=[object()] if points else [])


def _analysis(totals, members):
    return TaskAnalysis("t", 'all', dict(totals), member_snapshots=dict(members))


RATE = RateText(1024 * 35.0, 0.0, '/小时', 3600.0)


def test_build_member_rows_orders_rising_first_and_names_metrics():
    analysis = _analysis(
        {MEM: _snap(MEM, Verdict.STABLE)},
        {member_series_key(MEM, 'b.exe'): _snap(MEM, Verdict.STABLE),
         member_series_key(MEM, 'a.exe'): _snap(MEM, Verdict.RISING, rate=RATE),
         member_series_key(COUNT, 'a.exe'): _snap(COUNT, Verdict.STABLE)})
    rows = build_member_rows(analysis, names={'a.exe': "A.exe"}, instances={'a.exe': 3})
    assert [(r.member_key, r.name, r.instances, r.verdict_text) for r in rows] == [
        ('a.exe', "A.exe", 3, "持续上涨"), ('b.exe', 'b.exe', None, "平稳")]
    assert rows[0].rising_text.startswith("私有字节 +")
    assert rows[1].rising_text == "—"
    assert member_rows_header(rows) == "成员结论 · 2 个成员，1 个持续上涨"
    assert member_rows_header(rows[1:]) == "成员结论 · 1 个成员，没有成员持续上涨"


def _rows(*rising):
    return [MemberVerdictRow(k, k, 1, 'rising', "持续上涨", f"私有字节 +{i} MB/小时",
                             (f"私有字节 +{i} MB/小时",)) for i, k in enumerate(rising, 1)]


def test_group_verdict_summary_names_rising_members():
    stable = VerdictSummary("平稳", "2 项指标均平稳", "平稳：私有字节、进程总数")
    rising = VerdictSummary("持续上涨", "1 / 2 项持续上涨：私有字节 +1 MB/小时", "")
    assert group_verdict_summary(stable, []) == stable                # 没有成员上涨：原样
    only = group_verdict_summary(stable, _rows("a.exe", "b.exe"))
    assert only.verdict == "持续上涨"
    assert only.sentence.startswith("合计未见持续上涨，但 2 个成员持续上涨：a.exe（")
    assert "持续上涨的成员：" in only.tooltip and "平稳：私有字节" in only.tooltip
    also = group_verdict_summary(rising, _rows("a.exe"))
    assert also.sentence == rising.sentence + "；成员 a.exe 也在持续上涨"


def test_member_view_summary_prefixes_member_name():
    summary = VerdictSummary("平稳", "2 项指标均平稳", "tip")
    assert member_view_summary("a.exe", summary) == VerdictSummary(
        "平稳", MEMBER_BANNER_PREFIX.format(name="a.exe") + "2 项指标均平稳", "tip")
    empty = VerdictSummary("", "")
    assert member_view_summary("a.exe", empty) == empty


def test_table_row_usage_dash_only_for_limited_metrics():
    snapshot = _snap(GDI, Verdict.STABLE)
    plain = build_table_row(GDI, snapshot)
    assert plain.texts["usage"].endswith("%")
    noted = build_table_row(GDI, snapshot, usage_note=GROUP_USAGE_NOTE)
    assert noted.texts["usage"] == "—" and noted.tooltips["usage"] == GROUP_USAGE_NOTE
    other = build_table_row(MEM, _snap(MEM, Verdict.STABLE), usage_note=GROUP_USAGE_NOTE)
    assert other.texts["usage"] == ""                                 # 没有上限的指标不写占比
    assert build_table_row(GDI, snapshot) == plain                    # 不带说明时与原来逐项相同


# ---------------------------------------------------------------- 任务卡片


@pytest.fixture
def group_card(qapp):
    """进程树卡片：可以有多种成员，成员种类超限时有提示。"""
    widget = TaskCard("t1", "app.exe", 1002, [MEM, COUNT], interval=1.0, target_kind='tree')
    widget.resize(900, 300)
    widget.show()
    yield widget
    dispose_widget(widget)


@pytest.fixture
def name_card(qapp):
    widget = TaskCard("t2", "chrome.exe", 0, [MEM, COUNT], interval=1.0, target_kind='name')
    widget.resize(900, 300)
    widget.show()
    yield widget
    dispose_widget(widget)


def test_group_card_identity_and_empty_members_line(group_card):
    assert group_card.meta_label.text().startswith("· 进程树 · 根 PID 1002")
    assert group_card.is_group and group_card.members_box.isVisible()
    assert group_card.members_text() == MEMBERS_EMPTY_TEXT
    assert not group_card.members_hint_visible()


def test_name_card_identity_does_not_repeat_process_name(name_card):
    assert name_card.meta_label.text() == "· 同名进程 · 每 1 秒"
    assert "chrome.exe" not in name_card.meta_label.text()
    assert name_card.title_label.text() == "chrome.exe"


def test_name_card_members_line_only_counts_processes(name_card):
    assert name_card.members_box.isVisible()
    assert name_card.members_text() == "共 0 个进程"
    name_card.set_members([_m('chrome.exe', 37)], False)
    assert name_card.members_text() == "共 37 个进程"
    name_card.set_members([_m('chrome.exe', 12)], False)
    assert name_card.members_text() == "共 12 个进程"


def test_name_card_never_shows_member_kind_overflow_hint(name_card):
    """同名进程只有一种成员名，就算收到截断标志也不显示"成员种类超过 20"。"""
    name_card.set_members([_m('chrome.exe', 37)], True)
    assert not name_card.members_hint_visible() and not name_card.members_truncated


def test_tree_card_identity_shows_root_pid(qapp):
    card = TaskCard("t1", "app.exe", 4321, [MEM], interval=2.0, target_kind='tree')
    assert card.meta_label.text() == "· 进程树 · 根 PID 4321 · 每 2 秒"
    dispose_widget(card)


def test_group_card_shows_member_summary_and_tooltip(group_card):
    group_card.set_members([_m('app.exe', 1), _m('worker.exe', 2)], False)
    assert group_card.members_text() == "成员 2 种 · 共 3 个进程"
    assert group_card.members_label.toolTip().splitlines() == ["worker.exe × 2", "app.exe × 1"]
    assert not group_card.members_hint_visible()


def test_group_card_refreshes_when_members_change(group_card):
    group_card.set_members([_m('app.exe', 1)], False)
    group_card.set_members([_m('app.exe', 1), _m('gpu.exe', 1)], False)
    assert group_card.members_text() == "成员 2 种 · 共 2 个进程"
    group_card.set_members([_m('app.exe', 4), _m('gpu.exe', 1)], False)    # 只是实例数涨了
    assert group_card.members_text() == "成员 2 种 · 共 5 个进程"


def test_tree_card_truncation_hint(group_card):
    group_card.set_members([_m('app.exe', 1)], True)
    assert group_card.members_hint_visible()
    assert group_card.members_hint_label.text() == MEMBERS_TRUNCATED_TEXT == \
        "成员种类超过 20，超出部分只计入合计"
    group_card.set_members([_m('app.exe', 1)], False)
    assert not group_card.members_hint_visible()


def test_group_card_finished_hides_members_row(group_card):
    group_card.set_members([_m('app.exe', 1)], True)
    group_card.set_finished(StopReason.USER, T0)
    assert not group_card.members_box.isVisible()


def test_pid_card_has_no_members_row_and_ignores_set_members(qapp):
    card = TaskCard("t1", "demo.exe", 1234, [MEM], interval=1.0)
    card.show()
    assert not card.is_group and not card.members_box.isVisible()
    assert card.meta_label.text() == "· PID 1234 · 每 1 秒"
    card.set_members([_m('app.exe', 1)], True)
    assert not card.members_box.isVisible() and not card.members_hint_visible()
    dispose_widget(card)


# ---------------------------------------------------------------- 任务面板


def test_board_builds_group_card_from_snapshot(qapp):
    board = TaskBoard()
    board.resize(900, 600)
    info = TaskInfo(task_id="t1", pid=1002, process_name="app.exe", metric_types=(MEM, COUNT),
                    interval=2.0, note="", status=TaskStatus.PENDING, start_time=None,
                    end_time=None, create_time=None, is_paused=False, is_stopping=False,
                    target_kind='tree', members_truncated=True)
    card = board.create_card(info)
    assert card.target_kind == 'tree' and card.is_group
    assert card.members_truncated
    pid_card = board.create_card(TaskInfo(
        task_id="t2", pid=7, process_name="demo.exe", metric_types=(MEM,), interval=2.0,
        note="", status=TaskStatus.PENDING, start_time=None, end_time=None,
        create_time=None, is_paused=False, is_stopping=False))
    assert not pid_card.is_group
    board.close()


# ---------------------------------------------------------------- 进程选择器


@pytest.fixture
def picker(qapp):
    widget = ProcessPicker(FakeProcessQuery([(1001, "alpha.exe", 1.5), (1002, "Beta.EXE", 2.5)]))
    widget.refresh()
    yield widget
    widget.close()


def test_resolve_name_uses_selected_process_name(picker):
    picker.combo.select_data(next(t for t in picker.targets() if t.pid == 1002))
    assert picker.resolve_name(ERROR_GROUP_NEEDS_NAME) == "Beta.EXE"


def test_resolve_name_accepts_typed_name_without_running_process(picker):
    picker.set_text("chrome.exe")
    assert picker.resolve_name(ERROR_GROUP_NEEDS_NAME) == "chrome.exe"


def test_resolve_name_completes_missing_extension_from_listed_process(picker):
    picker.set_text("beta")
    assert picker.resolve_name(ERROR_GROUP_NEEDS_NAME) == "Beta.EXE"
    picker.set_text("unknown")
    assert picker.resolve_name(ERROR_GROUP_NEEDS_NAME) == "unknown"


@pytest.mark.parametrize("text", ["", "   ", "1002"])
def test_resolve_name_rejects_empty_and_digits_with_given_error_kind(picker, text):
    picker.set_text(text)
    assert picker.resolve_name(ERROR_GROUP_NEEDS_NAME) == ResolveError(
        ERROR_GROUP_NEEDS_NAME, text.strip())
    assert picker.resolve_name().kind == ERROR_WAIT_NEEDS_NAME        # 默认仍是等待进程的错误类型


def test_set_placeholder_overrides_and_restores(picker):
    default = picker.combo.line_edit.placeholderText()
    picker.set_placeholder("输入进程名")
    assert picker.combo.line_edit.placeholderText() == "输入进程名"
    picker.set_placeholder(None)
    assert picker.combo.line_edit.placeholderText() == default


# ---------------------------------------------------------------- 成员曲线加载


def _seed_group(db, started):
    from tests.group_doubles import ROOT_PID, inst
    from tests.test_group_analysis import root
    from tests.test_group_worker import run_group

    def frames(t, n):
        return [root(memory_private=100.0 + n),
                inst('worker.exe', 6000 + n % 2, memory_private=50.0, ppid=ROOT_PID)]
    return run_group(db, started, frames, cycles=20, interval=5.0)


from tests.test_monitor_worker_clock import started  # noqa: E402,F401


def test_load_member_bundle_returns_member_series_and_summaries(db, started):  # noqa: F811
    worker = _seed_group(db, started)
    request = MemberRequest(worker.task_id, (MEM, COUNT), 'all', request_seq=7)
    bundle = load_member_bundle(db, request)
    assert set(bundle.snapshots) == {member_series_key(m, k)
                                     for m in (MEM, COUNT) for k in ('app.exe', 'worker.exe')}
    assert all(s.points for s in bundle.snapshots.values())
    summaries = {s.member_key: s for s in bundle.summaries}
    assert summaries['worker.exe'].instance_count == 2 and summaries['app.exe'].name == "app.exe"
    assert bundle.error is None and bundle.request is request


def test_load_member_bundle_forwards_range_options(db, started):  # noqa: F811
    worker = _seed_group(db, started)
    task = db.tasks.get(worker.task_id)
    since = task.start_time + timedelta(seconds=50)
    request = MemberRequest(worker.task_id, (MEM,), 'custom', since=since,
                            until=since + timedelta(seconds=20), table_limit=5, max_buckets=4)
    snapshot = load_member_bundle(db, request).snapshots[member_series_key(MEM, 'app.exe')]
    assert 0 < len(snapshot.points) <= 5
    assert all(since <= p.timestamp <= since + timedelta(seconds=20) for p in snapshot.points)


def test_load_member_bundle_cancelled_returns_none(db, started):  # noqa: F811
    worker = _seed_group(db, started)
    request = MemberRequest(worker.task_id, (MEM,), 'all')
    assert load_member_bundle(db, request, lambda: True) is None


def test_load_member_bundle_tolerates_summary_failure(db, started, monkeypatch):  # noqa: F811
    worker = _seed_group(db, started)

    def broken(_task_id):
        raise StorageError("坏了")
    monkeypatch.setattr(db.members, 'list_members', broken)
    bundle = load_member_bundle(db, MemberRequest(worker.task_id, (MEM,), 'all'))
    assert bundle.snapshots and bundle.summaries == () and bundle.error is None


def test_load_member_bundle_for_pid_task_is_empty(db):
    from data.models import DataPoint, MonitorTask
    task = MonitorTask(task_id="pid-task", pid=1, process_name="a.exe", metric_types=[MEM],
                       interval=1.0, start_time=T0, end_time=T0, status="stopped")
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, T0, 1.0, MEM)])
    bundle = load_member_bundle(db, MemberRequest(task.task_id, (MEM,), 'all'))
    assert bundle.snapshots == {} and bundle.summaries == ()


def test_member_worker_delivers_bundle_on_result_ready(qapp, db, started):  # noqa: F811
    group = _seed_group(db, started)
    worker = MemberSnapshotsWorker(db, MemberRequest(group.task_id, (MEM,), 'all', request_seq=3))
    received = []
    worker.result_ready.connect(received.append)
    worker.start()
    assert process_events_until(lambda: bool(received), timeout=10)
    worker.wait(3000)
    assert received[0].request.request_seq == 3 and received[0].snapshots


def test_member_worker_reports_failure_as_bundle_error(qapp, db, monkeypatch):
    import core.workers.member_snapshots_worker as module

    def boom(*_args, **_kwargs):
        raise RuntimeError("读库失败")
    monkeypatch.setattr(module, 'load_member_snapshots', boom)
    worker = MemberSnapshotsWorker(db, MemberRequest("x", (MEM,), 'all', request_seq=4))
    received, failed = [], []
    worker.result_ready.connect(received.append)
    worker.failed.connect(failed.append)
    worker.start()
    assert process_events_until(lambda: bool(received), timeout=10)
    worker.wait(3000)
    assert isinstance(received[0], MemberBundle) and received[0].error and not received[0].snapshots
    assert failed


def test_member_request_options_only_include_given_values():
    assert MemberRequest("t", (MEM,), 'all').options() == {}
    since = T0
    assert MemberRequest("t", (MEM,), 'custom', since=since, until=since, table_limit=2,
                         max_buckets=3).options() == {
        'since': since, 'until': since, 'table_limit': 2, 'max_buckets': 3}
