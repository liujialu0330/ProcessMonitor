"""
打点回显与迷你图刻度的离屏双主题截图（--visual 才运行，产物要人看）：

- marker_echo：快速打点后的回显提示（带“撤销”按钮），叠在监控卡片之上；
- marker_notice_merge：同一类提示连发 4 次后，最旧的被关掉、最近两条留下、最新一条写“另有 N 条同类提示”；
- marker_tick_hover：鼠标悬停在迷你图的打点刻度上，显示“第 3 轮 · 时间 · 备注”；
- marker_tick_menu：在用户打点刻度上右键，弹出“设为基线 / 重命名 / 删除”。

弹出的提示条是主窗口的子控件，直接随整窗抓图；悬停提示与右键菜单是独立的顶层窗口，抓不进
整窗截图，这里把它们各自抓下来合成到整窗抓图上。断言只管“截出来了、关键控件在”，画得好不好由人看。
"""
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PyQt5.QtCore import QEvent, QPoint
from PyQt5.QtGui import QHelpEvent, QPainter
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QToolTip, QWidget
from qfluentwidgets import RoundMenu, Theme

from core.monitor_manager import MonitorManager
from data.models import DataPoint
from tests.doubles import FakeClock, FakeProcessQuery
from tests.factories import make_monitor_page, make_services
from tests.helpers import stop_and_join
from tests.visual.snapshot import DARK_BACKGROUND, LIGHT_BACKGROUND, process_events, theme_applied
from ui.services import InfoBarNotifier
from utils.metrics import MetricType

SIZE = (960, 520)
T0 = datetime(2026, 11, 7, 10, 0, 0)
PRIVATE = MetricType.MEMORY_PRIVATE
GDI = MetricType.NUM_GDI_OBJECTS
HANDLES = MetricType.NUM_HANDLES


def _value(metric, i):
    if metric == PRIVATE:
        return 524288.0 + i * 410.0
    if metric == GDI:
        return 120.0 + i // 4
    return 812.0 + (i * 3 if i > 14 else 0)


def _build_page(db, manager, clock):
    """一页监控：一个运行中的任务，迷你图上有基线、两轮打点和一个自定义名称的打点。"""
    services = make_services(clock=clock, notifier=InfoBarNotifier(),
                             process_query=FakeProcessQuery([(1001, "demo.exe", 1.5)]))
    page = make_monitor_page(db, manager, services)
    page.resize(*SIZE)
    task_id = manager.create_task(os.getpid(), "demo.exe", [PRIVATE, GDI, HANDLES], 10.0,
                                  note="过夜浸泡")
    card = page.board.card(task_id)
    card.set_started_at(T0)
    for i in range(31):
        moment = T0 + timedelta(seconds=i * 10)
        values = {metric: _value(metric, i) for metric in (PRIVATE, GDI, HANDLES)}
        card.update_values(values, moment)
        card.increment_count()
        db.save_data_points([DataPoint(task_id, moment, value, metric)
                             for metric, value in values.items()])
    manager.add_marker(task_id, "基线", T0 + timedelta(seconds=30), kind="baseline")
    manager.add_marker(task_id, "第 1 轮", T0 + timedelta(seconds=100))
    manager.add_marker(task_id, "建模 200 次", T0 + timedelta(seconds=170))
    manager.add_marker(task_id, "第 3 轮", T0 + timedelta(seconds=240))
    clock.set(T0 + timedelta(seconds=290))
    return page, task_id


class Env:
    def __init__(self, db, qapp):
        self.db = db
        self.managers = []

    def make(self):
        clock = FakeClock(T0)
        manager = MonitorManager(db=self.db, clock=clock)
        self.managers.append(manager)
        page, task_id = _build_page(self.db, manager, clock)
        return page, manager, task_id, clock

    def close(self):
        for manager in self.managers:
            stop_and_join(manager)


@pytest.fixture
def env(qapp, db):
    environment = Env(db, qapp)
    yield environment
    environment.close()


def _capture(env, theme, path, scenario):
    """在 theme 下建页面、执行 scenario(page, manager, task_id, clock)，把整窗与弹出的顶层窗口合成保存。"""
    background = DARK_BACKGROUND if theme == Theme.DARK else LIGHT_BACKGROUND
    with theme_applied(theme):
        host = QWidget()
        host.setStyleSheet(f"background: {background};")
        page, manager, task_id, clock = env.make()
        page.setParent(host)
        host.resize(*SIZE)
        page.resize(*SIZE)
        host.show()
        process_events()
        scenario(page, manager, task_id, clock)
        QTest.qWait(700)           # 提示条的滑入动画
        process_events()
        shot = host.grab()
        painter = QPainter(shot)
        origin = host.mapToGlobal(QPoint(0, 0))
        for top in QApplication.topLevelWidgets():
            kind = top.metaObject().className()
            if top is host or not top.isVisible() or kind not in ("RoundMenu", "QTipLabel"):
                continue
            painter.drawPixmap(top.mapToGlobal(QPoint(0, 0)) - origin, top.grab())
        painter.end()
        path.parent.mkdir(parents=True, exist_ok=True)
        assert shot.save(str(path), "PNG")
        QToolTip.hideText()
        host.hide()
        host.deleteLater()
        process_events()
    return path


def _both(env, visual_out_dir, name, scenario):
    light = _capture(env, Theme.LIGHT, Path(visual_out_dir) / f"{name}_light.png", scenario)
    dark = _capture(env, Theme.DARK, Path(visual_out_dir) / f"{name}_dark.png", scenario)
    assert light.stat().st_size > 5000 and dark.stat().st_size > 5000
    return light, dark


def _tick_global_pos(page, task_id, marker_id):
    spark = page.board.card(task_id).sparkline
    for fraction, tick in spark._tick_fractions():
        if tick.marker.id == marker_id:
            local = QPoint(int(spark._tick_x(fraction)), 12)
            return spark, local, spark.mapToGlobal(local)
    raise AssertionError(marker_id)


def test_marker_echo(env, visual_out_dir):
    def scenario(page, manager, task_id, clock):
        page.presenter.quick_marker()

    _both(env, visual_out_dir, "marker_echo", scenario)


def test_marker_notice_merge(env, visual_out_dir):
    def scenario(page, manager, task_id, clock):
        for _ in range(4):
            clock.advance(2)
            page.presenter.quick_marker()

    _both(env, visual_out_dir, "marker_notice_merge", scenario)


def test_marker_tick_hover(env, visual_out_dir):
    def scenario(page, manager, task_id, clock):
        page.verticalScrollBar().setValue(0)
        process_events()
        marker = manager.list_markers(task_id)[2]          # "建模 200 次"：第 2 轮的刻度，名称是备注
        spark, local, glob = _tick_global_pos(page, task_id, marker.id)
        event = QHelpEvent(QEvent.ToolTip, local, glob)
        QApplication.sendEvent(spark, event)
        process_events()

    _both(env, visual_out_dir, "marker_tick_hover", scenario)


def test_marker_tick_menu(env, visual_out_dir):
    def scenario(page, manager, task_id, clock):
        marker = manager.list_markers(task_id)[1]
        _spark, _local, glob = _tick_global_pos(page, task_id, marker.id)
        page.presenter.on_marker_menu(task_id, marker, glob)
        process_events()

    _both(env, visual_out_dir, "marker_tick_menu", scenario)
