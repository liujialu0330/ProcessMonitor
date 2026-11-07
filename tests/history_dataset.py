"""
测试用历史数据集（不是测试模块）：查询编排、趋势图、明细表与视图模型的测试共用。

简单任务（_seed_task，每秒一次采样）、两份固定数据集（每分钟一次采样）与对单次
run_history_query 钉死的期望值：
- _seed_query_dataset：私有字节 121 点（带尖峰）+ 线程数 61 点 + 两个用户打点；
- _seed_baseline_dataset：基线、两轮打点、暂停/恢复与 70..84 分钟空洞。
分析页层面"手动加载与自动刷新同口径"的安全网见 tests/test_safety_net_history.py。
"""
import uuid
from datetime import datetime, timedelta

from data.models import DataPoint, MonitorTask
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 8, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
THREADS = MetricType.NUM_THREADS

# 缩小表格上限与分桶上限，让"全部"范围真正触发截断与抽稀
TABLE_LIMIT = 50
MAX_BUCKETS = 20

# 固定的当前时间：只影响下拉时长文案，不影响查询锚点
NOW = BASE + timedelta(minutes=121)

# 简单任务（每秒一次采样）的起点：_seed_task 默认用它
SIMPLE_BASE = datetime(2026, 7, 16, 20, 0, 0)


def _make_task(db, name="leak.exe", start=BASE, metrics=(MEM, THREADS),
               status="running", note=""):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name,
        metric_types=list(metrics), interval=60.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(hours=2),
        status=status, note=note)
    db.save_task(task)
    return task


def _seed_task(db, samples=10, note="", status="stopped", markers=(), name="demo.exe",
               start=SIMPLE_BASE, metric=MEM, metrics=None):
    """每秒一次采样的简单任务：值为 (100 + i) MB，可附用户打点 (偏移秒数, 标签)。"""
    metrics = list(metrics or [metric])
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name, metric_types=metrics,
        interval=1.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(seconds=samples),
        status=status, note=note)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i), m)
        for i in range(samples) for m in metrics])
    for offset, label in markers:
        db.add_marker(task.task_id, start + timedelta(seconds=offset), label)
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


# 单次查询（单指标、非分析模式）的期望：
# (指标, 范围, 期望 since 相对 BASE 的分钟数（None=不过滤）, 表格行数, 统计 count,
#  分桶后曲线点数, 打点数, 当前值文本)
# 打点数恒为 2：快照带任务全部打点（编号与基线参考不随范围变化），图表自行裁剪视口。
# 分桶点数按 2.0 的图表点集定义（AnalyticsRepo.legacy_buckets：按时间等分，时长 ≥ 桶数分钟时
# 桶宽取整分钟并对齐自然分钟，每桶取 MIN/MAX 两个真实点）。1.x 按行号等分时三处都是 40。
QUERY_CASES = [
    # 全部：121 点（121 个自然分钟），表格截到最近 50 条；桶宽 ceil(121/20) = 7 分钟 →
    # 18 桶 × (MIN, MAX) = 36 点
    (MEM, 'all', None, 50, 121, 36, 2, "160 MB"),
    # 10 分钟：锚点 120 分钟 → since 110 分钟（含端点）共 11 点，点数少于桶数不抽稀
    (MEM, '10m', 110, 11, 11, 11, 2, "160 MB"),
    # 1 小时：61 点，桶宽 ceil(61/20) = 4 分钟 → 16 桶，最后一桶只有 120 分钟这一点 → 31 点
    (MEM, '1h', 60, 50, 61, 31, 2, "160 MB"),
    # 线程数 0..60 分钟共 61 点，同上 31 点
    (THREADS, 'all', None, 50, 61, 31, 2, "24 个"),
    # 线程数最后一点在 60 分钟；1 小时范围（since 60 分钟）只含这一点
    (THREADS, '1h', 60, 1, 1, 1, 2, "24 个"),
]
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
