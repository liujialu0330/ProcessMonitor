"""
报告图片 2.0 版式（ui/widgets/analysis/report_sheet.py）：

- 配色：默认固定浅色，深色界面下导出仍是浅底；设置"跟随主题"时才随界面变深；小倍数图之间
  的网格间距用页面底色（不再露出 Qt 默认的浅灰窗口色）；
- 标题块：120 字备注自动换行、不越出右边距，图片随之变高；
- 打点区间增量表：≥ 2 轮打点才出现；
- 小倍数图只画"持续上涨 / 波动"的指标；
- 尺寸：输出宽恒为 1600 像素，显式设备像素比与 QT_SCALE_FACTOR=1.5 下都不变、不裁切；
- render_report_for 签名不变，阶段快照的读库段 + 渲染仍可用；
- 设置页"报告配色"下拉卡与配置项。
"""
import inspect
import os
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtGui import QColor
from qfluentwidgets import Theme, qconfig

from app_config import REPORT_SCHEME_FOLLOW, REPORT_SCHEME_LIGHT, cfg
from core.history_query import TaskAnalysis, build_request, run_history_query
from data.models import DataPoint, MonitorTask
from tests.visual.snapshot import theme_applied
from ui import theme_tokens as tokens
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
GDI = MetricType.NUM_GDI_OBJECTS
USER = MetricType.NUM_USER_OBJECTS
THREADS = MetricType.NUM_THREADS
FIVE = (MEM, HANDLES, GDI, USER, THREADS)
THREE_ROUNDS = ((300, "基线", 'baseline'), (900, "第 1 轮", 'user'),
                (1500, "第 2 轮", 'user'), (2100, "第 3 轮", 'user'))
ONE_ROUND = ((300, "基线", 'baseline'), (900, "第 1 轮", 'user'))
GENERATED = BASE + timedelta(hours=1)
LONG_NOTE = "回归测试" * 30
LIGHT_PAGE = QColor(tokens.SURFACE[0]).rgb()
DARK_PAGE = QColor(tokens.SURFACE[1]).rgb()


def _value(metric, i, rising):
    if metric == MEM:
        return 50000 + 40 * i + (i % 7) * 3 if rising else 50000 + (i % 3)
    if metric == HANDLES:
        return 300 + i // 2 if rising else 300 + (i % 2)
    if metric == GDI:
        return 120 + (i % 3)
    if metric == USER:
        return 80
    return 20 + (i % 2)


def seed_task(db, *, note="回归 200 次", markers=THREE_ROUNDS, rising=True, metrics=FIVE):
    """5 个指标、每 10 秒一点共 40 分钟：rising 时私有字节与句柄数持续上涨，其余平稳。"""
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
        metric_types=list(metrics), interval=10.0, start_time=BASE,
        end_time=BASE + timedelta(minutes=40), status="stopped", note=note)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(seconds=10 * i), _value(m, i, rising), m)
        for i in range(240) for m in metrics])
    for offset, label, kind in markers:
        db.markers.add(task.task_id, BASE + timedelta(seconds=offset), label, kind=kind)
    return db.get_task(task.task_id)


def analyse(db, task):
    snapshots = {m: run_history_query(db, build_request(task.task_id, m, 'all', analysis=True))
                 for m in task.metric_types}
    return TaskAnalysis(task.task_id, 'all', snapshots)


def _render(task, analysis, **kwargs):
    from ui.widgets.analysis.report_sheet import build_report_text, render_report_image
    text = build_report_text(task, analysis, "全部", GENERATED)
    return text, render_report_image(text, analysis, **kwargs)


def _margin_is_blank(image, margin_px):
    """右边距那一条（像素）是否全是页面底色：内容越界或被裁切时会留下墨迹。"""
    page = image.pixel(image.width() - 1, 0)
    return all(image.pixel(x, y) == page
               for x in range(image.width() - margin_px + 2, image.width(), 2)
               for y in range(0, image.height(), 3))


@pytest.fixture
def report_scheme():
    """改"报告配色"配置项，用例结束复位（根 conftest 不认识这一项）。"""
    item = cfg.report_color_scheme
    yield lambda value: setattr(item, 'value', value)
    item.value = item.defaultValue


# ---------------------------------------------------------------- 配色


def test_dark_theme_still_exports_light_report(qapp, db):
    from ui.widgets.analysis.report_sheet import REPORT_MARGIN, render_report_for
    task = seed_task(db)
    analysis = analyse(db, task)
    assert cfg.get(cfg.report_color_scheme) == REPORT_SCHEME_LIGHT

    with theme_applied(Theme.DARK):
        image = render_report_for(task, analysis, GENERATED, "全部")

    assert image.width() == 1600
    assert image.pixel(2, 2) == LIGHT_PAGE
    assert image.pixel(image.width() // 2, image.height() - 3) == LIGHT_PAGE
    # 小倍数图区（图片下部）同样是浅底：卡片、绘图区、网格间距都不是深色
    chart_y = image.height() - REPORT_MARGIN - 150
    gap_x = REPORT_MARGIN + (1600 - 2 * REPORT_MARGIN) // 2
    for x in (REPORT_MARGIN + 200, gap_x, 1600 - REPORT_MARGIN - 200):
        # 取一小段的中位数，避开网格线与曲线
        values = sorted(QColor(image.pixel(x + dx, chart_y + dy)).lightness()
                        for dx in range(0, 30, 3) for dy in range(0, 30, 3))
        assert values[len(values) // 2] > 230, x


def test_follow_theme_scheme_uses_current_theme(qapp, db, report_scheme):
    from ui.widgets.analysis.report_sheet import render_report_for, report_is_dark
    task = seed_task(db)
    analysis = analyse(db, task)
    report_scheme(REPORT_SCHEME_FOLLOW)

    with theme_applied(Theme.DARK):
        assert report_is_dark() is True
        dark = render_report_for(task, analysis, GENERATED, "全部")
    with theme_applied(Theme.LIGHT):
        assert report_is_dark() is False
        light = render_report_for(task, analysis, GENERATED, "全部")

    assert dark.pixel(2, 2) == DARK_PAGE
    assert light.pixel(2, 2) == LIGHT_PAGE
    assert report_is_dark(REPORT_SCHEME_LIGHT) is False


def test_chart_grid_gap_uses_page_color(qapp, db):
    """小倍数图网格的 16 px 间距显式垫页面底色：深色报告里不再是一道浅灰缝。"""
    from ui.widgets.analysis.report_sheet import CHART_GAP, render_charts
    task = seed_task(db)
    analysis = analyse(db, task)

    image = render_charts(analysis, 1000, metrics=[MEM, HANDLES], dark=True)

    gap_x = (1000 - CHART_GAP) // 2 + CHART_GAP // 2
    for y in range(40, image.height() - 10, 20):
        assert image.pixel(gap_x, y) == DARK_PAGE, y


def test_report_chart_ignores_global_theme(qapp):
    """报告图表按给定 palette 取色，界面切主题不影响它（不连 themeChanged）。"""
    from ui.chart_theme import chart_palette
    from ui.widgets.analysis.report_sheet import ReportChartView, report_palette
    light = chart_palette(False)
    chart = ReportChartView(light, report_palette(False))
    try:
        with theme_applied(Theme.DARK):
            chart._on_theme_changed(Theme.DARK)      # 即便被调用也只用自己的 palette
            pen = chart.chart_widget.getAxis('left').textPen()
        assert pen.color().name() == QColor(light['text']).name()
    finally:
        chart.deleteLater()


# ---------------------------------------------------------------- 标题块


def test_long_note_wraps_and_grows_height(qapp, db):
    from ui.widgets.analysis.report_sheet import REPORT_MARGIN, TITLE_PX
    short_task = seed_task(db, note="短备注")
    long_task = seed_task(db, note=LONG_NOTE)

    short_text, short = _render(short_task, analyse(db, short_task))
    long_text, long_image = _render(long_task, analyse(db, long_task))

    assert len(LONG_NOTE) == 120
    assert long_text.heading == f"demo.exe · PID 4321 · {LONG_NOTE}"
    # 多出至少两行标题（24 px × 1.5 行高）；文字没有越过右边距
    assert long_image.height() - short.height() >= 2 * TITLE_PX * 1.5
    assert _margin_is_blank(long_image, REPORT_MARGIN)


# ---------------------------------------------------------------- 区间增量表


def test_round_table_needs_two_rounds(qapp, db):
    three = seed_task(db, markers=THREE_ROUNDS)
    one = seed_task(db, markers=ONE_ROUND)

    text3, image3 = _render(three, analyse(db, three))
    text1, image1 = _render(one, analyse(db, one))

    table = text3.round_table
    assert table is not None
    assert table.headers[:3] == ("区间", "时长", "私有字节 Δ")
    assert table.headers[-1] == "与上轮差"
    assert [row[0] for row in table.rows] == ["基线 → 第 1 轮", "第 1 轮 → 第 2 轮",
                                              "第 2 轮 → 第 3 轮"]
    assert all(row[1] == "10分钟" for row in table.rows)
    assert len(table.headers) == len(table.rows[0]) == 2 + len(FIVE) + 1
    # 第一轮没有上一轮；"与上轮差"列只列关注的（持续上涨）指标
    assert table.rows[0][-1] == "私有字节 --\n句柄数 --"
    assert text1.round_table is None
    # 区间表只在 3 轮时出现：其余区块相同，图片更高
    assert image3.height() > image1.height() + 100


def test_round_table_single_focus_metric_header(qapp, db):
    task = seed_task(db, metrics=(MEM, GDI))
    text, _image = _render(task, analyse(db, task))
    assert text.round_table.headers[-1] == "与上轮差 · 私有字节"
    assert text.round_table.rows[0][-1] == "--"


# ---------------------------------------------------------------- 小倍数图


def test_only_rising_or_fluctuating_metrics_are_charted(qapp, db, monkeypatch):
    import ui.widgets.analysis.report_sheet as sheet
    drawn = []
    original = sheet.draw_snapshot
    monkeypatch.setattr(sheet, 'draw_snapshot',
                        lambda chart, metric, *args, **kw: (drawn.append(metric),
                                                            original(chart, metric, *args, **kw)))
    task = seed_task(db)

    text, _image = _render(task, analyse(db, task))

    assert [row.verdict for row in text.conclusion_rows] == [
        "持续上涨", "持续上涨", "平稳", "平稳", "平稳"]
    assert text.chart_metrics == (MEM, HANDLES)
    assert drawn == [MEM, HANDLES]


def test_all_stable_report_has_no_charts(qapp, db, monkeypatch):
    import ui.widgets.analysis.report_sheet as sheet
    drawn = []
    monkeypatch.setattr(sheet, 'draw_snapshot', lambda chart, metric, *a, **k: drawn.append(metric))
    task = seed_task(db, rising=False)

    text, image = _render(task, analyse(db, task))

    assert {row.verdict for row in text.conclusion_rows} == {"平稳"}
    assert text.chart_metrics == ()
    assert drawn == []
    assert not image.isNull() and image.width() == 1600


# ---------------------------------------------------------------- 尺寸与设备像素比


def test_explicit_device_pixel_ratio_keeps_pixel_width(qapp, db):
    from ui.widgets.analysis.report_sheet import REPORT_MARGIN
    task = seed_task(db)
    analysis = analyse(db, task)

    _text, image = _render(task, analysis, device_pixel_ratio=1.5)

    assert image.width() == 1600
    assert image.devicePixelRatio() == pytest.approx(1.5)
    assert _margin_is_blank(image, int(REPORT_MARGIN * 1.5))


_SCALED_SCRIPT = textwrap.dedent(r"""
    import sys
    sys.path.insert(0, {root!r})
    sys.path.insert(0, {tests!r})
    from PyQt5.QtWidgets import QApplication
    app = QApplication([])
    from data.database import Database
    from test_report_sheet import GENERATED, analyse, seed_task, _margin_is_blank
    from ui.widgets.analysis.report_sheet import REPORT_MARGIN, render_report_for
    db = Database({db!r})
    task = seed_task(db)
    image = render_report_for(task, analyse(db, task), GENERATED, "全部")
    print("RESULT", app.primaryScreen().devicePixelRatio(), image.width(),
          _margin_is_blank(image, REPORT_MARGIN))
""")


def test_qt_scale_factor_keeps_width_1600(tmp_path):
    """QT_SCALE_FACTOR=1.5（相当于 150% 缩放的屏幕）下输出仍是 1600 宽，图表不越界裁切。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = _SCALED_SCRIPT.format(root=root, tests=os.path.join(root, "tests"),
                                 db=str(tmp_path / "scaled.db"))
    env = dict(os.environ, QT_SCALE_FACTOR="1.5", QT_QPA_PLATFORM="offscreen",
               PYTHONIOENCODING="utf-8", PROCESS_MONITOR_DATA_DIR=str(tmp_path / "appdata"))
    proc = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT")]
    assert proc.returncode == 0 and lines, proc.stderr[-2000:]
    _tag, screen_ratio, width, blank = lines[0].split()
    assert float(screen_ratio) == pytest.approx(1.5)
    assert int(width) == 1600
    assert blank == "True"


# ---------------------------------------------------------------- 契约


def test_render_report_for_signature_and_stage_snapshot(qapp, db, tmp_path):
    from core.automation.stage_snapshot import StageAnalysisWorker, range_text
    from core.workers.base import CancelToken
    from ui.widgets.analysis.report_sheet import render_report_for
    assert list(inspect.signature(render_report_for).parameters) == [
        'task', 'analysis', 'generated_at', 'range_desc']

    seeded = seed_task(db)
    worker = StageAnalysisWorker(db, seeded.task_id, str(tmp_path / "snap"))
    task, analysis = worker.execute(CancelToken())
    image = render_report_for(task, analysis, GENERATED, range_text(1))

    assert not image.isNull() and image.width() == 1600
    assert image.save(str(tmp_path / "snap" / "s.png"))


# ---------------------------------------------------------------- 设置


def test_report_color_scheme_config_item(tmp_path):
    import json
    item = cfg.report_color_scheme
    assert item.defaultValue == REPORT_SCHEME_LIGHT
    assert item.validator.options == [REPORT_SCHEME_LIGHT, REPORT_SCHEME_FOLLOW]
    assert (item.group, item.name) == ("Export", "ReportColorScheme")
    assert item.validator.correct("bogus") == REPORT_SCHEME_LIGHT
    path = tmp_path / "c.json"
    try:
        qconfig.load(str(path), cfg)
        cfg.set(item, REPORT_SCHEME_FOLLOW)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["Export"]["ReportColorScheme"] == REPORT_SCHEME_FOLLOW
    finally:
        item.value = item.defaultValue


def test_setting_page_report_color_card(qapp, db, tmp_path):
    from core.monitor_manager import MonitorManager
    from tests.factories import make_setting_page
    from ui.pages.setting_page import REPORT_COLOR_TEXTS, REPORT_COLOR_TITLE
    qconfig.load(str(tmp_path / "card.json"), cfg)
    page = make_setting_page(db, MonitorManager(db=db))
    item = cfg.report_color_scheme
    try:
        card = page.report_color_card
        assert card.titleLabel.text() == REPORT_COLOR_TITLE
        assert [card.comboBox.itemText(i) for i in range(card.comboBox.count())] == list(
            REPORT_COLOR_TEXTS)
        assert card.comboBox.currentIndex() == 0
        assert card in page.export_group.findChildren(type(card))

        card.comboBox.setCurrentIndex(1)
        assert cfg.get(item) == REPORT_SCHEME_FOLLOW
    finally:
        item.value = item.defaultValue
        page.close()
        page.deleteLater()
