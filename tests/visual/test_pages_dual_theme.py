"""
三个页面（监控 / 分析 / 设置）在浅色、深色主题下的离屏截图（1280×820）。

与组合根相同的装配：build_context(paths) 迁移并组装 Database / MonitorManager / 服务，
再建 MainWindow(db, manager, services, hotkey=...)；只把进程查询换成固定进程表、对话框
换成脚本替身、快捷键换成假后端（测试绝不注册真实热键）。数据目录是根 conftest 指向的
临时目录，里面造一条已结束的任务：基线 + 2 轮打点，3 个指标共 300 个点。

断言只管"截出来了、尺寸对、浅深确实不同、深色确实是深底"，图里画得好不好由人看
（输出目录见 README.md）。
"""
import dataclasses
import uuid
from datetime import datetime, timedelta

import pytest

from tests.doubles import FakeProcessQuery, ScriptedDialogs
from tests.helpers import process_events_until
from tests.visual.snapshot import DEFAULT_SIZE, load_image, mean_luminance, pixel_difference_ratio

SIZE = DEFAULT_SIZE   # (1280, 820)
# 浅深两张图至少要有这么多像素不同
MIN_DIFF_RATIO = 0.01
# 整图平均亮度：深色主题应明显偏暗、浅色明显偏亮（0–255）
DARK_MAX_LUMINANCE = 100
LIGHT_MIN_LUMINANCE = 170

PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "CADModeler.exe", 1002.5),
             (1003, "notepad.exe", 1003.5)]
SAMPLES = 100           # 每个指标的点数
INTERVAL = 10           # 秒；跨度 16.5 分钟，够判定门槛（≥10 分钟且 ≥30 点）


class _FakeHotkeyBackend:
    """注册总是成功、不碰系统热键的后端（设置页显示"已启用"）。"""

    def register(self, hotkey_id, modifiers, vk):
        return True

    def unregister(self, hotkey_id):
        pass


def _seed_finished_task(db, manager) -> str:
    """一条已结束的任务：私有字节持续上涨、句柄平稳、GDI 缓慢爬升；基线 + 2 轮打点。"""
    from data.models import DataPoint, MonitorTask
    from utils.metrics import MetricType

    metrics = [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES, MetricType.NUM_GDI_OBJECTS]
    start = (datetime.now() - timedelta(hours=2)).replace(microsecond=0)
    duration = (SAMPLES - 1) * INTERVAL
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=1002, process_name="CADModeler.exe",
                       metric_types=list(metrics), interval=float(INTERVAL),
                       start_time=start, end_time=start + timedelta(seconds=duration),
                       status="stopped", note="截图夹具 · 建模 200 次")
    assert db.save_task(task)
    points = []
    for i in range(SAMPLES):
        ts = start + timedelta(seconds=i * INTERVAL)
        values = {
            MetricType.MEMORY_PRIVATE: 512000.0 + i * 410 + (i % 7) * 35,
            MetricType.NUM_HANDLES: 812.0 + (3 if i % 23 in (11, 12) else 0),
            MetricType.NUM_GDI_OBJECTS: 1450.0 + i // 4,
        }
        points += [DataPoint(task.task_id, ts, value, metric) for metric, value in values.items()]
    assert len(points) == 300
    db.save_data_points(points)
    assert manager.add_marker(task.task_id, "基线", start + timedelta(minutes=1))
    assert manager.add_marker(task.task_id, "第 1 轮", start + timedelta(minutes=6))
    assert manager.add_marker(task.task_id, "第 2 轮", start + timedelta(minutes=11))
    return task.task_id


@pytest.fixture
def context(qapp, visual_data_dir):
    from app.bootstrap import build_context
    from ui.hotkey import MarkerHotkey

    ctx = build_context(visual_data_dir, retention_days=0)
    services = dataclasses.replace(
        ctx.services, process_query=FakeProcessQuery(PROCESSES),
        dialogs=ScriptedDialogs(ask_close_action='quit'))
    hotkey = MarkerHotkey(_FakeHotkeyBackend())
    hotkey.bind("Ctrl+Alt+M")
    task_id = _seed_finished_task(ctx.db, ctx.manager)
    yield ctx, services, hotkey, task_id
    process_events_until(lambda: not services.threads.entries(), timeout=10)
    ctx.manager.stop_all_tasks()
    ctx.manager.join_all(3000)
    ctx.db.close()


def _window_builder(ctx, services, hotkey, page_attr):
    def build():
        from ui.main_window import MainWindow
        window = MainWindow(ctx.db, ctx.manager, services, hotkey=hotkey)
        # 不发起真实联网的检查更新（主窗口 3 秒后会静默检查一次）
        window.about_group.check_update = lambda *args, **kwargs: None
        window.switchTo(getattr(window, page_attr))
        return window
    return build


def _settle_monitor(window):
    assert process_events_until(lambda: not window.monitor_page.form.picker.is_loading())


def _settle_analysis(window):
    vm = window.analysis_page.vm
    assert process_events_until(lambda: vm.last_analysis is not None, timeout=15), \
        "分析页应在期限内加载完任务分析"
    assert process_events_until(lambda: not window.services.threads.entries(), timeout=10)


def _settle_setting(window):
    process_events_until(lambda: False, timeout=0.3)


def _assert_pair(light, dark):
    for path in (light, dark):
        assert path.is_file(), path
        image = load_image(path)
        assert (image.width(), image.height()) == SIZE, path
    ratio = pixel_difference_ratio(light, dark)
    assert ratio > MIN_DIFF_RATIO, f"浅深两张图几乎相同（差异 {ratio:.2%}）"
    assert mean_luminance(dark) < DARK_MAX_LUMINANCE, "深色主题截图应是深底"
    assert mean_luminance(light) > LIGHT_MIN_LUMINANCE, "浅色主题截图应是浅底"


@pytest.mark.parametrize("page_attr, name, settle", [
    ("monitor_page", "monitor", _settle_monitor),
    ("analysis_page", "analysis", _settle_analysis),
    ("setting_page", "setting", _settle_setting),
])
def test_page_dual_theme(visual_snapshot, context, page_attr, name, settle):
    ctx, services, hotkey, _task_id = context
    light, dark = visual_snapshot(_window_builder(ctx, services, hotkey, page_attr), name,
                                  SIZE, settle=settle)
    _assert_pair(light, dark)


# ---------- 分析页：空洞 + 3 轮打点 + 对比任务（深色主题收口） ----------

RICH_SIZE = (1280, 2300)       # 结论条、摘要表、对比卡片、区间表、焦点大图、小倍数与折叠行一屏放下
RICH_SAMPLES = 120
GAP_INDICES = range(50, 65)    # 第 500–640 秒没有采样：间隔 150 秒 > 2.5 × 周期，画成空洞
# 卡片区域平均亮度（0–255）：深色主题下卡片不应再有浅色矩形，浅色主题下应是浅底
CARD_DARK_MAX_LUMINANCE = 80
CARD_LIGHT_MIN_LUMINANCE = 200


def _seed_rich_task(db, manager) -> str:
    """比夹具任务晚开始（分析页默认选中它）：私有字节上涨、句柄平稳、GDI 爬升；
    中间一段空洞；基线 + 3 轮打点。"""
    from data.models import DataPoint, MonitorTask
    from utils.metrics import MetricType

    metrics = [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES, MetricType.NUM_GDI_OBJECTS]
    start = (datetime.now() - timedelta(hours=1)).replace(microsecond=0)
    duration = (RICH_SAMPLES - 1) * INTERVAL
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=1003, process_name="CADModeler.exe",
                       metric_types=list(metrics), interval=float(INTERVAL),
                       start_time=start, end_time=start + timedelta(seconds=duration),
                       status="stopped", note="截图夹具 · 空洞 + 3 轮")
    assert db.save_task(task)
    points = []
    for i in range(RICH_SAMPLES):
        if i in GAP_INDICES:
            continue
        ts = start + timedelta(seconds=i * INTERVAL)
        values = {
            MetricType.MEMORY_PRIVATE: 520000.0 + i * 520 + (i % 5) * 60,
            MetricType.NUM_HANDLES: 820.0 + (4 if i % 17 in (8, 9) else 0),
            MetricType.NUM_GDI_OBJECTS: 1460.0 + i // 3,
        }
        points += [DataPoint(task.task_id, ts, value, metric) for metric, value in values.items()]
    db.save_data_points(points)
    for minutes, label in ((1, "基线"), (5, "第 1 轮"), (12, "第 2 轮"), (16, "第 3 轮")):
        assert manager.add_marker(task.task_id, label, start + timedelta(minutes=minutes))
    return task.task_id


def _region_luminance(path, rect) -> float:
    """图片中 rect=(x, y, w, h) 区域的平均亮度（Rec. 601，0–255）。"""
    import numpy as np

    image = load_image(path)
    ptr = image.constBits()
    ptr.setsize(image.byteCount())
    rows = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine())
    bgr = rows[:, :image.width() * 4].reshape(image.height(), image.width(), 4)[..., :3]
    x, y, w, h = rect
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(image.width(), x + w), min(image.height(), y + h)
    assert x1 > x0 and y1 > y0, f"区域不在图内：{rect}"
    region = bgr[y0:y1, x0:x1].astype(float)
    return float((0.299 * region[..., 2] + 0.587 * region[..., 1]
                  + 0.114 * region[..., 0]).mean())


def test_analysis_rich_dual_theme(visual_snapshot, context):
    ctx, services, hotkey, compare_task_id = context
    rich_task_id = _seed_rich_task(ctx.db, ctx.manager)
    rects = {}

    def settle(window):
        page = window.analysis_page
        vm = page.vm
        assert process_events_until(
            lambda: vm.current_task_id == rich_task_id and vm.last_analysis is not None,
            timeout=15), "分析页应默认选中最新任务并加载完分析"
        page.toolbar.compare_button.setChecked(True)
        # 与用户在对比下拉里选任务同一路径（selector.task_selected → vm.set_compare_task）
        assert page.compare_panel.selector.select_by_id(compare_task_id)
        assert process_events_until(lambda: vm.last_compare is not None, timeout=15), \
            "对比任务应在期限内加载完"
        assert process_events_until(lambda: not window.services.threads.entries(), timeout=10)
        process_events_until(lambda: False, timeout=0.3)
        # 平稳的句柄数折叠为一行迷你图，只有持续上涨的私有字节（焦点大图）与 GDI 有图
        from utils.metrics import MetricType
        charts = list(page.trend_panel.charts().values())
        assert len(charts) == 2
        assert page.trend_panel.folded_metrics() == [MetricType.NUM_HANDLES]
        first = charts[0]
        assert first.gap_items(), "夹具数据应画出空洞阴影"
        assert len([b for _k, b, _l in first.marker_items() if b is not None]) == 4
        # 记录各卡片在窗口中的位置（截图容器无边距，窗口坐标即图片坐标）
        for name, widget in (("summary", page.summary_table), ("chart", first),
                             ("compare", page.compare_panel)):
            assert widget.isVisible(), name
            top_left = widget.mapTo(window, widget.rect().topLeft())
            rects[name] = (top_left.x(), top_left.y(), widget.width(), widget.height())

    build = _window_builder(ctx, services, hotkey, "analysis_page")
    light, dark = visual_snapshot(build, "analysis_rich", RICH_SIZE, settle=settle)
    for path in (light, dark):
        image = load_image(path)
        assert (image.width(), image.height()) == RICH_SIZE, path
    assert pixel_difference_ratio(light, dark) > MIN_DIFF_RATIO
    assert set(rects) == {"summary", "chart", "compare"}
    for name, rect in rects.items():
        dark_value = _region_luminance(dark, rect)
        light_value = _region_luminance(light, rect)
        assert dark_value < CARD_DARK_MAX_LUMINANCE, \
            f"深色主题下 {name} 卡片区域偏亮（平均亮度 {dark_value:.0f}），可能残留浅色矩形"
        assert light_value > CARD_LIGHT_MIN_LUMINANCE, \
            f"浅色主题下 {name} 卡片区域偏暗（平均亮度 {light_value:.0f}）"


# ---------- 分析页首屏（1280×820）：结论条 + 摘要表 + 区间表前 3 行不滚动可见 ----------

FIRST_SCREEN_SAMPLES = 150     # 10 秒一个点，25 分钟
FIRST_SCREEN_ROWS = 3


def _seed_leak_preset_task(db, manager) -> str:
    """最新的任务（分析页默认选中）：泄漏排查预设 5 个指标，私有字节与句柄数持续上涨，
    GDI / USER / 线程平稳；基线 + 3 轮打点。"""
    from data.models import DataPoint, MonitorTask
    from utils.metrics import MetricType

    metrics = [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES, MetricType.NUM_GDI_OBJECTS,
               MetricType.NUM_USER_OBJECTS, MetricType.NUM_THREADS]
    start = (datetime.now() - timedelta(minutes=30)).replace(microsecond=0)
    duration = (FIRST_SCREEN_SAMPLES - 1) * INTERVAL
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=1004, process_name="CADModeler.exe",
                       metric_types=list(metrics), interval=float(INTERVAL),
                       start_time=start, end_time=start + timedelta(seconds=duration),
                       status="stopped", note="截图夹具 · 泄漏预设 3 轮")
    assert db.save_task(task)
    points = []
    for i in range(FIRST_SCREEN_SAMPLES):
        ts = start + timedelta(seconds=i * INTERVAL)
        values = {
            MetricType.MEMORY_PRIVATE: 512000.0 + i * 240 + (i % 7) * 30,
            MetricType.NUM_HANDLES: 812.0 + i // 3,
            MetricType.NUM_GDI_OBJECTS: 1450.0 + (2 if i % 19 in (5, 6) else 0),
            MetricType.NUM_USER_OBJECTS: 230.0,
            MetricType.NUM_THREADS: 24.0 + (1 if i % 29 == 7 else 0),
        }
        points += [DataPoint(task.task_id, ts, value, metric) for metric, value in values.items()]
    db.save_data_points(points)
    for minutes, label in ((1, "基线"), (7, "第 1 轮"), (13, "第 2 轮"), (19, "第 3 轮")):
        assert manager.add_marker(task.task_id, label, start + timedelta(minutes=minutes))
    return task.task_id


def _window_rect(widget, window):
    from PyQt5.QtCore import QPoint, QRect
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def test_analysis_first_screen_dual_theme(visual_snapshot, context):
    from PyQt5.QtCore import QEvent, QPoint, QRect
    from PyQt5.QtWidgets import QApplication

    from ui.widgets.history.chart_view import ChartView
    from utils.metrics import MetricType

    ctx, services, hotkey, _task_id = context
    task_id = _seed_leak_preset_task(ctx.db, ctx.manager)
    seen = []

    def settle(window):
        page = window.analysis_page
        vm = page.vm
        assert process_events_until(
            lambda: vm.current_task_id == task_id and vm.last_analysis is not None,
            timeout=15), "分析页应默认选中最新任务并加载完分析"
        assert process_events_until(lambda: not window.services.threads.entries(), timeout=10)
        process_events_until(lambda: False, timeout=0.3)
        assert page.verticalScrollBar().value() == 0
        # 只为 2 项持续上涨建图（焦点大图 + 1 张小图），3 项平稳折叠
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        views = [c for c in page.findChildren(ChartView) if type(c) is ChartView]
        assert len(views) == 2
        assert sorted(page.trend_panel.folded_metrics()) == sorted([
            MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS, MetricType.NUM_THREADS])
        assert page.verdict_banner.sentence().startswith("2 / 5 项持续上涨：")
        # 首屏（不滚动）可见：滚动区视口在窗口中的矩形
        viewport = page.viewport()
        visible = QRect(viewport.mapTo(window, QPoint(0, 0)), viewport.size())
        for name, widget in (("结论条", page.verdict_banner), ("摘要表", page.summary_table)):
            assert widget.isVisible(), name
            assert visible.contains(_window_rect(widget, window)), \
                f"{name}应在首屏完整可见：{_window_rect(widget, window)} 不在 {visible} 内"
        table = page.interval_table.table
        assert table.isVisible() and table.rowCount() >= FIRST_SCREEN_ROWS
        for row in range(FIRST_SCREEN_ROWS):
            cell = table.visualRect(table.model().index(row, 0))
            rect = QRect(table.viewport().mapTo(window, cell.topLeft()), cell.size())
            assert visible.contains(rect), f"区间表第 {row + 1} 行应在首屏可见：{rect} 不在 {visible} 内"
        seen.append(window.isVisible())

    build = _window_builder(ctx, services, hotkey, "analysis_page")
    light, dark = visual_snapshot(build, "analysis_first_screen", SIZE, settle=settle)
    assert seen == [True, True]
    _assert_pair(light, dark)


# ---------- 任务卡片：状态胶囊、选中色条、迷你图与打点刻度 ----------

def test_task_cards_dual_theme(visual_snapshot):
    from PyQt5.QtWidgets import QVBoxLayout, QWidget
    from types import SimpleNamespace

    from core.contracts import StopReason
    from ui.components.task_card import TaskCard
    from utils.metrics import MetricType

    metrics = [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES, MetricType.NUM_GDI_OBJECTS]
    start = datetime(2026, 11, 7, 9, 0, 0)

    def card(task_id, paused=False, finished=None):
        widget = TaskCard(task_id, "CADModeler.exe", 1002, list(metrics), interval=1.0,
                          note="截图夹具")
        widget.set_started_at(start)
        for i in range(120):
            widget.update_values({
                MetricType.MEMORY_PRIVATE: 512000.0 + i * 410 + (i % 7) * 35,
                MetricType.NUM_HANDLES: 812.0 + (3 if i % 23 in (11, 12) else 0),
                MetricType.NUM_GDI_OBJECTS: 1450.0 + i // 4,
            }, start + timedelta(seconds=i))
        widget.set_markers([
            SimpleNamespace(timestamp=start + timedelta(seconds=20), kind='baseline'),
            SimpleNamespace(timestamp=start + timedelta(seconds=60), kind='user'),
            SimpleNamespace(timestamp=start + timedelta(seconds=100), kind='user')])
        if paused:
            widget.set_paused(True)
        if finished is not None:
            widget.set_finished(finished, start + timedelta(seconds=120))
        return widget

    def build():
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(card("running"))
        layout.addWidget(card("paused", paused=True))
        layout.addWidget(card("abnormal", finished=StopReason.PROCESS_EXITED))
        layout.addWidget(card("finished", finished=StopReason.USER))
        layout.addStretch()
        return host

    light, dark = visual_snapshot(build, "task_cards", (1100, 640))
    assert pixel_difference_ratio(light, dark) > MIN_DIFF_RATIO
    assert mean_luminance(dark) < DARK_MAX_LUMINANCE
    assert mean_luminance(light) > LIGHT_MIN_LUMINANCE
