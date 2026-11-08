"""
统一通知出口 InfoBarNotifier.route：事件 × 窗口状态的渠道矩阵、右下角位置、切页不丢、
同 key 合并、停留时长规则、任务异常结束时闪任务栏、无窗口模式只写日志。

窗口状态经注入的 state_of 控制（离屏平台上 isActiveWindow 不可靠），托盘用记录型替身，
闪任务栏用记录型 alert；提示条是真实的 qfluentwidgets InfoBar。
"""
import logging

import pytest
from PyQt5.QtWidgets import QApplication, QWidget
from qfluentwidgets import InfoBarPosition, PushButton

from core.monitor_manager import MonitorManager
from data.database import Database
from tests.doubles import FakePreferences, RecordingNotifier, ScriptedDialogs
from tests.factories import make_main_window, make_services
from tests.helpers import dispose_widget, stop_and_join
from ui.services import (
    ACTION_DURATION_MS, EVENT_GENERIC, EVENT_TASK_ABNORMAL, EVENT_TASK_ERROR, InfoBarNotifier,
    ManagerTaskStatus, NoticeAction,
)
from ui.services.notifier import (
    BUBBLE_CONTENT_MAX, MAX_BARS_PER_KEY, STATE_FOREGROUND, STATE_HIDDEN, STATE_INACTIVE,
    STATE_MINIMIZED, DetachedBar, window_state,
)


class FakeTray:
    def __init__(self, available=True):
        self.available = available
        self.messages = []

    def is_available(self):
        return self.available

    def show_message(self, title, content, kind='info'):
        self.messages.append((title, content, kind))


class Env:
    """一套接好线的通知环境：真实 InfoBarNotifier + 主窗口替身（真实控件）。"""

    def __init__(self, host):
        self.host = host
        self.state = STATE_FOREGROUND
        self.bubble_enabled = True
        self.tray = FakeTray()
        self.flashes = []
        self.notifier = InfoBarNotifier(
            alert=self.flashes.append, state_of=lambda window: self.state)
        self.notifier.attach(host, tray=self.tray,
                             background_enabled=lambda: self.bubble_enabled)
        self.bars = []

    def route(self, level="success", *, event=EVENT_GENERIC, action=None, key=None,
              title="标题", content="内容", duration=None, anchor=None):
        bar = self.notifier.route(event, level, title, content, action=action, key=key,
                                  duration=duration, anchor=anchor)
        if bar is not None:
            self.bars.append(bar)
            bar.closedSignal.connect(lambda b=bar: b.setProperty("closed", True))
        return bar

    def live_bars(self):
        return [b for b in self.bars if _alive(b)]


def _alive(bar):
    try:
        return bar.isVisible() and not bar.property("closed")
    except RuntimeError:
        return False


@pytest.fixture
def host(qapp):
    widget = QWidget()
    widget.resize(900, 600)
    widget.show()
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def env(host):
    environment = Env(host)
    yield environment
    for bar in environment.bars:
        try:
            bar.close()
        except RuntimeError:
            pass
    QApplication.processEvents()


ACTION = NoticeAction("查看", lambda: None)

# 状态 -> (是否前台, 是否闪任务栏的候选状态)
BACKGROUND_STATES = [STATE_MINIMIZED, STATE_HIDDEN, STATE_INACTIVE]


# ========== 渠道矩阵：窗口状态 × 事件 ==========

@pytest.mark.parametrize("level, with_action", [
    ("success", False), ("warning", False), ("error", False), ("success", True),
])
def test_foreground_uses_a_main_window_bar_and_never_a_bubble(env, level, with_action):
    env.state = STATE_FOREGROUND
    bar = env.route(level, action=ACTION if with_action else None)
    assert bar is not None and bar.parent() is env.host
    assert bar.position == InfoBarPosition.BOTTOM_RIGHT
    assert env.tray.messages == [] and env.flashes == []


@pytest.mark.parametrize("state", [STATE_MINIMIZED, STATE_HIDDEN])
@pytest.mark.parametrize("level, with_action, persistent", [
    ("success", False, False), ("warning", False, False),
    ("error", False, True), ("success", True, False),
])
def test_background_sends_a_bubble_and_keeps_only_persistent_bars(
        env, state, level, with_action, persistent):
    env.state = state
    bar = env.route(level, action=ACTION if with_action else None, title="T", content="C")
    kinds = {"success": "info", "warning": "warning", "error": "critical"}
    assert env.tray.messages == [("T", "C", kinds[level])]
    if persistent:
        assert bar is not None and bar.duration == -1       # 错误常驻：用户回来时还在
    else:
        assert bar is None


def test_bubble_switch_off_keeps_every_notice_on_the_window(env):
    env.state = STATE_INACTIVE
    env.bubble_enabled = False
    for level in ("success", "warning", "error"):
        bar = env.route(level)
        assert bar is not None and bar.parent() is env.host
    assert env.tray.messages == []


def test_unavailable_tray_falls_back_to_window_bar(env):
    env.state = STATE_MINIMIZED
    env.tray.available = False
    assert env.route("success") is not None
    assert env.tray.messages == []


def test_headless_only_writes_the_log(host, caplog):
    notifier = InfoBarNotifier(alert=lambda w: pytest.fail("无窗口模式不闪任务栏"))
    with caplog.at_level(logging.INFO, logger="ui.services.notifier"):
        for level in ("success", "warning", "error"):
            assert notifier.route(EVENT_TASK_ERROR, level, "标题", f"内容-{level}",
                                  action=ACTION) is None
    assert [r.levelno for r in caplog.records] == [logging.INFO, logging.WARNING, logging.ERROR]
    assert "内容-error" in caplog.records[-1].getMessage()
    # 便捷写法在没有挂靠控件时同样只写日志，并给出可安全调用的占位对象
    bar = notifier.warning(None, "标题", "内容")
    assert isinstance(bar, DetachedBar)
    bar.addWidget(QWidget())
    assert not host.findChildren(type(bar))


def test_overlong_bubble_text_is_clipped(env):
    env.state = STATE_MINIMIZED
    env.route("warning", title="T" * 200, content="C" * 1000)
    title, content, _ = env.tray.messages[0]
    assert len(title) <= 60 and len(content) == BUBBLE_CONTENT_MAX and content.endswith("…")


def test_anchor_is_used_only_when_no_main_window_is_attached(host, qapp):
    anchor = QWidget()
    anchor.resize(400, 300)
    anchor.show()
    notifier = InfoBarNotifier()
    bar = notifier.route(EVENT_GENERIC, "info", "t", "c", anchor=anchor)
    assert bar.parent() is anchor and bar.position == InfoBarPosition.BOTTOM_RIGHT
    bar.close()
    notifier.attach(host)
    notifier._state_of = lambda window: STATE_FOREGROUND
    bar = notifier.route(EVENT_GENERIC, "info", "t", "c", anchor=anchor)
    assert bar.parent() is host                              # 有主窗口时忽略 anchor
    bar.close()
    anchor.close()


# ========== 切页不丢 ==========

def test_bar_survives_page_switches_in_the_real_main_window(tmp_path, qapp):
    db = Database(str(tmp_path / "notify.db"))
    manager = MonitorManager(db=db)
    notifier = InfoBarNotifier(state_of=lambda window: STATE_FOREGROUND)
    services = make_services(notifier=notifier, dialogs=ScriptedDialogs(),
                             preferences=FakePreferences(),
                             task_status=ManagerTaskStatus(manager))
    window = make_main_window(db, manager, services)
    window.about_group.check_update = lambda *args, **kwargs: None
    window.show()
    try:
        bar = services.notifier.route(EVENT_GENERIC, "error", "导出失败", "磁盘已满",
                                      anchor=window.monitor_page)
        assert bar.parent() is window                         # 挂在主窗口，不是监控页
        for page in (window.analysis_page, window.setting_page, window.monitor_page):
            window.switchTo(page)
            QApplication.processEvents()
            assert bar.isVisible() and bar.parent() is window
        bar.close()
    finally:
        stop_and_join(manager)
        dispose_widget(window)


# ========== 同 key 合并 ==========

def test_same_key_shows_at_most_two_bars_and_merges_the_rest(env):
    first = env.route("warning", key="k", content="第 1 条")
    second = env.route("warning", key="k", content="第 2 条")
    third = env.route("warning", key="k", content="第 3 条")
    assert MAX_BARS_PER_KEY == 2
    # 第 2 条被带"另有 1 条"的第 3 条取代，最早的一条留着
    assert _alive(first) and not _alive(second) and _alive(third)
    assert third.content == "第 3 条（另有 1 条同类提示）"
    fourth = env.route("warning", key="k", content="第 4 条")
    assert not _alive(third) and _alive(first) and _alive(fourth)
    assert fourth.content == "第 4 条（另有 2 条同类提示）"
    assert len(env.live_bars()) == 2


def test_different_keys_and_no_key_do_not_merge(env):
    for key in ("a", "b", None, None, None):
        env.route("warning", key=key)
    assert len(env.live_bars()) == 5


def test_merge_counter_restarts_after_the_summary_bar_is_closed(env):
    env.route("warning", key="k", content="1")
    env.route("warning", key="k", content="2")
    summary = env.route("warning", key="k", content="3")
    summary.close()
    QApplication.processEvents()
    again = env.route("warning", key="k", content="4")
    assert _alive(again) and "另有" not in again.content


# ========== 停留时长 ==========

@pytest.mark.parametrize("level, with_action, expected", [
    ("success", False, 2000), ("info", False, 2000), ("warning", False, 5000),
    ("error", False, -1), ("success", True, ACTION_DURATION_MS),
    ("warning", True, ACTION_DURATION_MS), ("error", True, -1),
])
def test_duration_rules(env, level, with_action, expected):
    bar = env.route(level, action=ACTION if with_action else None)
    assert bar.duration == expected


def test_explicit_duration_wins(env):
    assert env.route("success", duration=8000).duration == 8000
    assert env.route("warning", duration=-1).duration == -1
    assert env.route("error", duration=3000).duration == 3000


def test_action_button_is_part_of_the_bar_and_fires_its_callback(env):
    clicks = []
    bar = env.route("success", action=NoticeAction("打开文件夹", lambda: clicks.append(1)))
    button = next(b for b in bar.findChildren(PushButton) if b.text() == "打开文件夹")
    button.click()
    assert clicks == [1]


# ========== 闪任务栏 ==========

@pytest.mark.parametrize("event", [EVENT_TASK_ABNORMAL, EVENT_TASK_ERROR])
@pytest.mark.parametrize("state", [STATE_MINIMIZED, STATE_INACTIVE])
def test_abnormal_task_events_flash_the_taskbar_when_not_in_front(env, event, state):
    env.state = state
    env.route("warning", event=event)
    assert env.flashes == [env.host]


@pytest.mark.parametrize("event", [EVENT_TASK_ABNORMAL, EVENT_TASK_ERROR])
def test_no_flash_when_the_window_is_in_front(env, event):
    env.state = STATE_FOREGROUND
    env.route("warning", event=event)
    assert env.flashes == []


def test_flash_does_not_depend_on_the_bubble_switch_and_other_events_never_flash(env):
    env.state = STATE_INACTIVE
    env.bubble_enabled = False
    env.route("warning", event=EVENT_TASK_ABNORMAL)
    assert len(env.flashes) == 1
    env.route("warning", event=EVENT_GENERIC)
    env.route("error", event="export.failed")
    assert len(env.flashes) == 1


def test_a_failing_flash_does_not_lose_the_notice(env):
    def boom(window):
        raise RuntimeError("alert")
    env.notifier._alert = boom
    env.state = STATE_MINIMIZED
    env.route("warning", event=EVENT_TASK_ABNORMAL)
    assert len(env.tray.messages) == 1


def test_default_alert_calls_qapplication_alert(host, monkeypatch):
    calls = []
    monkeypatch.setattr(QApplication, "alert", lambda widget, msec=0: calls.append((widget, msec)))
    notifier = InfoBarNotifier(state_of=lambda window: STATE_MINIMIZED)
    notifier.attach(host, tray=FakeTray())
    notifier.route(EVENT_TASK_ABNORMAL, "warning", "t", "c")
    assert calls == [(host, 0)]                              # 0 = 直到用户切回窗口


# ========== 便捷写法与窗口状态判断 ==========

def test_convenience_methods_route_with_the_generic_event(env):
    env.state = STATE_MINIMIZED
    bar = env.notifier.warning(env.host, "标题", "内容")
    assert isinstance(bar, DetachedBar)                      # 只发了气泡：占位对象
    assert env.tray.messages == [("标题", "内容", "warning")]
    env.state = STATE_FOREGROUND
    bar = env.notifier.error(env.host, "错误", "出错了")
    env.bars.append(bar)
    assert bar.duration == -1 and bar.position == InfoBarPosition.BOTTOM_RIGHT


def test_window_state_of_a_real_widget(qapp):
    widget = QWidget()
    assert window_state(widget) == STATE_HIDDEN
    widget.show()
    assert window_state(widget) in (STATE_FOREGROUND, STATE_INACTIVE)
    widget.showMinimized()
    assert window_state(widget) in (STATE_MINIMIZED, STATE_HIDDEN)
    widget.close()
    widget.deleteLater()
    QApplication.processEvents()


def test_recording_notifier_records_route_arguments():
    notifier = RecordingNotifier()
    bar = notifier.route(EVENT_TASK_ABNORMAL, "warning", "t", "c", action=ACTION, key="k")
    notice = notifier.last("warning")
    assert (notice.event, notice.key, notice.action) == (EVENT_TASK_ABNORMAL, "k", ACTION)
    assert notice.effective_duration == ACTION_DURATION_MS and notice.bar is bar
    with pytest.raises(ValueError):
        notifier.route("e", "fatal", "t", "c")
