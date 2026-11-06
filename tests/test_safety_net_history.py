"""
阶段 0 安全网（特性测试）：历史页查询编排与选择语义。

锁住的行为（历史页任何重构下的漂移都应让这里变红）：
1. 用户操作的加载（点"刷新"）与自动刷新（定时到点）对同一任务、同一指标、同一时间
   范围——两条路径都经视图模型、都用生产装配的后台线程查询：
   - 发给仓储的 since（datetime）/ limit / 分桶上限完全相同；
   - 交给页面渲染的快照（明细点、统计、分桶点、打点、锚点）逐项相等；
   - 渲染后的表格内容、表格提示、统计条文本（标题与值）、曲线点、打点竖线与编号徽标、
     纵轴标题完全相同；
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
    wrap(db.analytics, 'aggregates', 'stats',
         lambda a, k: (k.get('since'), None))
    wrap(db.analytics, 'value_near', 'value_near',
         lambda a, k: (a[2], None))
    wrap(db.analytics, 'gaps', 'gaps',
         lambda a, k: (k.get('since'), a[1]))
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
        (item.value(), item.label.toPlainText() if getattr(item, 'label', None) else None,
         item.toolTip())
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
        stat_notes=page.stats_strip.notes(),
        pill=page.stats_strip.pill_text(),
        dimmed=page.stats_strip.dimmed_titles(),
        baseline_range=page.filter_bar.baseline_available(),
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
#  分桶后曲线点数, 打点数, 统计条文本（标题 → 值）)
# 打点数恒为 2：快照带任务全部打点（编号与基线参考不随范围变化），图表自行裁剪视口。
# 增长格：水位量做线性回归，跨度 < 1 小时按 /分、否则 /小时，± 为 95% 置信区间；
# 跨度不足 max(10 分钟, 30 × 周期 60 秒) = 30 分钟或点数 < 30 时置灰"数据不足"；
# 统计范围内有 ≥ 2 个用户打点时改为"每轮增长"（打点后 5 个样本中位数之差的均值）。
_LAST = BASE + timedelta(minutes=120)
QUERY_CASES = [
    # 全部：121 点，表格截到最近 50 条；61 点以上分桶 20 桶 × (MIN, MAX) = 40 点。
    # 两个用户打点围出一轮：30 分钟后 5 点中位数 116 MB → 115 分钟后 158.5 MB，+42.5 MB
    (MEM, 'all', None, 50, 121, 40, 2,
     {"开始值": "150 MB", "当前值": "160 MB", "变化量": "+10 MB (+6.7%)",
      "每轮增长": "+42.5 MB", "最小": "100.5 MB", "最大": "209.5 MB"}),
    # 10 分钟：锚点 120 分钟 → since 110 分钟（含端点）共 11 点，点数少于桶数不抽稀；
    # 只有 11 点、跨度 10 分钟，增速门槛不过
    (MEM, '10m', 110, 11, 11, 11, 2,
     {"开始值": "155 MB", "当前值": "160 MB", "变化量": "+5 MB (+3.2%)",
      "每分钟增长": "数据不足（已采 10分钟）", "最小": "155 MB", "最大": "209.5 MB"}),
    (MEM, '1h', 60, 50, 61, 40, 2,
     {"开始值": "130 MB", "当前值": "160 MB", "变化量": "+30 MB (+23.1%)",
      "每小时增长": "+32.2 ±10.7 MB/小时", "最小": "130 MB", "最大": "209.5 MB"}),
    (THREADS, 'all', None, 50, 61, 40, 2,
     {"开始值": "20 个", "当前值": "24 个", "变化量": "+4 个 (+20.0%)",
      "每小时增长": "+0.3 ±1.72 个/小时", "最小": "20 个", "最大": "26 个"}),
    # 线程数最后一点在 60 分钟；1 小时范围（since 60 分钟）只含这一点，增速无法计算
    (THREADS, '1h', 60, 1, 1, 1, 2,
     {"开始值": "24 个", "当前值": "24 个", "变化量": "±0",
      "每分钟增长": "数据不足（已采 0秒）", "最小": "24 个", "最大": "24 个"}),
]
# 各用例的判定胶囊、置灰格与增长格说明（与 QUERY_CASES 一一对应）
QUERY_JUDGEMENTS = {
    (MEM, 'all'): ("波动", [], "1 轮均值"),
    (MEM, '10m'): ("数据不足", ["每分钟增长"], ""),
    (MEM, '1h'): ("波动", [], "基于 1小时0分 / 61 点"),
    (THREADS, 'all'): ("平稳", [], "基于 1小时0分 / 61 点"),
    (THREADS, '1h'): ("数据不足", ["每分钟增长"], ""),
}
# 打点竖线：(位置, 徽标, tooltip)
MARKER_LINES = [
    ((BASE + timedelta(minutes=30)).timestamp(), "①", "① 第1轮 · 07-16 08:30:00"),
    ((BASE + timedelta(minutes=115)).timestamp(), "②", "② 第2轮 · 07-16 09:55:00"),
]


def _expected_calls(metric, since):
    """一次有数据的加载发给仓储的查询（顺序固定）：打点（全部）→ 明细 → 统计范围聚合 →
    各轮打点处的值（仅私有字节"全部"有两个用户打点落在统计范围内）→ 空洞 → 分桶。"""
    calls = [('markers', None, None), ('table', since, TABLE_LIMIT), ('stats', since, None)]
    if metric == MEM and since is None:
        calls += [('value_near', BASE + timedelta(minutes=30), None),
                  ('value_near', BASE + timedelta(minutes=115), None)]
    calls += [('gaps', since, 60.0), ('buckets', since, MAX_BUCKETS)]
    return calls


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
    expected_calls = _expected_calls(metric, expected_since)
    assert manual_calls == expected_calls
    assert auto_calls == expected_calls
    assert manual.request.since == auto.request.since == expected_since

    # 2) 查询结果：逐项相等（DataPoint/Marker 是 dataclass，按值比较）
    for key in ('points', 'stats', 'buckets', 'markers', 'judgement', 'rate', 'gaps',
                'rounds', 'baseline', 'total_count', 'stats_since'):
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
    truncated = f" · 仅显示最近 {rows:,} 条" if count > rows else ""
    assert manual_view['hint'] == f"共 {count:,} 条（倒序）{truncated}"
    assert manual_view['stats'] == stat_texts
    assert manual_view['curve_points'] == [buckets]
    assert len(manual_view['chart_x']) == buckets
    assert len(manual_view['markers']) == len(manual_view['marker_data']) == markers
    assert manual_view['markers'] == MARKER_LINES
    pill, dimmed, rate_note = QUERY_JUDGEMENTS[(metric, range_key)]
    assert manual_view['pill'] == pill
    assert manual_view['dimmed'] == dimmed
    assert list(manual_view['stat_notes'].values())[3] == rate_note
    assert manual_view['baseline_range'] is False


def test_out_of_range_metric_shows_same_empty_state_on_both_paths(page, db, monkeypatch):
    """
    锚点取任务整体最后采样（120 分钟），线程数最后一点在 60 分钟：选"10分钟"时该指标
    范围内无数据。两条路径都应显示"所选时间范围内暂无数据"，且都只查到明细为止
    （打点先查——"自基线"范围的起点依赖它；不再查统计/空洞/分桶）。
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
    assert manual_calls == [('markers', None, None), ('table', expected_since, TABLE_LIMIT)]
    assert isinstance(manual_calls[1][1], datetime)
    assert auto_calls == manual_calls
    assert manual_view['content'] == auto_view['content'] == 'empty'
    assert manual_view['empty_title'] == auto_view['empty_title'] == "所选时间范围内暂无数据"
    assert manual_view['empty_detail'] == auto_view['empty_detail'] == "尝试切换到「全部」查看完整历史"


# ---------- 基线、空洞与系统打点 ----------

GAP_FROM, GAP_TO = 70, 85   # 70..84 分钟无采样（暂停 15 分钟）


def _seed_baseline_dataset(db):
    """
    带基线与空洞的数据集（每分钟一次采样，私有字节）：
    - 0..19 分钟预热段陡升 100 + 2i MB；20 分钟起平稳线性上涨 140 + 0.5(i-20) MB；
    - 70..84 分钟无采样（69 → 85 分钟间隔 16 分钟 > 2.5 × 60 秒，构成空洞）；
    - 打点：20 分钟基线、50 分钟「第1轮」、100 分钟「第2轮」，69:30 暂停、84:30 恢复。
    """
    task = _make_task(db, name="baseline.exe", metrics=(MEM,))
    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(minutes=i),
                  1024.0 * (100 + 2 * i if i < 20 else 140 + 0.5 * (i - 20)), MEM)
        for i in range(121) if not GAP_FROM <= i < GAP_TO])
    for minutes, label, kind in ((20, "基线", 'baseline'), (50, "第1轮", 'user'),
                                 (100, "第2轮", 'user'), (69.5, "暂停", 'pause'),
                                 (84.5, "恢复", 'resume')):
        db.markers.add(task.task_id, BASE + timedelta(minutes=minutes), label, kind)
    return task


# 统计只用基线（20 分钟）之后的 86 个点：基线值 = 20..24 分钟中位数 141 MB；
# 两轮：141 → 156 → 181 MB，每轮 +15、+25，均值 +20 MB；10 分钟范围只有 11 点，
# 增速门槛不过，但基线值仍作参考、变化量相对基线
BASELINE_CASES = [
    ('baseline', 20, 86,
     {"基线值": "141 MB", "当前值": "190 MB", "变化量": "+49 MB (+34.8%)",
      "每轮增长": "+20 MB", "最小": "140 MB", "最大": "190 MB"},
     "基线 08:20:00 起 · 1小时40分 · 86 点", "持续上涨", ["无数据 16分钟"]),
    ('all', None, 106,
     {"基线值": "141 MB", "当前值": "190 MB", "变化量": "+49 MB (+34.8%)",
      "每轮增长": "+20 MB", "最小": "140 MB", "最大": "190 MB"},
     "基线 08:20:00 起 · 1小时40分 · 86 点", "持续上涨", ["无数据 16分钟"]),
    ('10m', 110, 11,
     {"基线值": "141 MB", "当前值": "190 MB", "变化量": "+49 MB (+34.8%)",
      "每分钟增长": "数据不足（已采 10分钟）", "最小": "185 MB", "最大": "190 MB"},
     "10分钟 · 11 点", "数据不足", []),
]
BASELINE_MARKER_LINES = [
    ((BASE + timedelta(minutes=20)).timestamp(), "基", "基 基线 · 07-16 08:20:00"),
    ((BASE + timedelta(minutes=50)).timestamp(), "①", "① 第1轮 · 07-16 08:50:00"),
    ((BASE + timedelta(minutes=69.5)).timestamp(), None, "暂停 · 07-16 09:09:30"),
    ((BASE + timedelta(minutes=84.5)).timestamp(), None, "恢复 · 07-16 09:24:30"),
    ((BASE + timedelta(minutes=100)).timestamp(), "②", "② 第2轮 · 07-16 09:40:00"),
]


@pytest.mark.parametrize(
    "range_key, since_minutes, total, stat_texts, basis, pill, gap_texts", BASELINE_CASES,
    ids=[case[0] for case in BASELINE_CASES])
def test_baseline_and_gaps_identical_on_both_paths(
        page, db, range_key, since_minutes, total, stat_texts, basis, pill, gap_texts):
    """
    存在基线时：统计只用基线之后的数据、首格为基线值；"自基线"范围从基线起显示；
    空洞处曲线断开并画阴影；暂停/恢复画为不编号的系统打点。两条路径渲染逐项相同。
    """
    task = _seed_baseline_dataset(db)
    _prepare(page, task, MEM, range_key)
    snapshots = _record_snapshots(page)

    _manual_load(page)
    manual_view = _view(page)
    manual_chart = (page.chart_view.connect_array(),
                    [text for _region, text in page.chart_view.gap_items()],
                    page.stats_strip.basis())
    _clear_widgets(page)
    _auto_refresh(page)
    auto_view = _view(page)
    auto_chart = (page.chart_view.connect_array(),
                  [text for _region, text in page.chart_view.gap_items()],
                  page.stats_strip.basis())

    manual, auto = snapshots
    assert manual_view == auto_view
    assert manual_chart == auto_chart
    for key in ('points', 'stats', 'buckets', 'markers', 'judgement', 'rate', 'gaps',
                'rounds', 'baseline', 'total_count', 'stats_since'):
        assert getattr(manual, key) == getattr(auto, key), f"{key} 两条路径不一致"

    expected_since = None if since_minutes is None else BASE + timedelta(minutes=since_minutes)
    baseline_at = BASE + timedelta(minutes=20)
    assert manual.request.since == expected_since
    assert manual.stats_since == max(expected_since or baseline_at, baseline_at)
    assert manual.total_count == total
    assert manual.baseline.value == 1024.0 * 141 and manual.baseline.sample_count == 5

    assert manual_view['stats'] == stat_texts
    assert manual_view['pill'] == pill
    assert manual_view['baseline_range'] is True
    assert manual_view['markers'] == BASELINE_MARKER_LINES
    connect, gaps, strip_basis = manual_chart
    assert strip_basis == basis
    assert gaps == gap_texts
    # 曲线只在空洞处断开一次（无空洞时全连）
    assert connect.count(False) == len(gap_texts)
    assert manual_view['hint'].startswith(f"共 {total:,} 条（倒序）")
    # 明细"较基线"列相对基线值 141 MB
    assert manual_view['rows'][0][3] == "+49 MB"


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
