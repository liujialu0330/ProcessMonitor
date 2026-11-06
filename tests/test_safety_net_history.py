"""
阶段 0 安全网（特性测试）：历史页查询编排与选择语义。

锁住的行为（历史页任何重构下的漂移都应让这里变红）：
1. 用户操作的加载（点"刷新"）与自动刷新（定时到点）对同一任务、同一指标、同一时间
   范围——两条路径都经视图模型、都用生产装配的后台线程查询：
   - 发给仓储的 since（datetime）/ limit / 分桶上限完全相同；
   - 交给页面渲染的快照（明细点、统计、分桶点、打点、锚点）逐项相等；
   - 渲染后的表格内容、表格提示、统计条文本、曲线点、打点竖线、纵轴标题完全相同；
   - 范围内无数据时两条路径显示同一空状态文案。
2. 选择语义：select_task 显式选中后不被新任务切走；自动选中时新任务出现会一路
   跟到最新；自动选中的任务被删除时回落到最新任务（而用户显式选中的任务消失时清空）。
   选择统一由视图模型决定、控件只跟随（不再有信号屏蔽与"先设下标再置自动"的语句顺序）。

不依赖真实时间流逝：注入固定时钟（FakeClock）；不依赖机器进程列表：
"是否运行中"由注入的任务状态替身给出；数据库为 conftest 的临时库。
"""
import uuid
from datetime import datetime, timedelta

import pyqtgraph as pg
import pytest
from PyQt5.QtWidgets import QApplication

from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock, FakeTaskStatus
from tests.factories import make_history_page, make_services
from tests.helpers import SyncHistoryRunner, poll_until
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 8, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
THREADS = MetricType.NUM_THREADS

# 缩小表格上限与分桶上限，让"全部"范围真正触发截断与抽稀
TABLE_LIMIT = 50
MAX_BUCKETS = 20


# 固定的当前时间：只影响下拉时长文案，不影响查询锚点
NOW = BASE + timedelta(minutes=121)


def _pump():
    QApplication.processEvents()


def _wait(condition, timeout=10.0):
    def check():
        _pump()
        return condition()
    return poll_until(check, timeout=timeout)


def _make_task(db, name="leak.exe", start=BASE, metrics=(MEM, THREADS),
               status="running", note=""):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name,
        metric_types=list(metrics), interval=60.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(hours=2),
        status=status, note=note)
    db.save_task(task)
    return task


def _seed_query_dataset(db):
    """
    固定数据集（每分钟一次采样）：
    - 私有字节：0..120 分钟共 121 点，缓慢上涨并在若干位置出现尖峰，让分桶的 MIN/MAX 有意义；
    - 线程数：只在 0..60 分钟有 61 点（之后该指标不再采样），用于覆盖"锚点来自
      任务整体最后采样、而该指标在所选范围内无数据"的情形；
    - 打点：30 分钟「第1轮」、115 分钟「第2轮」。
    任务的 last_sample_at = BASE + 120 分钟。
    """
    task = _make_task(db)
    points = []
    for i in range(121):
        ts = BASE + timedelta(minutes=i)
        spike = 50.0 if i % 17 == 0 else 0.0
        points.append(DataPoint(task.task_id, ts, 1024.0 * (100 + i * 0.5 + spike), MEM))
        if i <= 60:
            points.append(DataPoint(task.task_id, ts, float(20 + (i % 7)), THREADS))
    db.save_data_points(points)
    db.add_marker(task.task_id, BASE + timedelta(minutes=30), "第1轮")
    db.add_marker(task.task_id, BASE + timedelta(minutes=115), "第2轮")
    return task


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def page(qapp, db, manager):
    """生产装配的查询路径（每个请求一个后台线程、加载推迟到下一轮事件循环），
    只缩小表格上限与分桶上限。所选任务视为运行中：定时刷新会发起后台查询。"""
    services = make_services(clock=FakeClock(NOW), task_status=FakeTaskStatus(all_running=True))
    widget = make_history_page(db, manager, services,
                               table_limit=TABLE_LIMIT, max_buckets=MAX_BUCKETS)
    widget.resize(1200, 800)
    yield widget
    widget.auto_refresh.stop()
    _wait(lambda: services.threads.entries() == [])
    widget.close()


def _idle(page):
    """等视图模型没有在途/排队的查询（结果已在主线程应用完毕）。"""
    assert _wait(lambda: not page.vm.has_pending_query), "查询未在期限内返回"
    _pump()


def _record_db_queries(db, monkeypatch):
    """包一层仓储只读查询，记录 (查询名, since, 规模参数)；工作线程里的调用同样被记录。

    since 一律是 datetime（仓储只收 datetime）：points.fetch 从 PointQuery 取，
    其余从 since 关键字参数取。"""
    calls = []

    def wrap(repo, name, label, extract):
        real = getattr(repo, name)

        def spy(*args, **kwargs):
            calls.append((label, *extract(args, kwargs)))
            return real(*args, **kwargs)
        monkeypatch.setattr(repo, name, spy)

    wrap(db.points, 'fetch', 'table',
         lambda a, k: (a[0].since, a[0].limit))
    wrap(db.analytics, 'legacy_stats', 'stats',
         lambda a, k: (k.get('since'), None))
    wrap(db.analytics, 'legacy_buckets', 'buckets',
         lambda a, k: (k.get('since'), a[2]))
    wrap(db.markers, 'list', 'markers',
         lambda a, k: (k.get('since'), None))
    return calls


def _record_snapshots(page):
    """记录视图模型交给页面渲染的快照（两条路径都汇入 snapshot_ready）。"""
    snapshots = []
    page.vm.snapshot_ready.connect(snapshots.append)
    return snapshots


def _view(page):
    """渲染结果快照：只收集用户看得到的内容。"""
    chart = page.chart_view
    plot_items = chart.chart_widget.getPlotItem().items
    marker_lines = sorted(
        (item.value(), item.label.toPlainText() if item.label else None)
        for item in plot_items
        if isinstance(item, pg.InfiniteLine) and item is not chart.crosshair_line)
    curve_points = [len(item.getData()[0]) for item in plot_items
                    if isinstance(item, pg.PlotDataItem)]
    is_empty = page.content_stack.currentWidget() is page.empty_state_card
    card = page.empty_state_card
    return dict(
        content='empty' if is_empty else 'analysis',
        # 空状态文案只在空状态卡可见时有意义（分析视图下是上一次残留的隐藏文本）
        empty_title=card.title_label.text() if is_empty else None,
        empty_detail=card.hint_label.text() if is_empty else None,
        rows=page.sample_table.row_texts(),
        hint=page.sample_table.table_hint_label.text(),
        stats=page.stats_strip.texts(),
        chart_x=list(chart.x_values), chart_y=list(chart.y_values),
        curve_points=curve_points,
        markers=marker_lines,
        marker_data=[(m.timestamp, m.label) for m in chart.markers],
        display_unit=chart.display_unit,
        left_axis=chart.axis_title(),
        interval=page.auto_refresh.interval,
    )


def _clear_widgets(page):
    page.chart_view.clear()
    page.stats_strip.clear()
    page.sample_table.clear()


def _prepare(page, task, metric, range_key):
    """模拟用户：显示页面、选中任务、在筛选条上切到指定指标与时间范围（定时器停掉，
    只手动触发）。"""
    page.show()
    page.auto_refresh.stop()
    page.select_task(task.task_id)
    _idle(page)
    combo = page.filter_bar.metric_combo
    index = next(i for i in range(combo.count()) if combo.itemData(i) == metric)
    combo.setCurrentIndex(index)
    page.filter_bar.range_segmented.setCurrentItem(range_key)
    _idle(page)
    assert (page.current_metric, page.vm.range_key) == (metric, range_key)


def _manual_load(page):
    """用户点"刷新"：整体重载并以用户操作口径加载（auto_refresh=False）。"""
    page.filter_bar.refresh_button.click()
    _idle(page)


def _auto_refresh(page):
    """定时刷新到点：运行中任务发起后台查询（auto_refresh=True）。"""
    page.auto_refresh.tick()
    assert page.vm.has_pending_query, "运行中任务的定时刷新应发起后台查询"
    _idle(page)


# (指标, 范围, 期望 since 相对 BASE 的分钟数（None=不过滤）, 表格行数, 统计 count,
#  分桶后曲线点数, 打点数, 统计条文本)
_LAST = BASE + timedelta(minutes=120)
QUERY_CASES = [
    # 全部：121 点，表格截到最近 50 条；61 点以上分桶 20 桶 × (MIN, MAX) = 40 点
    (MEM, 'all', None, 50, 121, 40, 2,
     {"起点": "150 MB", "当前": "160 MB", "变化": "+10 MB (+6.7%)",
      "增速": "+29.92 MB/小时", "最小": "100.5 MB", "最大": "209.5 MB"}),
    # 10 分钟：锚点 120 分钟 → since 110 分钟（含端点）共 11 点，点数少于桶数不抽稀
    (MEM, '10m', 110, 11, 11, 11, 1,
     {"起点": "155 MB", "当前": "160 MB", "变化": "+5 MB (+3.2%)",
      "增速": "+139.09 MB/小时", "最小": "155 MB", "最大": "209.5 MB"}),
    (MEM, '1h', 60, 50, 61, 40, 1,
     {"起点": "130 MB", "当前": "160 MB", "变化": "+30 MB (+23.1%)",
      "增速": "+32.22 MB/小时", "最小": "130 MB", "最大": "209.5 MB"}),
    (THREADS, 'all', None, 50, 61, 40, 2,
     {"起点": "20 个", "当前": "24 个", "变化": "+4 个 (+20.0%)",
      "增速": "+0.3 个/小时", "最小": "20 个", "最大": "26 个"}),
    # 线程数最后一点在 60 分钟；1 小时范围（since 60 分钟）只含这一点，增速无法计算
    (THREADS, '1h', 60, 1, 1, 1, 1,
     {"起点": "24 个", "当前": "24 个", "变化": "±0",
      "增速": "--", "最小": "24 个", "最大": "24 个"}),
]


@pytest.mark.parametrize(
    "metric, range_key, since_minutes, rows, count, buckets, markers, stat_texts",
    QUERY_CASES,
    ids=[f"{m}-{r}" for m, r, *_ in QUERY_CASES])
def test_manual_load_and_auto_refresh_are_identical(
        page, db, monkeypatch, metric, range_key, since_minutes, rows, count,
        buckets, markers, stat_texts):
    """
    用户操作的加载与自动刷新的查询参数、查询结果与渲染结果逐项相同；同时把具体数值
    钉死，任何一边漂移都会红。
    """
    task = _seed_query_dataset(db)
    _prepare(page, task, metric, range_key)

    calls = _record_db_queries(db, monkeypatch)
    snapshots = _record_snapshots(page)

    # ---- 路径一：用户操作的加载 ----
    _manual_load(page)
    manual_calls = list(calls)
    assert len(snapshots) == 1 and snapshots[0].request.auto_refresh is False
    manual = snapshots[0]
    manual_view = _view(page)

    # 清掉界面，确保后面看到的内容确实是自动刷新渲染出来的
    _clear_widgets(page)
    assert _view(page)['rows'] == []

    # ---- 路径二：自动刷新 ----
    calls.clear()
    _auto_refresh(page)
    auto_calls = list(calls)
    assert len(snapshots) == 2 and snapshots[1].request.auto_refresh is True
    auto = snapshots[1]
    auto_view = _view(page)

    # 1) 查询参数：同一 since、同一 limit / 分桶上限、同样的查询集合与顺序
    expected_since = (None if since_minutes is None
                      else BASE + timedelta(minutes=since_minutes))
    expected_calls = [
        ('table', expected_since, TABLE_LIMIT),
        ('stats', expected_since, None),
        ('buckets', expected_since, MAX_BUCKETS),
        ('markers', expected_since, None),
    ]
    assert manual_calls == expected_calls
    assert auto_calls == expected_calls
    assert manual.request.since == auto.request.since == expected_since

    # 2) 查询结果：逐项相等（DataPoint/Marker 是 dataclass，按值比较）
    for key in ('points', 'stats', 'buckets', 'markers'):
        assert getattr(manual, key) == getattr(auto, key), f"{key} 两条路径不一致"
    assert manual.request.metric == auto.request.metric == metric
    assert len(manual.points) == rows
    assert manual.points[-1].timestamp == (
        BASE + timedelta(minutes=120 if metric == MEM else 60))
    assert manual.stats['count'] == count
    assert len(manual.buckets) == buckets
    assert len(manual.markers) == markers

    # 3) 锚点：两条路径都在查询线程里现取、都回填同一个锚点（旧页面"全部"范围下
    #    手动加载不计算锚点的分叉已统一）
    assert manual.request.anchor == auto.request.anchor == _LAST
    assert manual.task_last_at == auto.task_last_at == _LAST

    # 4) 渲染结果：除视图自适应（keep_view）外完全相同
    assert manual_view == auto_view
    assert manual_view['content'] == 'analysis'
    assert len(manual_view['rows']) == rows
    # 明细表倒序：第一行是范围内最新一条
    assert manual_view['rows'][0][0] == manual.points[-1].timestamp.strftime('%m-%d %H:%M:%S')
    assert manual_view['hint'] == f"最新数据优先 · 显示最近 {rows:,} 条 / 共 {count:,} 条"
    assert manual_view['stats'] == stat_texts
    assert manual_view['curve_points'] == [buckets]
    assert len(manual_view['chart_x']) == buckets
    assert len(manual_view['markers']) == len(manual_view['marker_data']) == markers


def test_out_of_range_metric_shows_same_empty_state_on_both_paths(page, db, monkeypatch):
    """
    锚点取任务整体最后采样（120 分钟），线程数最后一点在 60 分钟：选"10分钟"时该指标
    范围内无数据。两条路径都应显示"所选时间范围内暂无数据"，且都只查到明细为止
    （不再查统计/分桶/打点）。
    """
    task = _seed_query_dataset(db)
    _prepare(page, task, THREADS, '10m')
    expected_since = BASE + timedelta(minutes=110)

    calls = _record_db_queries(db, monkeypatch)
    snapshots = _record_snapshots(page)

    _manual_load(page)
    manual_calls = list(calls)
    manual_view = _view(page)

    # 先人为切到分析视图，确认自动刷新确实再次落到空状态
    page.content_stack.setCurrentWidget(page.analysis_page)
    calls.clear()
    _auto_refresh(page)
    auto_calls = list(calls)
    auto_view = _view(page)

    assert snapshots == []
    assert manual_calls == [('table', expected_since, TABLE_LIMIT)]
    assert isinstance(manual_calls[0][1], datetime)
    assert auto_calls == manual_calls
    assert manual_view['content'] == auto_view['content'] == 'empty'
    assert manual_view['empty_title'] == auto_view['empty_title'] == "所选时间范围内暂无数据"
    assert manual_view['empty_detail'] == auto_view['empty_detail'] == "尝试切换到「全部」查看完整历史"


# ---------- 选择语义 ----------

def _seed_simple(db, name, start, metrics=(MEM,), samples=3):
    task = _make_task(db, name=name, start=start, metrics=metrics, status="stopped")
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i), metric)
        for i in range(samples) for metric in metrics])
    return task


@pytest.fixture
def plain_page(qapp, db, manager):
    """不显示、不跑自动刷新、查询同步执行的页面：只观察重载与选择的结果。"""
    services = make_services(clock=FakeClock(NOW), task_status=FakeTaskStatus())
    widget = make_history_page(db, manager, services,
                               worker_runner=SyncHistoryRunner(db), defer=lambda fn: fn())
    yield widget
    widget.close()


def _reload(page):
    """重新载入任务列表（页面每次显示、点刷新都走这条命令）。"""
    page.vm.reload_tasks()


def test_select_task_survives_new_task_and_keeps_user_metric(plain_page, db):
    """
    select_task 是显式选择：之后列表里出现更新的任务、再次重载，仍停在该任务；
    用户在其上切过的指标也被恢复，不回到首指标。
    """
    page = plain_page
    target = _seed_simple(db, "target.exe", BASE, metrics=(MEM, THREADS))
    page.select_task(target.task_id)
    assert page.vm.selection_is_auto is False
    page.filter_bar.metric_combo.setCurrentIndex(1)
    assert page.current_metric == THREADS

    _seed_simple(db, "newer.exe", BASE + timedelta(hours=1))
    _reload(page)

    assert page.current_task_id == target.task_id
    assert page.task_selector.current_id() == target.task_id
    assert page.vm.selection_is_auto is False
    assert page.current_metric == THREADS
    assert page.filter_bar.current_metric() == THREADS


def test_auto_selection_keeps_following_newest_and_resets_metric(plain_page, db):
    """
    自动选中（启动默认选最新）：新任务接连出现时每次都切到最新，切换后仍是自动选中
    状态；切到新任务时指标回到新任务的首指标；最新任务不变时重载不改变选择。
    """
    page = plain_page
    first = _seed_simple(db, "first.exe", BASE, metrics=(MEM, THREADS))
    _reload(page)
    assert page.current_task_id == first.task_id
    assert page.vm.selection_is_auto is True

    # 最新任务没变：重载保持原选择与自动状态
    _reload(page)
    assert page.current_task_id == first.task_id
    assert page.vm.selection_is_auto is True

    second = _seed_simple(db, "second.exe", BASE + timedelta(hours=1), metrics=(THREADS, MEM))
    _reload(page)
    assert page.current_task_id == second.task_id
    assert page.task_selector.current_index() == 0
    assert page.vm.selection_is_auto is True
    assert page.current_metric == THREADS
    assert page.filter_bar.current_metric() == THREADS

    third = _seed_simple(db, "third.exe", BASE + timedelta(hours=2))
    _reload(page)
    assert page.current_task_id == third.task_id
    assert page.vm.selection_is_auto is True
    assert page.current_metric == MEM


def test_auto_selected_task_removed_falls_back_to_newest(plain_page, db):
    """
    自动选中的任务被删掉：回落到最新任务并保持自动状态（与"用户显式选中的任务消失
    时清空选择"形成对照，见下一个用例）。
    """
    page = plain_page
    older = _seed_simple(db, "older.exe", BASE)
    newest = _seed_simple(db, "newest.exe", BASE + timedelta(hours=1))
    _reload(page)
    assert page.current_task_id == newest.task_id

    db.delete_task(newest.task_id)
    _reload(page)
    assert page.current_task_id == older.task_id
    assert page.task_selector.current_index() == 0
    assert page.vm.selection_is_auto is True
    assert page.content_stack.currentWidget() is page.analysis_page


def test_user_selected_task_removed_clears_selection(plain_page, db):
    """用户在下拉里手动选中的任务被删掉：清空选择，不静默选中其他任务。"""
    page = plain_page
    older = _seed_simple(db, "older.exe", BASE)
    _seed_simple(db, "newest.exe", BASE + timedelta(hours=1))
    _reload(page)
    page.task_selector.combo.setCurrentIndex(1)   # 等同用户点选
    assert page.current_task_id == older.task_id
    assert page.vm.selection_is_auto is False

    db.delete_task(older.task_id)
    _reload(page)
    assert page.current_task_id is None
    assert page.current_metric is None
    assert page.task_selector.current_index() == -1
    assert page.task_selector.count() == 1
    assert page.empty_state_card.title_label.text() == "请选择监控任务"
