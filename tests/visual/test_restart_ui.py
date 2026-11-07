"""
被测进程退出后续接与异常退出的离屏双主题截图（--visual 才运行，产物要人看）：

- restart_cards：任务面板里"等待重启（第 2/3 次）"的卡片、刚续接成功（PID 已换、迷你图带异常退出与
  重启点划线）的卡片、因访问冲突异常结束的已结束行、普通退出的已结束行与同名进程卡片对照；
- restart_analysis：分析页（1280×820 主窗口）趋势图上的异常退出 / 重启 / 续采三种系统打点，
  结论条的统计依据带"仅统计第 N 次启动后的数据"；
- restart_analysis_full：同一页拉高后的整页长图，方便看焦点图下方的小图；
- recover_dialog：工具异常退出后重新启动时的"继续监控"对话框，叠在 1280×820 的主窗口之上
  （对话框的半透明窗口抓不进整窗截图，这里把它的遮罩与对话框本体分别抓下来合成到主窗口抓图上）。

分析页的数据直接写进临时库：私有字节在第一段持续上涨、异常退出，隔 90 秒重启后第二段平稳，
随后工具被强杀、5 分钟后重新启动并续采。断言只管"截出来了、尺寸对、关键控件在"，画得好不好由人看。
"""
import dataclasses
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtWidgets import QWidget

from core.contracts import StopReason, TaskInfo, TaskStatus
from tests.doubles import FakeProcessQuery, ScriptedDialogs
from tests.helpers import process_events_until
from tests.visual.snapshot import DEFAULT_SIZE
from tests.visual.test_pages_dual_theme import _FakeHotkeyBackend, _assert_pair
from utils.metrics import MetricType

SIZE = DEFAULT_SIZE
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
INTERVAL = 30.0
ACCESS_VIOLATION = 0xC0000005


# ---------------------------------------------------------------- 卡片


def _info(task_id, name, pid, **kwargs):
    base = dict(task_id=task_id, pid=pid, process_name=name, metric_types=(MEM, HANDLES),
                interval=2.0, note="过夜浸泡", status=TaskStatus.RUNNING, start_time=None,
                end_time=None, create_time=None, is_paused=False, is_stopping=False)
    base.update(kwargs)
    return TaskInfo(**base)


def _cards_builder():
    from core.group_aggregator import MemberSnapshot
    from data.models import Marker
    from ui.widgets.monitor.task_board import TaskBoard

    def feed(card, now, scale=1.0):
        card.set_started_at(now - timedelta(minutes=95))
        for i, minutes in enumerate((90, 60, 30, 10, 0)):
            card.update_values({MEM: (524288.0 + i * 4096) * scale, HANDLES: 812.0 + i * 3},
                               now - timedelta(minutes=minutes))
            card.increment_count()

    def build():
        now = datetime(2026, 11, 7, 10, 30, 0)
        board = TaskBoard()

        waiting = board.create_card(_info(
            "w1", "demo.exe", 4321, waiting_restart=True, restart_count=1, restart_limit=3,
            exit_code=ACCESS_VIOLATION))
        feed(waiting, now)

        restarted = board.create_card(_info("r1", "launcher.exe", 5566))
        feed(restarted, now)
        restarted.set_pid(5566)
        restarted.set_markers([
            Marker("r1", now - timedelta(minutes=60), "异常退出 0xC0000005（访问冲突）", 1, "crash"),
            Marker("r1", now - timedelta(minutes=58), "第 2 次启动 PID 5566", 2, "restart"),
            Marker("r1", now - timedelta(minutes=20), "工具重启后继续监控", 3, "recover"),
            Marker("r1", now - timedelta(minutes=80), "基线", 4, "baseline"),
        ])

        crashed = board.create_card(_info("c1", "demo.exe", 4321, note="过夜浸泡"))
        feed(crashed, now)
        board.finish_card("c1", StopReason.CRASHED, now, ACCESS_VIOLATION)

        exited = board.create_card(_info("e1", "worker.exe", 2200, note=""))
        feed(exited, now)
        board.finish_card("e1", StopReason.PROCESS_EXITED, now - timedelta(minutes=3))

        group = board.create_card(_info("g1", "chrome.exe", 0, target_kind='name',
                                        metric_types=(MEM,)))
        feed(group, now)
        group.set_members([MemberSnapshot('chrome.exe', "chrome.exe", 37)], False)
        board.set_counts(3, 5)
        return board
    return build


def test_restart_and_crash_cards(visual_snapshot):
    light, dark = visual_snapshot(_cards_builder(), "restart_cards", (1180, 1100))
    for path in (light, dark):
        assert path.is_file()


# ---------------------------------------------------------------- 分析页


def _seed_restarted_task(db) -> str:
    """第一段（60 点）持续上涨后异常退出；90 秒后重启，第二段（60 点）平稳；工具被强杀、5 分钟后
    续采，再 40 点。"""
    from data.models import DataPoint, Marker, MonitorTask

    start = (datetime.now() - timedelta(hours=1, minutes=40)).replace(microsecond=0)
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
                       metric_types=[MEM, HANDLES], interval=INTERVAL, start_time=start,
                       end_time=None, status='running', note="过夜浸泡")
    assert db.save_task(task)
    points, markers = [], []
    cursor = start

    def segment(count, memory_at, handles):
        nonlocal cursor
        for i in range(count):
            points.append(DataPoint(task.task_id, cursor, memory_at(i), MEM))
            points.append(DataPoint(task.task_id, cursor, handles + (i % 3), HANDLES))
            cursor += timedelta(seconds=INTERVAL)

    segment(60, lambda i: 400000.0 + i * 900 + (i % 5) * 40, 800.0)
    markers.append(Marker(task.task_id, cursor, "异常退出 0xC0000005（访问冲突）", 1, 'crash'))
    cursor += timedelta(seconds=90)
    markers.append(Marker(task.task_id, cursor - timedelta(seconds=5),
                          "第 2 次启动 PID 5566", 2, 'restart'))
    segment(60, lambda i: 520000.0 + (i % 7) * 60, 900.0)
    cursor += timedelta(minutes=5)
    markers.append(Marker(task.task_id, cursor - timedelta(seconds=5),
                          "工具重启后继续监控", 3, 'recover'))
    segment(40, lambda i: 520000.0 + (i % 7) * 60 + i * 20, 900.0)
    db.save_data_points(points)
    for marker in markers:
        db.markers.add(task.task_id, marker.timestamp, marker.label, marker.kind)
    return task.task_id


@pytest.fixture
def context(qapp, visual_data_dir):
    from app.bootstrap import build_context
    from ui.hotkey import MarkerHotkey

    ctx = build_context(visual_data_dir, retention_days=0)
    services = dataclasses.replace(
        ctx.services, process_query=FakeProcessQuery([(1001, "alpha.exe", 1001.5)]),
        dialogs=ScriptedDialogs(ask_close_action='quit'))
    hotkey = MarkerHotkey(_FakeHotkeyBackend())
    hotkey.bind("Ctrl+Alt+M")
    task_id = _seed_restarted_task(ctx.db)
    yield ctx, services, hotkey, task_id
    process_events_until(lambda: not services.threads.entries(), timeout=10)
    ctx.manager.stop_all_tasks()
    ctx.manager.join_all(3000)
    ctx.db.close()


def _analysis_builder(context):
    ctx, services, hotkey, task_id = context

    def build():
        from ui.main_window import MainWindow
        window = MainWindow(ctx.db, ctx.manager, services, hotkey=hotkey)
        window.about_group.check_update = lambda *args, **kwargs: None
        window.switchTo(window.analysis_page)
        return window

    def settle(window):
        page = window.analysis_page
        page.select_task(task_id)
        vm = page.vm
        assert process_events_until(lambda: vm.last_analysis is not None, timeout=15)
        assert process_events_until(lambda: not services.threads.entries(), timeout=10)
        process_events_until(lambda: False, timeout=0.3)
        chart = page.trend_panel.focus_chart
        kinds = [kind for kind, _badge, _line in chart.marker_items()]
        assert kinds == ['crash', 'restart', 'recover'], kinds
        assert "仅统计第 2 次启动后的数据" in page.verdict_banner.basis_label.text()

    return build, settle


def test_analysis_chart_shows_system_event_markers(visual_snapshot, context):
    build, settle = _analysis_builder(context)
    light, dark = visual_snapshot(build, "restart_analysis", SIZE, settle=settle)
    _assert_pair(light, dark)


def test_analysis_full_page(visual_snapshot, context):
    build, settle = _analysis_builder(context)
    light, dark = visual_snapshot(build, "restart_analysis_full", (1280, 1500), settle=settle)
    assert light.is_file() and dark.is_file()


# ---------------------------------------------------------------- 继续监控对话框


def _recover_builder(context):
    from PyQt5.QtGui import QColor, QPainter, QPixmap
    from PyQt5.QtWidgets import QApplication, QLabel
    from qfluentwidgets import isDarkTheme

    from core.session_recovery import RecoveryCandidate
    from ui.dialogs.recover_dialog import RecoverDialog
    ctx, services, hotkey, _task_id = context

    def build():
        from ui.main_window import MainWindow
        window = MainWindow(ctx.db, ctx.manager, services, hotkey=hotkey)
        window.about_group.check_update = lambda *args, **kwargs: None
        window.resize(*SIZE)
        window.show()
        QApplication.processEvents()
        now = datetime(2026, 11, 7, 10, 30, 0)
        candidates = [
            RecoveryCandidate("a", "demo.exe", 4321, 'pid', now - timedelta(hours=3),
                              now - timedelta(minutes=4), "过夜浸泡"),
            RecoveryCandidate("b", "app.exe", 1002, 'tree', now - timedelta(hours=2),
                              now - timedelta(minutes=4)),
            RecoveryCandidate("c", "chrome.exe", 0, 'name', now - timedelta(hours=5),
                              now - timedelta(minutes=4), "长时间运行后内存持续上涨的现场排查任务"),
        ]
        dialog = RecoverDialog(candidates, window)
        dialog.show()
        QApplication.processEvents()
        QApplication.processEvents()
        dialog_rect = dialog.widget.geometry()
        assert dialog_rect.width() >= 520
        assert 0 <= dialog_rect.left() and dialog_rect.right() <= SIZE[0]
        assert 0 <= dialog_rect.top() and dialog_rect.bottom() <= SIZE[1]
        assert dialog.yesButton.isEnabled() and dialog.list_widget.count() == 3
        assert dialog.selected_task_ids() == ["a", "b", "c"]
        composite = QPixmap(window.grab())
        painter = QPainter(composite)
        mask = 0 if isDarkTheme() else 255
        painter.fillRect(composite.rect(), QColor(mask, mask, mask, int(255 * 0.6)))
        painter.drawPixmap(dialog.widget.pos(), dialog.widget.grab())
        painter.end()
        dialog.hide()
        label = QLabel()
        label.setPixmap(composite)
        label.keep = (window, dialog)
        return label
    return build


def test_recover_dialog_in_1280x820_window(visual_snapshot, context):
    light, dark = visual_snapshot(_recover_builder(context), "recover_dialog", SIZE)
    _assert_pair(light, dark)
