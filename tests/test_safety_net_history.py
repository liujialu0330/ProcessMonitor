"""
安全网（特性测试）：分析页查询编排与选择语义（经 AnalysisViewModel 锁住，不依赖页面控件）。

锁住的行为（任何重构下的漂移都应让这里变红）：
1. 用户操作的加载（点"刷新"）与自动刷新（定时到点）对同一任务、同一焦点指标、同一
   时间范围——两条路径都经视图模型、都用生产装配的后台线程（每个指标一个查询）：
   - 发给仓储的查询集合（since / until / limit / 分桶上限）完全相同；
   - 合并后的 TaskAnalysis 中每个指标的快照（明细点、统计、分桶、min/max 带、打点、
     空洞、分轮、打点区间、基线、锚点）逐项相等；
   - 由快照生成的判定摘要表各行（基线/当前/较基线/每轮均增/增速/末段/判定）与依据行
     完全相同，并把具体数值钉死；
   - 焦点指标在范围内无数据时两条路径给出同一空状态文案。
2. 选择语义：select_task 显式选中后不被新任务切走；自动选中时新任务出现会一路
   跟到最新；自动选中的任务被删除时回落到最新任务（而用户显式选中的任务消失时清空）。
   选择统一由视图模型决定，页面控件只跟随（连线见 tests/test_analysis_page_flow.py）。

不依赖真实时间流逝：注入固定时钟（FakeClock）；不依赖机器进程列表：
"是否运行中"由注入的任务状态替身给出；数据库为 conftest 的临时库。
"""
from collections import Counter
from datetime import timedelta

import pytest
from PyQt5.QtWidgets import QApplication

from core.monitor_manager import MonitorManager
from data.models import DataPoint
from tests.doubles import FakeClock, FakeTaskStatus
from tests.factories import make_services
from tests.helpers import SyncHistoryRunner, poll_until
from tests.history_dataset import (
    BASE, MAX_BUCKETS, MEM, NOW, TABLE_LIMIT, THREADS, _make_task, _seed_baseline_dataset,
    _seed_query_dataset)
from ui.formatting import SUMMARY_COLUMNS, basis_text, build_summary_row
from ui.viewmodels.analysis_vm import AnalysisViewModel

# 快照里逐项比较的字段（两条路径必须一致）
SNAPSHOT_FIELDS = ('points', 'stats', 'buckets', 'band', 'markers', 'judgement', 'rate',
                   'gaps', 'rounds', 'baseline', 'total_count', 'stats_since', 'intervals',
                   'task_last_at')


def _pump():
    QApplication.processEvents()


def _wait(condition, timeout=10.0):
    def check():
        _pump()
        return condition()
    return poll_until(check, timeout=timeout)


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    # 所选任务视为运行中：定时刷新会发起后台查询
    return make_services(clock=FakeClock(NOW), task_status=FakeTaskStatus(all_running=True))


@pytest.fixture
def vm(qapp, db, manager, services):
    """生产装配的查询路径（每个请求一个后台线程、加载推迟到下一轮事件循环），
    只缩小表格上限与分桶上限。"""
    model = AnalysisViewModel(db, manager, services,
                              table_limit=TABLE_LIMIT, max_buckets=MAX_BUCKETS)
    yield model
    model.cancel_pending()
    _wait(lambda: services.threads.entries() == [])


def _idle(vm):
    """等视图模型没有在途/排队的查询（结果已在主线程应用完毕）。"""
    assert _wait(lambda: not vm.has_pending_query), "查询未在期限内返回"
    _pump()


def _record_db_queries(db, monkeypatch):
    """包一层仓储只读查询，记录 (查询名, 参数, 关键字参数)；工作线程里的调用同样被
    记录。各指标的查询并行进行、次序不定，比较时用多重集合。"""
    calls = []

    def wrap(repo, name, label):
        real = getattr(repo, name)

        def spy(*args, **kwargs):
            if label == 'table':
                query = args[0]
                key = (query.metric, query.since, query.until, query.limit)
            else:
                key = args
            calls.append((label, key, tuple(sorted(kwargs.items()))))
            return real(*args, **kwargs)
        monkeypatch.setattr(repo, name, spy)

    wrap(db.points, 'fetch', 'table')
    for name in ('aggregates', 'value_near', 'gaps', 'buckets', 'legacy_buckets'):
        wrap(db.analytics, name, name)
    wrap(db.markers, 'list', 'markers')
    return calls


def _record(vm):
    """记录视图模型交给页面的快照与空状态（两条路径都汇入这两个信号）。"""
    snapshots, empties = [], []
    vm.snapshot_ready.connect(snapshots.append)
    vm.empty_state.connect(lambda title, hint: empties.append((title, hint)))
    return snapshots, empties


def _summary(vm):
    """页面判定摘要表看到的内容：每个指标一行文本 + 是否置灰，以及依据行。"""
    analysis = vm.last_analysis
    if analysis is None:
        return None
    rows, basis = [], ""
    for metric, snapshot in analysis.snapshots.items():
        row = build_summary_row(metric, snapshot if snapshot.points else None)
        rows.append(([row.text(c) for c in SUMMARY_COLUMNS], row.dimmed))
        if not basis and snapshot.stats:
            basis = basis_text(snapshot)
    return rows, basis


def _prepare(vm, task, metric, range_key):
    """模拟用户：载入列表、选中任务、切到指定焦点指标与时间范围。"""
    vm.reload_tasks()
    _idle(vm)
    vm.select_task(task.task_id)
    _idle(vm)
    vm.select_metric(metric)
    _idle(vm)
    vm.set_range(range_key)
    _idle(vm)
    assert (vm.current_metric, vm.range_key) == (metric, range_key)


def _manual_load(vm):
    """用户点"刷新"：整体重载并以用户操作口径加载（auto_refresh=False）。"""
    vm.refresh()
    _idle(vm)


def _auto_refresh(vm):
    """定时刷新到点：运行中任务发起后台查询（auto_refresh=True）。"""
    vm.on_tick()
    assert vm.has_pending_query, "运行中任务的定时刷新应发起后台查询"
    _idle(vm)


def _assert_same_analysis(manual, auto):
    assert manual.task_id == auto.task_id and list(manual.snapshots) == list(auto.snapshots)
    for metric in manual.snapshots:
        left, right = manual.snapshots[metric], auto.snapshots[metric]
        assert left.request.since == right.request.since, metric
        assert left.request.anchor == right.request.anchor, metric
        for key in SNAPSHOT_FIELDS:
            assert getattr(left, key) == getattr(right, key), f"{metric}.{key} 两条路径不一致"


_LAST = BASE + timedelta(minutes=120)
# 摘要表各行：(各列文本, 是否置灰)，指标顺序为任务的指标顺序（私有字节、线程数）。
# 增速：跨度 < 1 小时按 /分、否则 /小时，± 为 95% 置信区间；跨度不足 30 分钟
# （max(10 分钟, 30 × 周期 60 秒)）或点数 < 30 时置灰"数据不足"；两个用户打点围出
# 一轮：30 分钟后 5 点中位数 116 MB → 115 分钟后 158.5 MB，每轮 +42.5 MB。
MEM_ALL_ROW = (['私有字节', '150 MB', '160 MB', '+10 MB (+6.7%)', '+42.5 MB',
                '+29.9 ±3.83 MB/小时 · R² 0.66', '↑ +2.5 MB', '波动', ''], False)
THREADS_ALL_ROW = (['线程数', '20 个', '24 个', '+4 个 (+20.0%)', '--',
                    '+0.3 ±1.72 个/小时 · R² 0.00', '↑ +2 个', '平稳', ''], False)
MEM_1H_ROW = (['私有字节', '130 MB', '160 MB', '+30 MB (+23.1%)', '--',
               '+32.2 ±10.7 MB/小时 · R² 0.37', '↑ +2.5 MB', '波动', ''], False)
# 线程数最后一点在 60 分钟；1 小时范围（since 60 分钟）只含这一点，增速无法计算
THREADS_1H_ROW = (['线程数', '24 个', '24 个', '±0', '--', '数据不足（已采 0秒）', '--',
                   '数据不足', ''], True)
# (焦点指标, 范围, 期望 since 相对 BASE 的分钟数（None=不过滤）, 焦点明细行数,
#  焦点统计 count, 焦点分桶点数, 摘要表各行, 依据行)
# 分桶：61 点以上抽稀为 20 桶 × (MIN, MAX) = 40 点；打点恒为 2（快照带任务全部打点）。
QUERY_CASES = [
    (MEM, 'all', None, 50, 121, 40, [MEM_ALL_ROW, THREADS_ALL_ROW], "2小时0分 · 121 点"),
    # 10 分钟：锚点 120 分钟 → since 110 分钟（含端点）共 11 点，点数少于桶数不抽稀；
    # 线程数在该范围内无数据，整行置灰
    (MEM, '10m', 110, 11, 11, 11,
     [(['私有字节', '155 MB', '160 MB', '+5 MB (+3.2%)', '--', '数据不足（已采 10分钟）',
        '↑ +2.5 MB', '数据不足', ''], True),
      (['线程数', '--', '--', '--', '--', '--', '--', '--', ''], True)],
     "10分钟 · 11 点"),
    (MEM, '1h', 60, 50, 61, 40, [MEM_1H_ROW, THREADS_1H_ROW], "1小时0分 · 61 点"),
    (THREADS, 'all', None, 50, 61, 40, [MEM_ALL_ROW, THREADS_ALL_ROW], "2小时0分 · 121 点"),
    (THREADS, '1h', 60, 1, 1, 1, [MEM_1H_ROW, THREADS_1H_ROW], "1小时0分 · 61 点"),
]


@pytest.mark.parametrize(
    "metric, range_key, since_minutes, rows, count, buckets, summary_rows, basis",
    QUERY_CASES, ids=[f"{m}-{r}" for m, r, *_ in QUERY_CASES])
def test_manual_load_and_auto_refresh_are_identical(
        vm, db, monkeypatch, metric, range_key, since_minutes, rows, count, buckets,
        summary_rows, basis):
    """
    用户操作的加载与自动刷新的查询参数、查询结果与摘要表逐项相同；同时把具体数值
    钉死，任何一边漂移都会红。
    """
    task = _seed_query_dataset(db)
    _prepare(vm, task, metric, range_key)

    calls = _record_db_queries(db, monkeypatch)
    snapshots, empties = _record(vm)

    # ---- 路径一：用户操作的加载 ----
    _manual_load(vm)
    manual_calls = Counter(calls)
    assert len(snapshots) == 1 and snapshots[0].request.auto_refresh is False
    manual, manual_analysis = snapshots[0], vm.last_analysis
    manual_summary = _summary(vm)

    # ---- 路径二：自动刷新 ----
    calls.clear()
    _auto_refresh(vm)
    auto_calls = Counter(calls)
    assert len(snapshots) == 2 and snapshots[1].request.auto_refresh is True
    auto, auto_analysis = snapshots[1], vm.last_analysis
    assert empties == []

    # 1) 查询参数：同一批查询（每个指标一组），明细 since / limit 与分桶上限钉死
    expected_since = (None if since_minutes is None
                      else BASE + timedelta(minutes=since_minutes))
    assert manual_calls == auto_calls
    tables = sorted(key for label, key, _kw in manual_calls if label == 'table')
    assert tables == sorted((m, expected_since, None, TABLE_LIMIT) for m in (MEM, THREADS))
    # 分桶查询的位置参数为 (task_id, 指标, 分桶上限)
    bucket_limits = [key[2] for label, key, _kw in manual_calls
                     if label in ('buckets', 'legacy_buckets')]
    assert bucket_limits and set(bucket_limits) == {MAX_BUCKETS}
    assert manual.request.since == auto.request.since == expected_since

    # 2) 查询结果：每个指标的快照逐项相等（DataPoint/Marker 是 dataclass，按值比较）
    _assert_same_analysis(manual_analysis, auto_analysis)
    assert manual.request.metric == auto.request.metric == metric
    assert len(manual.points) == rows
    assert manual.points[-1].timestamp == (
        BASE + timedelta(minutes=120 if metric == MEM else 60))
    assert manual.stats['count'] == count
    assert len(manual.buckets) == buckets
    assert [m.label for m in manual.markers] == ["第1轮", "第2轮"]

    # 3) 锚点：两条路径都在查询线程里现取、都回填同一个锚点
    assert manual.request.anchor == auto.request.anchor == _LAST
    assert manual.task_last_at == auto.task_last_at == _LAST

    # 4) 摘要表：两条路径相同，具体文本钉死
    assert manual_summary == _summary(vm) == (summary_rows, basis)


def test_out_of_range_metric_shows_same_empty_state_on_both_paths(vm, db, monkeypatch):
    """
    锚点取任务整体最后采样（120 分钟），线程数最后一点在 60 分钟：焦点为线程数、选
    "10分钟"时该指标范围内无数据。两条路径都应显示"所选时间范围内暂无数据"，查询集合
    相同（线程数只查到明细为止，不再查统计/空洞/分桶）。
    """
    task = _seed_query_dataset(db)
    _prepare(vm, task, THREADS, '10m')
    expected_since = BASE + timedelta(minutes=110)

    calls = _record_db_queries(db, monkeypatch)
    snapshots, empties = _record(vm)

    _manual_load(vm)
    manual_calls = Counter(calls)
    calls.clear()
    _auto_refresh(vm)
    auto_calls = Counter(calls)

    assert snapshots == []
    assert manual_calls == auto_calls
    thread_calls = sorted(label for label, key, kw in manual_calls
                          if THREADS in key or ('metric', THREADS) in kw)
    assert thread_calls == ['table']
    assert ('table', (THREADS, expected_since, None, TABLE_LIMIT), ()) in manual_calls
    assert empties == [("所选时间范围内暂无数据", "尝试切换到「全部」查看完整历史")] * 2
    assert vm.last_analysis is None


# ---------- 基线、空洞与系统打点 ----------

# 统计只用基线（20 分钟）之后的 86 个点：基线值 = 20..24 分钟中位数 141 MB；
# 两轮：141 → 156 → 181 MB，每轮 +15、+25，均值 +20 MB；10 分钟范围只有 11 点，
# 增速门槛不过，但基线值仍作参考、变化量相对基线
BASELINE_ROW = (['私有字节', '141 MB', '190 MB', '+49 MB (+34.8%)', '+20 MB',
                 '+30 ±0 MB/小时 · R² 1.00', '↑ +10 MB', '持续上涨', ''], False)
BASELINE_CASES = [
    ('baseline', 20, 86, 1, BASELINE_ROW, "基线 08:20:00 起 · 1小时40分 · 86 点"),
    ('all', None, 106, 1, BASELINE_ROW, "基线 08:20:00 起 · 1小时40分 · 86 点"),
    ('10m', 110, 11, 0,
     (['私有字节', '141 MB', '190 MB', '+49 MB (+34.8%)', '--', '数据不足（已采 10分钟）',
       '↑ +5 MB', '数据不足', ''], True),
     "10分钟 · 11 点"),
]


@pytest.mark.parametrize(
    "range_key, since_minutes, total, gap_count, summary_row, basis", BASELINE_CASES,
    ids=[case[0] for case in BASELINE_CASES])
def test_baseline_and_gaps_identical_on_both_paths(
        vm, db, range_key, since_minutes, total, gap_count, summary_row, basis):
    """
    存在基线时：统计只用基线之后的数据、首列为基线值；"自基线"范围从基线起；
    空洞（69 → 85 分钟）进入快照；暂停/恢复作为系统打点保留。两条路径逐项相同。
    """
    task = _seed_baseline_dataset(db)
    _prepare(vm, task, MEM, range_key)
    snapshots, _empties = _record(vm)

    _manual_load(vm)
    manual_analysis, manual_summary = vm.last_analysis, _summary(vm)
    _auto_refresh(vm)

    manual, _auto = snapshots
    _assert_same_analysis(manual_analysis, vm.last_analysis)
    assert manual_summary == _summary(vm) == ([summary_row], basis)

    expected_since = None if since_minutes is None else BASE + timedelta(minutes=since_minutes)
    baseline_at = BASE + timedelta(minutes=20)
    assert manual.request.since == expected_since
    assert manual.stats_since == max(expected_since or baseline_at, baseline_at)
    assert manual.total_count == total
    assert manual.baseline.value == 1024.0 * 141 and manual.baseline.sample_count == 5
    assert vm.has_baseline is True
    assert [(m.label, m.kind) for m in manual.markers] == [
        ("基线", 'baseline'), ("第1轮", 'user'), ("暂停", 'pause'), ("恢复", 'resume'),
        ("第2轮", 'user')]
    assert len(manual.gaps) == gap_count
    if gap_count:
        assert manual.gaps[0] == (BASE + timedelta(minutes=69), BASE + timedelta(minutes=85))


# ---------- 选择语义 ----------


def _seed_simple(db, name, start, metrics=(MEM,), samples=3):
    task = _make_task(db, name=name, start=start, metrics=metrics, status="stopped")
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i), metric)
        for i in range(samples) for metric in metrics])
    return task

@pytest.fixture
def plain_vm(qapp, db, manager):
    """查询同步执行、加载请求立即发出的视图模型：只观察重载与选择的结果。"""
    services = make_services(clock=FakeClock(NOW), task_status=FakeTaskStatus())
    return AnalysisViewModel(db, manager, services, worker_runner=SyncHistoryRunner(db),
                             defer=lambda fn: fn())


def test_select_task_survives_new_task_and_keeps_user_metric(plain_vm, db):
    """
    select_task 是显式选择：之后列表里出现更新的任务、再次重载，仍停在该任务；
    用户在其上切过的焦点指标也被恢复，不回到首指标。
    """
    vm = plain_vm
    target = _seed_simple(db, "target.exe", BASE, metrics=(MEM, THREADS))
    vm.select_task(target.task_id)
    assert vm.selection_is_auto is False
    vm.select_metric(THREADS)
    assert vm.current_metric == THREADS

    _seed_simple(db, "newer.exe", BASE + timedelta(hours=1))
    vm.reload_tasks()

    assert vm.current_task_id == target.task_id
    assert vm.selection_is_auto is False
    assert vm.current_metric == THREADS


def test_auto_selection_keeps_following_newest_and_resets_metric(plain_vm, db):
    """
    自动选中（启动默认选最新）：新任务接连出现时每次都切到最新，切换后仍是自动选中
    状态；切到新任务时指标回到新任务的首指标；最新任务不变时重载不改变选择。
    """
    vm = plain_vm
    first = _seed_simple(db, "first.exe", BASE, metrics=(MEM, THREADS))
    vm.reload_tasks()
    assert vm.current_task_id == first.task_id
    assert vm.selection_is_auto is True

    # 最新任务没变：重载保持原选择与自动状态
    vm.reload_tasks()
    assert vm.current_task_id == first.task_id
    assert vm.selection_is_auto is True

    second = _seed_simple(db, "second.exe", BASE + timedelta(hours=1), metrics=(THREADS, MEM))
    vm.reload_tasks()
    assert vm.current_task_id == second.task_id
    assert vm.tasks[0].task_id == vm.current_task_id
    assert vm.selection_is_auto is True
    assert vm.current_metric == THREADS

    third = _seed_simple(db, "third.exe", BASE + timedelta(hours=2))
    vm.reload_tasks()
    assert vm.current_task_id == third.task_id
    assert vm.selection_is_auto is True
    assert vm.current_metric == MEM


def test_auto_selected_task_removed_falls_back_to_newest(plain_vm, db):
    """
    自动选中的任务被删掉：回落到最新任务并保持自动状态（与"用户显式选中的任务消失
    时清空选择"形成对照，见下一个用例）。
    """
    vm = plain_vm
    older = _seed_simple(db, "older.exe", BASE)
    newest = _seed_simple(db, "newest.exe", BASE + timedelta(hours=1))
    vm.reload_tasks()
    assert vm.current_task_id == newest.task_id

    db.delete_task(newest.task_id)
    vm.reload_tasks()
    assert vm.current_task_id == older.task_id
    assert vm.tasks[0].task_id == vm.current_task_id
    assert vm.selection_is_auto is True
    assert vm.last_analysis is not None


def test_user_selected_task_removed_clears_selection(plain_vm, db):
    """用户在下拉里手动选中的任务被删掉：清空选择，不静默选中其他任务。"""
    vm = plain_vm
    older = _seed_simple(db, "older.exe", BASE)
    _seed_simple(db, "newest.exe", BASE + timedelta(hours=1))
    vm.reload_tasks()
    vm.select_task(older.task_id, explicit=True)   # 等同用户在下拉里点选
    assert vm.current_task_id == older.task_id
    assert vm.selection_is_auto is False

    db.delete_task(older.task_id)
    vm.reload_tasks()
    assert vm.current_task_id is None
    assert vm.current_metric is None
    assert len(vm.tasks) == 1
    assert vm.last_analysis is None
