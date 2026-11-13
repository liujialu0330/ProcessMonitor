"""
存储守卫的界面提示：StorageNotices（信号 → 提示条 / 卡片标注 / 设置页警告）、任务卡片的暂停原因、
主窗口接入（attach_storage_guard）与真退出前停守卫。

守卫本身的逻辑见 test_storage_guard.py；这里用假守卫（只有同名信号）验证提示的文案、时长、
关闭与只提示一次，再用真实 MainWindow + 真实 StorageGuard（假时钟、假磁盘）走通整条链路。
"""
import os
from collections import namedtuple
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from core.monitor_manager import MonitorManager
from core.storage_guard import GB, MB, StorageGuard
from data.database import Database
from tests.doubles import FakePreferences, RecordingNotifier, ScriptedDialogs
from tests.factories import make_main_window, make_services
from tests.helpers import dispose_widget, stop_and_join
from ui import storage_notices as notices
from ui.components.task_card import TaskCard
from ui.services import EVENT_STORAGE_INTEGRITY, EVENT_STORAGE_SPACE, ManagerTaskStatus
from ui.storage_notices import StorageNotices
from utils.metrics import MetricType


class FakeGuard(QObject):
    low_space = pyqtSignal(object)
    space_critical = pyqtSignal(object, object)
    space_recovered = pyqtSignal(object)
    space_paused_changed = pyqtSignal(object)
    integrity_problem = pyqtSignal(str)

    def __init__(self, db_path=r"D:\data\monitor.db"):
        super().__init__()
        self.db_path = db_path
        self.paused = frozenset()
        self.integrity_summary = None

    def paused_task_ids(self):
        return self.paused


@pytest.fixture
def parts(qapp):
    guard = FakeGuard()
    notifier = RecordingNotifier()
    marks, integrity = [], []
    anchor = QObject()
    n = StorageNotices(guard, notifier, anchor, set_pause_marks=marks.append,
                       show_integrity=integrity.append)

    class P:
        pass
    p = P()
    p.guard, p.notifier, p.marks, p.integrity, p.notices = (
        guard, notifier, marks, integrity, n)
    p.anchor = anchor
    return p


# ========== 文案 ==========

def test_text_helpers():
    assert "512.0 MB" in notices.low_space_text(512 * MB, r"D:\data\monitor.db")
    assert r"D:\data\monitor.db" in notices.low_space_text(512 * MB, r"D:\data\monitor.db")
    text = notices.critical_text(150 * MB, r"D:\data\monitor.db", 3)
    assert "150.0 MB" in text and r"D:\data\monitor.db" in text and "已暂停 3 个" in text
    assert "手动" in text
    assert "没有需要暂停" in notices.critical_text(100 * MB, "x", 0)
    assert "不足 200 MB）" in text and "200.0" not in text          # 阈值是整数写法
    assert "低于 200 MB 时" in notices.low_space_text(512 * MB, "x")
    assert notices.LOW_SPACE_TITLE == "磁盘空间不足 1 GB"
    assert "不会自动恢复" in notices.recovered_text(1 * GB, 2)
    assert "后台检查" in notices.integrity_text("Page 5 bad")
    assert "复制诊断信息" in notices.integrity_text("x") and "联系作者" in notices.integrity_card_content("x")


def test_format_threshold_is_integer_for_thresholds():
    assert notices.format_threshold(200 * MB) == "200 MB"
    assert notices.format_threshold(300 * MB) == "300 MB"
    assert notices.format_threshold(1 * GB) == "1 GB"
    assert notices.format_threshold(2 * GB) == "2 GB"
    assert notices.format_threshold(1536 * MB) == "1536 MB"
    assert notices.format_threshold(1500) == "1.5 KB"                # 非整 MB 回落到实测值格式


# ========== 提示条 ==========

def test_low_space_is_a_timed_warning(parts):
    parts.guard.low_space.emit(900 * MB)
    n = parts.notifier.last("warning")
    assert n.title == notices.LOW_SPACE_TITLE and "900.0 MB" in n.content
    assert n.effective_duration == notices.LOW_SPACE_DURATION_MS
    assert n.parent is parts.anchor


def test_critical_is_a_persistent_warning_with_free_space_and_db_path(parts):
    parts.guard.paused = frozenset({"a", "b"})
    parts.guard.space_critical.emit(150 * MB, ("a", "b"))
    n = parts.notifier.last("warning")
    assert n.title == notices.CRITICAL_TITLE
    assert n.effective_duration == -1                       # 常驻，不自动消失
    assert "150.0 MB" in n.content and r"D:\data\monitor.db" in n.content
    assert "已暂停 2 个" in n.content


def test_recovery_replaces_the_persistent_bar_and_says_it_does_not_resume(parts):
    parts.guard.paused = frozenset({"a"})
    parts.guard.space_critical.emit(150 * MB, ("a",))
    critical_bar = parts.notifier.last("warning").bar
    parts.guard.space_recovered.emit(2 * GB)
    assert critical_bar.closed                              # 原来的常驻提示被关掉
    n = parts.notifier.last("success")
    assert n.title == notices.RECOVERED_TITLE == "空间已恢复，可继续监控"
    assert "不会自动恢复" in n.content and "2.0 GB" in n.content
    assert n.effective_duration == -1


def test_new_critical_closes_the_recovery_bar(parts):
    parts.guard.space_critical.emit(150 * MB, ())
    parts.guard.space_recovered.emit(1 * GB)
    recovery = parts.notifier.last("success").bar
    parts.guard.space_critical.emit(100 * MB, ())
    assert recovery.closed
    assert len(parts.notifier.messages("warning")) == 2


def test_repeated_critical_does_not_stack_bars(parts):
    parts.guard.space_critical.emit(150 * MB, ())
    first = parts.notifier.last("warning").bar
    parts.guard.space_critical.emit(120 * MB, ())
    assert first.closed


def test_critical_closes_the_low_space_bar(parts):
    parts.guard.low_space.emit(900 * MB)
    low_bar = parts.notifier.last("warning").bar
    assert not low_bar.closed
    parts.guard.space_critical.emit(150 * MB, ())
    assert low_bar.closed                                   # 不叠放、不挡页面标题
    assert parts.notifier.last("warning").title == notices.CRITICAL_TITLE


def test_recovered_closes_the_critical_bar_leaving_one_space_bar(parts):
    parts.guard.low_space.emit(900 * MB)
    parts.guard.space_critical.emit(150 * MB, ())
    parts.guard.space_recovered.emit(1 * GB)
    bars = {n.title: n.bar for n in parts.notifier.messages()}
    assert bars[notices.LOW_SPACE_TITLE].closed
    assert bars[notices.CRITICAL_TITLE].closed
    assert not bars[notices.RECOVERED_TITLE].closed
    assert [n for n in parts.notifier.messages() if not n.bar.closed] == [
        parts.notifier.last("success")]                     # 同一时刻只剩一条空间提示


def test_low_space_after_recovery_replaces_the_recovery_bar(parts):
    parts.guard.space_critical.emit(150 * MB, ())
    parts.guard.space_recovered.emit(2 * GB)
    recovery = parts.notifier.last("success").bar
    parts.guard.low_space.emit(900 * MB)
    assert recovery.closed
    assert [n.title for n in parts.notifier.messages() if not n.bar.closed] == [
        notices.LOW_SPACE_TITLE]


def test_all_space_bars_share_one_parent(parts):
    parts.guard.low_space.emit(900 * MB)
    parts.guard.space_critical.emit(150 * MB, ())
    parts.guard.space_recovered.emit(1 * GB)
    assert [n.parent for n in parts.notifier.messages()] == [parts.anchor] * 3


def test_pause_marks_are_forwarded(parts):
    parts.guard.space_paused_changed.emit(frozenset({"a", "b"}))
    parts.guard.space_paused_changed.emit(frozenset())
    assert parts.marks == [frozenset({"a", "b"}), frozenset()]


def test_space_notices_go_through_route_with_storage_events(parts):
    """渠道（提示条 / 窗口在后台时的托盘气泡）由 Notifier 按窗口状态选，这里只管事件名"""
    parts.guard.low_space.emit(900 * MB)
    parts.guard.space_critical.emit(150 * MB, ())
    parts.guard.space_recovered.emit(1 * GB)
    parts.guard.integrity_problem.emit("x")
    assert [n.event for n in parts.notifier.messages()] == [
        EVENT_STORAGE_SPACE] * 3 + [EVENT_STORAGE_INTEGRITY]


def test_space_bar_semantics_hold_when_the_new_notice_has_no_bar(qapp):
    """窗口在后台时不常驻的提示只发气泡、没有提示条（route 返回 None）：上一条空间提示
    仍要先关掉，不留下过期的"严重不足"。"""
    class BubbleOnly(RecordingNotifier):
        def route(self, event, level, title, content, **kwargs):
            bar = super().route(event, level, title, content, **kwargs)
            return None if kwargs.get('duration', 0) >= 0 else bar
    guard, notifier = FakeGuard(), BubbleOnly()
    keep = StorageNotices(guard, notifier, QObject())
    guard.space_critical.emit(150 * MB, ())
    critical = notifier.last("warning").bar
    guard.low_space.emit(900 * MB)
    assert critical.closed
    del keep


# ========== 完整性警告：主窗口与设置页各一次 ==========

def test_integrity_problem_shows_once_in_window_and_settings(parts):
    parts.guard.integrity_problem.emit("Page 5: btree error")
    parts.guard.integrity_problem.emit("Page 5: btree error")
    warnings = parts.notifier.messages("warning")
    assert len(warnings) == 1
    assert warnings[0].title == notices.INTEGRITY_TITLE
    assert warnings[0].effective_duration == -1
    assert "复制诊断信息" in warnings[0].content and "联系作者" in warnings[0].content
    assert parts.integrity == ["Page 5: btree error"]


def test_integrity_result_that_predates_the_window_is_shown_on_attach(qapp):
    guard = FakeGuard()
    guard.integrity_summary = "early problem"
    notifier = RecordingNotifier()
    shown = []
    StorageNotices(guard, notifier, QObject(), show_integrity=shown.append)
    assert shown == ["early problem"] and len(notifier.messages("warning")) == 1


# ========== 任务卡片：暂停原因 ==========

@pytest.fixture
def card(qapp):
    widget = TaskCard("t1", "demo.exe", 1234, [MetricType.MEMORY_PRIVATE], interval=1.0)
    widget.show()
    yield widget
    dispose_widget(widget)


def test_card_marks_space_pause_only_while_paused(card):
    card.set_pause_reason(notices.SPACE_PAUSE_REASON)      # 没在暂停：忽略
    assert card.pause_reason is None and "磁盘" not in card.status_label.text()
    card.set_paused(True)
    assert card.status_label.text() == "● 已暂停"            # 用户自己暂停：不带原因
    card.set_pause_reason(notices.SPACE_PAUSE_REASON)
    assert card.status_label.text() == "● 已暂停（磁盘空间不足）"
    assert "磁盘空间不足" in card.pause_button.toolTip()
    assert card.pause_button.text() == "恢复"


def test_card_clears_the_mark_on_resume_and_on_explicit_clear(card):
    card.set_paused(True)
    card.set_pause_reason(notices.SPACE_PAUSE_REASON)
    card.set_pause_reason(None)
    assert card.status_label.text() == "● 已暂停"
    card.set_pause_reason(notices.SPACE_PAUSE_REASON)
    card.set_paused(False)
    assert card.pause_reason is None and card.status_label.text() == "● 运行中"
    assert card.pause_button.text() == "暂停"
    card.set_paused(True)                                   # 再次暂停不带着旧原因
    assert card.status_label.text() == "● 已暂停"


# ========== 主窗口 + 真实守卫 ==========

Usage = namedtuple('Usage', 'total used free')


class Disk:
    def __init__(self, free):
        self.free = free

    def __call__(self, path):
        return Usage(0, 0, self.free)


@pytest.fixture
def window(tmp_path, qapp):
    db = Database(str(tmp_path / "storage_notices.db"))
    manager = MonitorManager(db=db)
    services = make_services(
        notifier=RecordingNotifier(), dialogs=ScriptedDialogs(),
        preferences=FakePreferences(), task_status=ManagerTaskStatus(manager))
    win = make_main_window(db, manager, services)
    win.about_group.check_update = lambda *args, **kwargs: None
    win.show()
    yield win
    stop_and_join(win.monitor_manager)
    dispose_widget(win)


def _guard_for(window, disk, **kwargs):
    from core.maintenance_service import MaintenanceService
    service = MaintenanceService(window.db, window.monitor_manager)
    guard = StorageGuard(window.monitor_manager, service, window.services.threads,
                         window.db.db_path, disk_usage=disk, quick_check_delay_s=None,
                         clock=lambda: 0.0, **kwargs)
    return guard


def _running_task(window):
    manager = window.monitor_manager
    task_id = manager.create_task(os.getpid(), "pytest.exe", [MetricType.NUM_THREADS], 0.2)
    assert task_id and manager.start_task(task_id)
    return task_id


def test_window_shows_notices_and_marks_cards_through_a_real_guard(window):
    task_id = _running_task(window)
    disk = Disk(150 * MB)
    guard = _guard_for(window, disk)
    window.attach_storage_guard(guard)
    guard.check_disk()
    card = window.monitor_page.presenter.board.card(task_id)
    assert card is not None and card.is_paused
    assert card.status_label.text() == "● 已暂停（磁盘空间不足）"
    notifier = window.services.notifier
    critical = notifier.last("warning")
    assert critical.title == notices.CRITICAL_TITLE and critical.parent is window
    assert "150.0 MB" in critical.content and window.db.db_path in critical.content

    disk.free = 5 * GB
    guard.check_disk()
    assert critical.bar.closed
    assert notifier.last("success").title == notices.RECOVERED_TITLE
    assert card.is_paused                                    # 不自动恢复，仍标注
    assert card.status_label.text() == "● 已暂停（磁盘空间不足）"

    window.monitor_page.presenter.on_resume(task_id)         # 用户手动恢复
    assert not card.is_paused and card.status_label.text() == "● 运行中"
    assert guard.paused_task_ids() == frozenset()
    guard.stop()


def test_window_keeps_one_space_bar_on_one_parent(window):
    _running_task(window)
    disk = Disk(900 * MB)
    guard = _guard_for(window, disk)
    window.attach_storage_guard(guard)
    guard.check_disk()
    low = window.services.notifier.last("warning")
    assert low.title == notices.LOW_SPACE_TITLE
    disk.free = 150 * MB
    guard.check_disk()
    critical = window.services.notifier.last("warning")
    assert critical.title == notices.CRITICAL_TITLE and low.bar.closed
    disk.free = 5 * GB
    guard.check_disk()
    assert critical.bar.closed
    shown = [n for n in window.services.notifier.messages() if not n.bar.closed]
    assert [n.title for n in shown] == [notices.RECOVERED_TITLE]
    assert {n.parent for n in window.services.notifier.messages()} == {window}
    guard.stop()


def test_window_hands_integrity_warning_to_the_settings_page(window):
    guard = _guard_for(window, Disk(50 * GB))
    window.attach_storage_guard(guard)
    page = window.setting_page
    assert page.integrity_card.isHidden()
    guard.integrity_problem.emit("Page 9 corrupt")
    assert not page.integrity_card.isHidden()
    assert "Page 9 corrupt" in page.integrity_card.contentLabel.text()
    assert page.integrity_card.titleLabel.text() == notices.INTEGRITY_TITLE
    assert len([n for n in window.services.notifier.messages("warning")
                if n.title == notices.INTEGRITY_TITLE]) == 1
    guard.stop()


def test_integrity_card_copy_button_copies_diagnostics(window):
    page = window.setting_page
    page.integrity_copy_button.click()
    assert window.services.dialogs.calls_of("copy_text")      # 与“复制诊断信息”同一动作


def test_settings_card_is_the_first_card_of_the_data_group(window):
    page = window.setting_page
    assert page.integrity_card.parent() is page.data_group
    page.show_integrity_warning("x")
    assert page.integrity_card.y() <= page.retention_card.y()


def test_real_quit_stops_the_guard_before_shutdown(window, monkeypatch):
    guard = _guard_for(window, Disk(50 * GB))
    window.attach_storage_guard(guard)
    guard.start()
    order = []
    real_stop = guard.stop
    monkeypatch.setattr(guard, 'stop', lambda: (order.append('guard.stop'), real_stop()))
    monkeypatch.setattr(window, '_run_shutdown', lambda: order.append('shutdown') or True)
    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    window._really_quit = True
    window.close()
    assert order == ['guard.stop', 'shutdown']
    assert not guard.is_running and not guard._timer.isActive()
