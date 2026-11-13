"""
监控页的打点撤销与迷你图打点刻度（真实 manager + 真实卡片 / 主窗口，不启动采集线程）：
撤销按钮与 Ctrl+Z 作用于库、卡片刻度与基线显示；30 秒时限用假时钟；迷你图刻度的悬停文字、
右键菜单（复用分析页的 MarkerEditFlow）与系统打点只读。
"""
import os
from datetime import datetime, timedelta

import pytest
from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QContextMenuEvent, QHelpEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QToolTip
from qfluentwidgets import RoundMenu

from core.monitor_manager import MonitorManager
from data.models import Marker
from tests.doubles import FakeClock, FakeProcessQuery
from tests.factories import make_main_window, make_monitor_page, make_services
from tests.helpers import dispose_widget, stop_and_join
from ui.components.sparkline import SparklineWidget
from ui.presenters.marker_feedback import UNDO_EXPIRED_TEXT
from ui.services import EVENT_MARKER_ADDED, EVENT_MARKER_UNDONE
from ui.widgets.analysis.marker_menu import (
    ADD_TEXT, BASELINE_TEXT, DELETE_TEXT, RENAME_TEXT, MarkerEditFlow,
)
from utils.metrics import MetricType

T0 = datetime(2026, 11, 6, 10, 20, 0)


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def manager(db, qapp, clock):
    mgr = MonitorManager(db=db, clock=clock)
    yield mgr
    stop_and_join(mgr)


@pytest.fixture
def services(clock):
    return make_services(clock=clock, process_query=FakeProcessQuery([(1001, "a.exe", 1.5)]))


@pytest.fixture
def page(qapp, db, manager, services):
    widget = make_monitor_page(db, manager, services)
    widget.resize(900, 700)
    yield widget
    widget.close()


@pytest.fixture
def task_id(manager, page):
    task = manager.create_task(
        os.getpid(), "demo.exe", [MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS], 1.0)
    assert task
    return task


def _feed_samples(page, task_id, count=21):
    """给卡片的迷你图喂 count 个每秒一次的采集点（打点刻度要靠采样时间定位）。"""
    card = page.board.card(task_id)
    for i in range(count):
        card.update_values({MetricType.MEMORY_PRIVATE: 100000.0 + i,
                            MetricType.NUM_GDI_OBJECTS: 10.0}, T0 + timedelta(seconds=i))
    return card


def _labels(manager, task_id):
    return [(m.label, m.kind) for m in manager.list_markers(task_id)]


# ================================================================ 撤销

def test_undo_button_removes_the_marker_and_refreshes_the_card(
        page, manager, services, clock, task_id):
    presenter = page.presenter
    card = _feed_samples(page, task_id)
    presenter.quick_marker()
    clock.advance(5)
    presenter.quick_marker()
    assert _labels(manager, task_id) == [("基线", "baseline"), ("第 1 轮", "user")]
    assert len(card.sparkline.tick_positions()) == 2
    notice = services.notifier.last("success")
    assert (notice.event, notice.key) == (EVENT_MARKER_ADDED, EVENT_MARKER_ADDED)

    notice.action.callback()

    assert _labels(manager, task_id) == [("基线", "baseline")]
    assert manager.db.markers.list(task_id) == manager.list_markers(task_id)
    assert manager.next_marker_label(task_id) == "第 1 轮"
    assert len(card.sparkline.tick_positions()) == 1        # 卡片随 markers_changed 刷新
    assert notice.bar.closed
    undone = services.notifier.last("success")
    assert (undone.event, undone.title) == (EVENT_MARKER_UNDONE, "已撤销打点")


def test_undone_baseline_clears_the_card_baseline(page, manager, services, task_id):
    page.presenter.quick_marker()
    card = page.board.card(task_id)
    assert card.baseline is not None

    services.notifier.last("success").action.callback()

    assert manager.get_baseline(task_id) is None and card.baseline is None
    assert manager.next_marker_label(task_id) == "基线"


def test_undone_new_baseline_gives_the_card_the_previous_baseline_back(
        page, manager, services, clock, task_id):
    old = manager.add_marker(task_id, "基线", T0, kind="baseline")
    clock.advance(10)
    page.presenter.quick_marker()                       # "第 1 轮"
    clock.advance(10)
    new = manager.add_marker(task_id, "重新预热", clock.now(), kind="baseline")
    card = page.board.card(task_id)
    assert card.baseline.timestamp == new.timestamp

    outcome = manager.undo_last_marker()

    assert outcome.ok
    assert manager.get_baseline(task_id).id == old.id
    assert card.baseline.timestamp == old.timestamp
    assert manager.next_marker_label(task_id) == "第 2 轮"


def test_undo_after_thirty_seconds_is_refused_with_a_message(
        page, manager, services, clock, task_id):
    page.presenter.quick_marker()
    notice = services.notifier.last("success")
    clock.advance(31)

    notice.action.callback()

    assert len(manager.list_markers(task_id)) == 1
    warned = services.notifier.last("warning")
    assert warned.content == UNDO_EXPIRED_TEXT == "已超过 30 秒，无法撤销"


def test_auto_markers_are_undoable_too_and_share_the_numbering(
        page, manager, services, clock, task_id):
    """自动打点（定时 / 日志触发）与手动同样经 manager.add_marker：Ctrl+Z 能撤销，编号同一序列。"""
    page.presenter.quick_marker()                                  # 基线
    label = manager.next_marker_label(task_id)
    manager.add_marker(task_id, label, clock.now())                # 自动打点写的"第 1 轮"
    assert manager.next_marker_label(task_id) == "第 2 轮"

    page.presenter.undo_last_marker()

    assert _labels(manager, task_id) == [("基线", "baseline")]
    assert manager.next_marker_label(task_id) == "第 1 轮"
    assert services.notifier.last("success").content == "第 1 轮（demo.exe）"


@pytest.fixture
def window(qapp, db, manager, services):
    widget = make_main_window(db, manager, services)
    widget.resize(1200, 800)
    widget.show()
    yield widget
    # 不能 close()：真主窗口的 closeEvent 走真退出路径并调 QApplication.quit()，测试进程没有主事件
    # 循环，Qt 会一直记着"已退出"，之后同进程里所有局部 QEventLoop 立即返回（waitSignal 全超时）
    dispose_widget(widget)


def test_ctrl_z_in_the_main_window_undoes_the_latest_marker(
        window, manager, services, clock):
    task_id = manager.create_task(os.getpid(), "demo.exe", [MetricType.MEMORY_PRIVATE], 1.0)
    window.monitor_page.presenter.quick_marker()
    clock.advance(3)
    window.monitor_page.presenter.quick_marker()
    assert len(manager.list_markers(task_id)) == 2

    shortcut = window.undo_marker_shortcut
    assert shortcut.key().toString() == "Ctrl+Z" and shortcut.context() == Qt.WindowShortcut
    window.activateWindow()
    QApplication.setActiveWindow(window)
    QTest.keyClick(window, Qt.Key_Z, Qt.ControlModifier)

    assert [m.label for m in manager.list_markers(task_id)] == ["基线"]
    assert services.notifier.last("success").title == "已撤销打点"


# ================================================================ 迷你图刻度：悬停

def _markers():
    return [
        Marker("t", T0 + timedelta(seconds=2), "基线", 1, "baseline"),
        Marker("t", T0 + timedelta(seconds=6), "第 1 轮", 2, "user"),
        Marker("t", T0 + timedelta(seconds=10), "第 2 轮", 3, "user"),
        Marker("t", T0 + timedelta(seconds=14, microseconds=0), "建模 200 次", 4, "user"),
        Marker("t", T0 + timedelta(seconds=16), "异常退出 0xC0000005", 5, "crash"),
        Marker("t", T0 + timedelta(seconds=18), "暂停", 6, "pause"),
    ]


@pytest.fixture
def spark(qapp):
    widget = SparklineWidget()
    widget.resize(400, 36)
    for i in range(21):
        widget.append(float(i), T0 + timedelta(seconds=i))
    widget.set_markers(_markers())
    widget.show()
    yield widget
    widget.close()


def _x_of(spark, marker_id):
    for fraction, tick in spark._tick_fractions():
        if tick.marker.id == marker_id:
            return spark._tick_x(fraction)
    raise AssertionError(marker_id)


def test_hover_text_has_round_time_and_remark(spark):
    clock = lambda s: (T0 + timedelta(seconds=s)).strftime("%H:%M:%S")      # noqa: E731
    assert spark.tick_at(_x_of(spark, 1)).text == f"基线 · {clock(2)}"
    assert spark.tick_at(_x_of(spark, 2)).text == f"第 1 轮 · {clock(6)}"      # 默认名不重复
    assert spark.tick_at(_x_of(spark, 3)).text == f"第 2 轮 · {clock(10)}"
    # 自定义名称作为备注放在最后；序号按用户打点的先后数
    assert spark.tick_at(_x_of(spark, 4)).text == f"第 3 轮 · {clock(14)} · 建模 200 次"
    assert spark.tick_at(_x_of(spark, 5)).text == f"异常退出 0xC0000005 · {clock(16)}"


def test_hover_finds_the_nearest_tick_within_a_few_pixels_only(spark):
    x = _x_of(spark, 2)
    assert spark.tick_at(x + 3).marker.id == 2
    assert spark.tick_at(x - 4).marker.id == 2
    assert spark.tick_at(x + 12) is None
    assert spark.tick_at(1.0) is None


def test_pause_markers_are_not_ticks(spark):
    assert [t.marker.kind for _f, t in spark._tick_fractions()] == [
        "baseline", "user", "user", "user", "crash"]


def test_tooltip_event_shows_the_tick_text(spark):
    x = int(_x_of(spark, 2))
    event = QHelpEvent(QEvent.ToolTip, QPoint(x, 10), spark.mapToGlobal(QPoint(x, 10)))
    assert QApplication.sendEvent(spark, event)
    assert QToolTip.text().startswith("第 1 轮 · ")


def test_tooltip_elsewhere_falls_back_to_the_widget_tooltip(spark):
    spark.setToolTip("任务开始至今的趋势")
    QToolTip.hideText()
    event = QHelpEvent(QEvent.ToolTip, QPoint(1, 10), spark.mapToGlobal(QPoint(1, 10)))
    QApplication.sendEvent(spark, event)
    assert QToolTip.text() == "任务开始至今的趋势"


# ================================================================ 迷你图刻度：右键菜单

def _right_click(spark, x):
    got = []
    spark.marker_menu_requested.connect(lambda marker, pos: got.append(marker))
    event = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(int(x), 10),
                              spark.mapToGlobal(QPoint(int(x), 10)))
    QApplication.sendEvent(spark, event)
    return got


def test_right_click_on_user_and_baseline_ticks_requests_the_menu(spark):
    assert [m.id for m in _right_click(spark, _x_of(spark, 2))] == [2]
    assert [m.id for m in _right_click(spark, _x_of(spark, 1))][-1] == 1


def test_system_ticks_and_blank_space_have_no_menu(spark):
    assert _right_click(spark, _x_of(spark, 5)) == []        # 异常退出：只能悬停
    assert _right_click(spark, 1.0) == []


def test_card_relays_the_menu_request_with_its_task_id(page, manager, task_id):
    card = _feed_samples(page, task_id)
    marker = manager.add_marker(task_id, "基线", T0 + timedelta(seconds=5), kind="baseline")
    got = []
    card.marker_menu_requested.connect(lambda *args: got.append(args))

    card.sparkline.resize(400, 36)
    x = card.sparkline._tick_x(card.sparkline._tick_fractions()[0][0])
    assert _right_click(card.sparkline, x) == [marker]
    assert [(tid, m) for tid, m, _pos in got] == [(task_id, marker)]


@pytest.fixture
def captured_menu(monkeypatch):
    """RoundMenu.exec 会阻塞：换成记录菜单项文字、不弹出。"""
    shown = []

    def fake_exec(self, pos, *args, **kwargs):
        shown.append([a.text() for a in self.actions()])

    monkeypatch.setattr(RoundMenu, "exec", fake_exec)
    return shown


def test_menu_offers_rename_delete_and_baseline_without_add(
        page, manager, task_id, captured_menu):
    manager.add_marker(task_id, "基线", T0, kind="baseline")
    marker = manager.add_marker(task_id, "第 1 轮", T0 + timedelta(seconds=5))

    page.presenter.on_marker_menu(task_id, marker, QPoint(0, 0))

    assert captured_menu == [[BASELINE_TEXT, RENAME_TEXT, DELETE_TEXT]]
    assert ADD_TEXT not in captured_menu[0]


def test_menu_flow_is_the_analysis_page_flow_and_edits_through_the_manager(
        page, manager, services, task_id):
    manager.add_marker(task_id, "基线", T0, kind="baseline")
    marker = manager.add_marker(task_id, "第 1 轮", T0 + timedelta(seconds=5))
    flow = page.presenter.marker_flow(task_id)
    assert isinstance(flow, MarkerEditFlow)
    items = {text: (run, enabled) for text, run, enabled in flow.items(0.0, marker)}
    assert items[BASELINE_TEXT][1] and items[RENAME_TEXT][1] and items[DELETE_TEXT][1]

    services.dialogs.push("ask_marker_label", "热身结束")
    assert items[RENAME_TEXT][0]()
    assert [m.label for m in manager.list_markers(task_id)] == ["基线", "热身结束"]

    assert items[BASELINE_TEXT][0]()
    assert manager.get_baseline(task_id).id == marker.id
    assert page.board.card(task_id).baseline.timestamp == marker.timestamp

    services.dialogs.push("confirm", True)
    assert items[DELETE_TEXT][0]()
    # 删掉的正是刚设为基线的那个：任务回到"无基线"，卡片回到"比开始时"
    assert [m.label for m in manager.list_markers(task_id)] == ["基线"]
    assert manager.get_baseline(task_id) is None
    assert page.board.card(task_id).baseline is None


def test_menu_on_a_system_marker_has_every_edit_disabled(page, manager, task_id):
    crash = manager.add_marker(task_id, "异常退出 0xC0000005", T0, kind="crash")
    flow = page.presenter.marker_flow(task_id)
    enabled = {text: ok for text, _run, ok in flow.items(0.0, crash)}
    assert not (enabled[BASELINE_TEXT] or enabled[RENAME_TEXT] or enabled[DELETE_TEXT])


def test_menu_is_refused_in_read_only_mode(page, manager, services, task_id, captured_menu):
    marker = manager.add_marker(task_id, "第 1 轮", T0)
    page.presenter.set_read_only("数据库由更高版本创建")
    page.presenter.on_marker_menu(task_id, marker, QPoint(0, 0))
    assert captured_menu == []
    assert services.notifier.last("warning")


def test_menu_flow_is_kept_while_the_menu_is_open_and_dropped_with_the_card(
        page, manager, task_id):
    """菜单是非阻塞弹出的，动作触发时流程对象必须还在：按任务缓存，卡片关闭时释放。"""
    presenter = page.presenter
    flow = presenter.marker_flow(task_id)
    assert presenter.marker_flow(task_id) is flow
    presenter.on_close(task_id)
    assert task_id not in presenter._marker_flows
