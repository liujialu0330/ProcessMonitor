"""
监控页交互：默认预设与记住选择、进程选择规则、备注、较开始变化量、打点、停止确认、
任务结束后卡片保留、异常结束提醒。

页面的外部依赖全部经 UiServices 注入：提示用 RecordingNotifier 断言，对话框选择用
ScriptedDialogs 预设（不弹出模态框），时间用 FakeClock，进程表用 FakeProcessQuery，
用户偏好用 FakePreferences；测试不替换页面模块里的任何名字。
"""
import os
from datetime import datetime, timedelta

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget

from core.contracts import StopReason, UnavailableReason
from core.monitor_manager import MonitorManager
from tests.doubles import (
    DEFAULT_FAKE_NOW, FakeClock, FakePreferences, FakeProcessQuery, ScriptedDialogs,
)
from tests.factories import make_monitor_page, make_services
from ui.components.metric_selector import MetricSelectorDialog
from ui.services import InfoBarNotifier
from utils.metrics import (
    AVAILABLE_METRICS, LEAK_CHECK_PRESET, MetricType, get_metric_description
)

# (pid, name, create_time)：与 ProcessQuery.list_processes 的返回形态一致
FAKE_PROCESSES = [
    (1001, "alpha.exe", 1001.5), (1002, "beta.exe", 1002.5), (1003, "MPrintExp.exe", 1003.5)]


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(process_query=FakeProcessQuery(FAKE_PROCESSES))


@pytest.fixture
def notifier(services):
    return services.notifier


@pytest.fixture
def dialogs(services):
    return services.dialogs


@pytest.fixture
def query(services):
    return services.process_query


@pytest.fixture
def page(qapp, db, manager, services):
    widget = make_monitor_page(db, manager, services)
    widget.resize(900, 700)
    yield widget
    widget.close()


def _add_card(page, manager, metrics=None, note="", pid=None):
    """创建（不启动）一个任务并取回页面上对应的卡片"""
    task_id = manager.create_task(
        pid or os.getpid(), "demo.exe",
        metrics or [MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS], 1.0, note=note)
    assert task_id
    return task_id, page.task_cards[task_id]


# ---------- 依赖注入与默认值 ----------

def test_page_uses_injected_dependencies(page, db, manager, services):
    assert page.manager is manager
    assert page.services is services
    assert page.db is db


def test_constructor_requires_db_manager_and_services(qapp):
    """不再兜底自建 Database / MonitorManager：三者缺一即构造失败"""
    from ui.pages.monitor_page import MonitorPage
    with pytest.raises(TypeError):
        MonitorPage()


def test_default_metrics_is_leak_check_preset(page):
    assert page.selected_metrics == LEAK_CHECK_PRESET
    assert "泄漏排查预设" in page.metric_summary_label.text()


def test_remembered_metrics_are_restored_on_new_page(qapp, db, manager):
    prefs = FakePreferences(last_metrics=[MetricType.CPU_PERCENT, MetricType.MEMORY_RSS])
    widget = make_monitor_page(db, manager, make_services(preferences=prefs))
    assert widget.selected_metrics == [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]
    widget.close()


def test_interval_spinbox_starts_from_default_interval_preference(qapp, db, manager):
    widget = make_monitor_page(
        db, manager, make_services(preferences=FakePreferences(default_interval=7)))
    assert widget.interval_spinbox.value() == 7
    widget.close()


def test_confirming_metric_dialog_remembers_selection(page, services, dialogs):
    chosen = [MetricType.NUM_THREADS, MetricType.MEMORY_PRIVATE]
    dialogs.push("choose_metrics", chosen)

    page._on_select_metric_clicked()

    # 对话框以当前选择预选、挂在主窗口上
    call = dialogs.last_call("choose_metrics")
    assert call["selected"] == list(LEAK_CHECK_PRESET)
    assert call["parent"] is page.window()
    assert page.selected_metrics == chosen
    assert services.preferences.remembered == [chosen]
    assert "已选 2 项" in page.metric_summary_label.text()


def test_cancelling_metric_dialog_keeps_selection(page, services, dialogs):
    dialogs.push("choose_metrics", None)

    page._on_select_metric_clicked()

    assert page.selected_metrics == LEAK_CHECK_PRESET
    assert services.preferences.remembered == []


# ---------- 指标对话框 ----------

def test_metric_dialog_has_tooltips_and_preset_button(qapp):
    host = QWidget()
    host.resize(800, 600)
    dialog = MetricSelectorDialog([MetricType.CPU_PERCENT], host)
    for metrics in AVAILABLE_METRICS.values():
        for metric in metrics:
            assert dialog.metric_checkboxes[metric].toolTip() == get_metric_description(metric)
            assert dialog.metric_checkboxes[metric].toolTip()

    assert dialog.get_selected() == [MetricType.CPU_PERCENT]
    dialog.preset_button.click()
    assert set(dialog.get_selected()) == set(LEAK_CHECK_PRESET)
    assert dialog.yesButton.isEnabled()
    # 分类三态联动：内存类只勾了私有字节 -> 部分选中
    assert dialog.category_checkboxes["内存"].checkState() == Qt.PartiallyChecked
    dialog.close()
    host.close()


# ---------- 进程选择 ----------

def test_startup_selects_no_process_and_fills_no_pid(page):
    assert page.process_combo.count() == len(FAKE_PROCESSES)
    assert page.process_combo.currentIndex() == -1
    assert page.process_combo.text() == ""
    assert page.pid_input.text() == ""


def test_start_without_any_selection_is_rejected(page, manager, notifier, monkeypatch):
    created = []
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: created.append(a))
    page._on_start_clicked()
    assert created == []
    assert notifier.levels() == ["warning"]


def test_unmatched_combo_text_without_pid_is_rejected(page, manager, notifier, monkeypatch):
    created = []
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: created.append(a))
    page.process_combo.setText("不存在的进程")
    page._on_start_clicked()
    assert created == []
    assert "不匹配" in notifier.last("warning").content


def test_combo_selection_is_used_when_pid_empty(page, manager, monkeypatch):
    started = []
    monkeypatch.setattr(
        page, "_start_monitoring", lambda *args, **kwargs: started.append((args, kwargs)))
    page.process_combo.setCurrentIndex(1)
    page.pid_input.clear()  # 用户清空了自动填入的 PID

    page._on_start_clicked()

    args, kwargs = started[0]
    assert args[:2] == (1002, "beta.exe")
    # 带上列表里该进程的创建时间，任务启动时据此确认 PID 未被复用
    assert kwargs["create_time"] == 1002.5


def test_autofilled_pid_carries_create_time_but_manual_pid_does_not(page, monkeypatch):
    """下拉选择自动填入的 PID 沿用列表的创建时间；手动输入的 PID 没有可比对信息"""
    started = []
    monkeypatch.setattr(
        page, "_start_monitoring", lambda *args, **kwargs: started.append((args, kwargs)))

    page.process_combo.setCurrentIndex(1)
    assert page.pid_input.text() == "1002"
    page._on_start_clicked()
    assert started[-1][0][:2] == (1002, "beta.exe")
    assert started[-1][1]["create_time"] == 1002.5

    # 用户清掉后手动输入同一个 PID（列表可能已过期）：不带创建时间，跳过比对
    page.pid_input.clear()
    page.pid_input.setText("1002")
    page._on_start_clicked()
    assert started[-1][0][0] == 1002 and started[-1][1]["create_time"] is None


def test_create_time_is_passed_through_to_manager(page, manager, monkeypatch):
    calls = []
    monkeypatch.setattr(
        manager, "create_task", lambda *a, **k: calls.append((a, k)) or None)
    page._start_monitoring(1001, "alpha.exe", [MetricType.NUM_THREADS], 1.0,
                           create_time=1001.5)
    assert calls[0][1]["create_time"] == 1001.5


def test_pid_input_takes_precedence_over_combo(page, query, monkeypatch):
    started = []
    monkeypatch.setattr(
        page, "_start_monitoring", lambda *args, **kwargs: started.append(args))
    page.process_combo.setCurrentIndex(0)
    page.pid_input.setText("1003")

    page._on_start_clicked()

    # 进程名经注入的进程查询按 PID 取得
    assert started[0][:2] == (1003, "MPrintExp.exe")
    assert ("name_of", 1003) in query.calls


def test_searching_another_process_clears_autofilled_pid(page):
    """下拉选择自动填入的 PID，在用户改为搜索别的进程时要清掉，否则会盖过新选择"""
    page.process_combo.setCurrentIndex(0)
    assert page.pid_input.text() == "1001"
    page.process_combo.setText("be")
    assert page.pid_input.text() == ""
    # 用户手动输入的 PID 不会被清掉
    page.pid_input.setText("4242")
    page.process_combo.setText("al")
    assert page.pid_input.text() == "4242"


def test_refresh_keeps_selection_but_does_not_select_first(page):
    page.process_combo.setCurrentIndex(2)
    page._refresh_process_list()
    assert page.process_combo.currentIndex() == 2

    page.process_combo.setCurrentIndex(-1)
    page._refresh_process_list()
    assert page.process_combo.currentIndex() == -1


def test_refresh_picks_up_process_table_changes(page, query):
    query.processes.append((1004, "gamma.exe", 1004.5))
    page._refresh_process_list()
    assert page.process_combo.count() == len(FAKE_PROCESSES) + 1
    assert page.process_create_times[1004] == 1004.5


def test_duplicate_pid_asks_for_confirmation(page, manager, dialogs, monkeypatch):
    created = []
    monkeypatch.setattr(
        manager, "create_task", lambda *a, **k: created.append((a, k)) or None)
    monkeypatch.setattr(manager, "has_running_task_for_pid", lambda pid: True)

    dialogs.push("confirm", False, True)
    assert page._start_monitoring(1001, "alpha.exe", [MetricType.NUM_THREADS], 1.0) is None
    assert created == []

    page._start_monitoring(1001, "alpha.exe", [MetricType.NUM_THREADS], 1.0)
    assert len(created) == 1

    calls = dialogs.calls_of("confirm")
    assert len(calls) == 2
    assert calls[0]["title"] == "已在监控该进程"
    assert "alpha.exe（PID 1001）" in calls[0]["content"]
    assert calls[0]["confirm_text"] == "继续创建"
    assert calls[0]["cancel_text"] == "取消"
    assert calls[0]["parent"] is page.window()


def test_no_confirmation_when_pid_not_running(page, manager, dialogs, monkeypatch):
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: None)
    # ScriptedDialogs 未预设 confirm：若被调用会直接断言失败
    page._start_monitoring(1001, "alpha.exe", [MetricType.NUM_THREADS], 1.0)
    assert dialogs.calls_of("confirm") == []


# ---------- 备注 ----------

def test_note_is_passed_to_create_task(page, manager, monkeypatch):
    calls = []
    monkeypatch.setattr(
        manager, "create_task", lambda *a, **k: calls.append((a, k)) or None)
    page.pid_input.setText("1001")
    page.note_input.setText("  建模 200 次  ")

    page._on_start_clicked()

    args, kwargs = calls[0]
    assert kwargs["note"] == "建模 200 次"
    assert args[3] == float(page.interval_spinbox.value())


def test_note_defaults_to_empty_and_shows_on_card(page, manager, monkeypatch):
    calls = []
    real_create = manager.create_task
    monkeypatch.setattr(
        manager, "create_task", lambda *a, **k: calls.append(k) or None)
    page.pid_input.setText("1001")
    page._on_start_clicked()
    assert calls[0]["note"] == ""
    monkeypatch.setattr(manager, "create_task", real_create)

    _, card = _add_card(page, manager, note="过夜测试")
    assert "过夜测试" in card.note_label.text()
    assert not card.note_label.isHidden()


# ---------- 卡片较开始变化量 ----------

def test_card_shows_delta_against_first_valid_value(page, manager):
    _, card = _add_card(page, manager)
    private, gdi = MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS

    card.update_values({private: 1024.0, gdi: 100.0})
    assert card.delta_labels[private].text() == "较开始 ±0"
    assert card.delta_labels[gdi].text() == "较开始 ±0"

    card.update_values({private: 3072.0, gdi: 103.0})
    assert card.delta_labels[private].text() == "较开始 +2 MB"
    assert card.delta_labels[gdi].text() == "较开始 +3 个"
    # 趋势图旁显示首指标的较开始
    assert card.trend_delta_label.text() == "较开始 +2 MB"

    card.update_values({private: 1024.0 - 512.0, gdi: 90.0})
    assert card.delta_labels[private].text() == "较开始 -512 KB"
    assert card.delta_labels[gdi].text() == "较开始 -10 个"


def test_card_baseline_is_first_valid_value_after_unavailable(page, manager):
    _, card = _add_card(page, manager)
    gdi = MetricType.NUM_GDI_OBJECTS
    card.mark_unavailable({gdi: UnavailableReason.ACCESS_DENIED})
    assert card.delta_labels[gdi].text() == "较开始 --"

    card.update_values({gdi: 50.0})
    assert card.delta_labels[gdi].text() == "较开始 ±0"
    assert card.baselines[gdi] == 50.0


def test_card_trend_covers_whole_task_with_bounded_points(page, manager):
    _, card = _add_card(page, manager)
    for i in range(1000):
        card.update_values({MetricType.MEMORY_PRIVATE: float(i)})
    assert len(card.sparkline.series()) <= 240
    assert "任务开始至今" in card.sparkline_label.text()


# ---------- 打点 ----------

def test_marker_flow_writes_via_manager_with_incrementing_default_label(
        page, manager, db, notifier, dialogs):
    task_id, card = _add_card(page, manager)
    # 第一次用默认标签，第二次取消，第三次自定义
    dialogs.push("ask_marker_label", "标记 1", None, "第 3 轮")

    page._on_marker_task(task_id)
    page._on_marker_task(task_id)   # 取消：不写库、序号不变
    page._on_marker_task(task_id)

    calls = dialogs.calls_of("ask_marker_label")
    assert [c["default_label"] for c in calls] == ["标记 1", "标记 2", "标记 2"]
    assert all(c["parent"] is page.window() for c in calls)
    assert [m.label for m in db.get_markers(task_id)] == ["标记 1", "第 3 轮"]
    assert card.next_marker_label() == "标记 3"
    assert notifier.levels() == ["success", "success"]
    assert "标记 1" in notifier.messages()[0].content


def test_marker_failure_is_reported(page, manager, notifier, dialogs, monkeypatch):
    task_id, card = _add_card(page, manager)
    dialogs.push("ask_marker_label", "标记 1")
    monkeypatch.setattr(manager, "add_marker", lambda tid, label, ts=None: None)

    page._on_marker_task(task_id)

    assert notifier.levels() == ["error"]
    # 错误提示不自动消失（可手动关闭）
    assert notifier.last("error").effective_duration == -1
    assert card.next_marker_label() == "标记 1"


class _ClockAdvancingDialogs(ScriptedDialogs):
    """打点输入框开着期间让时钟走若干秒，模拟用户输入标签花的时间"""

    def __init__(self, clock: FakeClock, typing_time: timedelta, **fixed):
        super().__init__(**fixed)
        self._clock = clock
        self._typing_time = typing_time

    def ask_marker_label(self, parent, default_label, timestamp):
        answer = super().ask_marker_label(parent, default_label, timestamp)
        self._clock.advance(self._typing_time)
        return answer


def test_marker_time_is_click_time_not_confirm_time(qapp, db, manager):
    """打点时间取点击"打点"按钮的时刻：用户输入标签花了 12 秒，落库时间不能是确认时刻"""
    clicked = datetime(2026, 11, 6, 10, 0, 0)
    clock = FakeClock(clicked)
    dialogs = _ClockAdvancingDialogs(
        clock, timedelta(seconds=12), ask_marker_label="开始第 3 轮")
    services = make_services(clock=clock, dialogs=dialogs)
    page = make_monitor_page(db, manager, services)
    try:
        task_id, _ = _add_card(page, manager)

        page._on_marker_task(task_id)

        markers = db.get_markers(task_id)
        assert [m.label for m in markers] == ["开始第 3 轮"]
        assert markers[0].timestamp == clicked
        assert dialogs.last_call("ask_marker_label")["timestamp"] == clicked
        assert clock.now() == clicked + timedelta(seconds=12)
        assert "10:00:00" in services.notifier.last("success").content
    finally:
        page.close()


# ---------- 停止确认 ----------

def test_stop_requires_confirmation(page, manager, dialogs, monkeypatch):
    task_id, card = _add_card(page, manager)
    stops = []
    monkeypatch.setattr(manager, "stop_task", lambda tid: stops.append(tid) or True)

    dialogs.push("confirm", False)
    page._on_stop_task(task_id)
    assert stops == [] and card.stop_button.isEnabled()

    dialogs.push("confirm", True)
    page._on_stop_task(task_id)
    assert stops == [task_id]
    # 发出停止请求后卡片等 task_stopped：按钮禁用，防止窗口期内重复点击
    assert not card.stop_button.isEnabled()
    assert not card.pause_button.isEnabled()

    call = dialogs.last_call("confirm")
    assert call["title"] == "停止监控"
    assert call["confirm_text"] == "停止" and call["cancel_text"] == "取消"
    assert "不可续采" in call["content"]


def test_stop_returning_false_is_tolerated(page, manager, notifier, dialogs, monkeypatch):
    task_id, card = _add_card(page, manager)
    dialogs.always("confirm", True)
    monkeypatch.setattr(manager, "stop_task", lambda tid: False)

    page._on_stop_task(task_id)

    assert card.stop_button.isEnabled()
    assert notifier.notices == []


def test_pause_resume_false_is_tolerated(page, manager, monkeypatch):
    task_id, card = _add_card(page, manager)
    monkeypatch.setattr(manager, "pause_task", lambda tid: False)
    monkeypatch.setattr(manager, "resume_task", lambda tid: False)
    page._on_pause_task(task_id)
    page._on_resume_task(task_id)
    assert not card.is_paused

    monkeypatch.setattr(manager, "pause_task", lambda tid: True)
    page._on_pause_task(task_id)
    assert card.is_paused and card.pause_button.text() == "恢复"


# ---------- 任务结束后卡片保留 ----------

def test_card_is_kept_in_finished_state_after_stop(page, manager, notifier):
    task_id, card = _add_card(page, manager, note="备注A")
    card.update_values({MetricType.MEMORY_PRIVATE: 1024.0, MetricType.NUM_GDI_OBJECTS: 10.0})
    card.update_values({MetricType.MEMORY_PRIVATE: 2048.0, MetricType.NUM_GDI_OBJECTS: 14.0})

    page._on_task_stopped(task_id, StopReason.USER)

    # 卡片保留并即时转"已结束"；core 侧任务对象延后一轮事件循环释放
    assert task_id in page.task_cards and card.is_finished
    assert manager.get_task_info(task_id) is not None
    QApplication.processEvents()
    assert manager.get_task_info(task_id) is None
    # 最后值与较开始保留
    assert card.value_labels[MetricType.MEMORY_PRIVATE].text() == "2 MB"
    assert card.delta_labels[MetricType.NUM_GDI_OBJECTS].text() == "较开始 +4 个"
    # 结束原因与时间（取注入的时钟）；运行中操作隐藏、结束后操作可见
    assert "用户停止" in card.finished_detail_label.text()
    assert DEFAULT_FAKE_NOW.strftime("%Y-%m-%d %H:%M:%S") in card.finished_detail_label.text()
    assert card.running_actions.isHidden() and not card.finished_actions.isHidden()
    assert not card.finished_label.isHidden() and card.running_label.isHidden()
    for button in (card.history_button, card.restart_button, card.close_button):
        assert not button.isHidden()
    # 空状态只在没有任何卡片时显示
    assert page.empty_state_label.isHidden()


def test_quota_counts_only_running_tasks(page, manager, monkeypatch):
    task_id, card = _add_card(page, manager)
    other_id, _ = _add_card(page, manager)
    monkeypatch.setattr(manager, "running_task_count", lambda: 1)
    page._on_task_stopped(task_id, StopReason.PROCESS_EXITED)
    text = page.tasks_label.text()
    assert "1/" in text and "运行中" in text and "1 个已结束" in text
    assert other_id in page.task_cards


def test_close_button_removes_card_and_shows_empty_state(page, manager):
    task_id, card = _add_card(page, manager)
    page._on_task_stopped(task_id, StopReason.USER)
    assert page.empty_state_label.isHidden()

    card.close_button.click()
    assert task_id not in page.task_cards
    assert not page.empty_state_label.isHidden()


def test_view_history_button_emits_request(page, manager):
    task_id, card = _add_card(page, manager)
    page._on_task_stopped(task_id, StopReason.USER)
    received = []
    page.history_requested.connect(received.append)

    card.history_button.click()

    assert received == [task_id]


def test_restart_with_single_match_creates_task_with_same_settings(
        page, manager, query, monkeypatch):
    task_id, card = _add_card(
        page, manager, metrics=[MetricType.NUM_HANDLES, MetricType.NUM_THREADS], note="第二轮")
    card.interval = 5.0
    page._on_task_stopped(task_id, StopReason.PROCESS_EXITED)
    query.processes.append((4321, "demo.exe", 777.25))
    started = []
    monkeypatch.setattr(
        page, "_start_monitoring", lambda *args, **kwargs: started.append((args, kwargs)))

    card.restart_button.click()

    assert started == [((
        4321, "demo.exe", [MetricType.NUM_HANDLES, MetricType.NUM_THREADS], 5.0, "第二轮"),
        {"create_time": 777.25})]


@pytest.mark.parametrize("matches, keyword", [
    ([], "未找到"), ([(11, 1.0), (22, 2.0)], "找到 2 个")])
def test_restart_with_zero_or_many_matches_fills_search_box(
        page, manager, notifier, query, monkeypatch, matches, keyword):
    task_id, card = _add_card(page, manager)
    page._on_task_stopped(task_id, StopReason.PROCESS_EXITED)
    notifier.clear()
    query.processes.extend((pid, "demo.exe", create_time) for pid, create_time in matches)
    monkeypatch.setattr(
        page, "_start_monitoring", lambda *a, **k: pytest.fail("不应直接创建任务"))
    page.pid_input.setText("999")

    card.restart_button.click()

    assert page.process_combo.text() == "demo.exe"
    assert page.pid_input.text() == ""
    last = notifier.last("warning")
    assert keyword in last.title + last.content


def test_find_processes_by_name_is_case_insensitive(page):
    assert page._find_processes_by_name("mprintexp.EXE") == [(1003, 1003.5)]
    assert page._find_processes_by_name("nothing.exe") == []


# ---------- 只经 manager 只读接口 ----------

def test_page_never_touches_task_threads(page, manager, monkeypatch):
    """建卡、重复 PID 判断都只用 manager 的快照/查询接口，不取任务线程对象"""
    monkeypatch.setattr(manager, "get_task", lambda tid: pytest.fail("界面不应取任务线程"))
    monkeypatch.setattr(manager, "get_all_tasks", lambda: pytest.fail("界面不应取任务线程"))

    task_id, card = _add_card(page, manager, metrics=[MetricType.NUM_THREADS], note="快照")
    assert (card.process_name, card.note, card.metric_types) == (
        "demo.exe", "快照", [MetricType.NUM_THREADS])

    monkeypatch.setattr(manager, "has_running_task_for_pid", lambda pid: pid == 4321)
    assert page._has_running_task_for_pid(4321) is True
    assert page._has_running_task_for_pid(1) is False


# ---------- 指标不可用提示 ----------

def test_unavailable_hint_mentions_admin_only_for_access_denied(page, manager, notifier):
    task_id, _ = _add_card(page, manager)
    gdi, uss = MetricType.NUM_GDI_OBJECTS, MetricType.MEMORY_USS

    page._on_metrics_unavailable(task_id, {gdi: UnavailableReason.NOT_IN_SESSION})
    content = notifier.last("warning").content
    assert "进程不在当前会话" in content and "管理员" not in content

    page._on_metrics_unavailable(task_id, {uss: UnavailableReason.ACCESS_DENIED})
    content = notifier.last("warning").content
    assert "权限不足" in content and "以管理员身份运行" in content


# ---------- 异常结束提醒 ----------

def test_abnormal_stop_is_prominent_and_signals_main_window(page, manager, notifier):
    task_id, card = _add_card(page, manager)
    received = []
    page.task_ended_abnormally.connect(lambda title, content: received.append(content))

    page._on_task_stopped(task_id, StopReason.PROCESS_EXITED)

    last = notifier.last()
    assert last.level == "warning" and last.effective_duration >= 10000
    assert last.parent is page
    assert received and "进程已终止" in received[0] and "demo.exe" in received[0]
    # 结束原因非"用户停止"时卡片也有对应文案
    assert "进程已终止" in card.finished_detail_label.text()


def test_user_stop_does_not_signal_abnormal(page, manager, notifier):
    task_id, _ = _add_card(page, manager)
    received = []
    page.task_ended_abnormally.connect(lambda *args: received.append(args))

    page._on_task_stopped(task_id, StopReason.USER)

    assert received == []
    assert notifier.last().level == "info"


def test_find_processes_by_name_excludes_own_process(page, query):
    """被测的同名进程退出后，"重新监控"不能因为只剩本程序同名而监控起自己。"""
    own = query.self_pid()
    query.processes = [(own, "python.exe", 1.0), (99999, "python.exe", 2.0),
                       (12345, "other.exe", 3.0)]
    assert page._find_processes_by_name("PYTHON.EXE") == [(99999, 2.0)]

    query.processes = [(own, "python.exe", 1.0)]
    assert page._find_processes_by_name("python.exe") == []


def test_exited_process_is_cleared_from_form(page, manager, query):
    """被测进程退出后，表单里不能一直留着已不存在的 PID（再点开始才报"进程不存在"）。"""
    task_id, _ = _add_card(page, manager, pid=1002)
    page.process_combo.setCurrentIndex(1)
    assert page.pid_input.text() == "1002"

    query.kill(1002)
    page._on_task_stopped(task_id, StopReason.PROCESS_EXITED)

    assert page.pid_input.text() == ""
    assert page.process_combo.currentIndex() == -1
    assert page.process_combo.text() == ""
    assert all(page.process_combo.itemData(i)[0] != 1002
               for i in range(page.process_combo.count()))


def test_form_is_kept_when_selection_is_another_or_still_alive(page, manager, query):
    task_id, _ = _add_card(page, manager, pid=1002)
    other_id, _ = _add_card(page, manager, pid=1002)

    # 表单选的是别的进程：不动
    page.process_combo.setCurrentIndex(0)
    query.alive = set()
    page._on_task_stopped(task_id, StopReason.PROCESS_EXITED)
    assert page.pid_input.text() == "1001"
    assert page.process_combo.currentIndex() == 0

    # 选的就是它，但进程还活着（如用户手动停止）：不动
    page.process_combo.setCurrentIndex(1)
    query.alive = {1002}
    page._on_task_stopped(other_id, StopReason.USER)
    assert page.pid_input.text() == "1002"
    assert page.process_combo.currentIndex() == 1


# ---------- 表单校验错误与错误提示 ----------

@pytest.mark.parametrize("pid_text, expected", [
    ("abc", "PID 必须是数字"),
    ("999999", "进程 PID 999999 不存在或无法访问"),
])
def test_form_validation_error_shows_under_field_and_clears_on_edit(
        page, manager, notifier, monkeypatch, pid_text, expected):
    """PID 无效/进程不存在：错误显示在输入框下方（不用一闪即逝的顶部提示），改输入即清除"""
    created = []
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: created.append(a))
    page.show()

    page.pid_input.setText(pid_text)
    page._on_start_clicked()

    assert created == []
    assert page.form_error_label.isVisible()
    assert page.form_error_label.text() == expected
    assert notifier.notices == []

    page.pid_input.setText(pid_text + "1")
    assert not page.form_error_label.isVisible()
    assert page.form_error_label.text() == ""

    # 改为在下拉框里搜索同样清除
    page.pid_input.setText(pid_text)
    page._on_start_clicked()
    assert page.form_error_label.isVisible()
    page.process_combo.setText("al")
    assert not page.form_error_label.isVisible()


def test_task_error_infobar_is_persistent(page, notifier):
    """运行期错误提示不自动消失（可手动关闭）"""
    page._on_error("t1", "采集数据时发生错误: x")
    assert notifier.levels() == ["error"]
    assert notifier.last("error").effective_duration == -1


def test_repeated_task_error_is_not_stacked_until_closed(page, notifier):
    """错误提示不再自动消失后，每周期重复上报的同一错误不能越叠越多；关掉后可再次提示"""
    for _ in range(3):
        page._on_error("t1", "采集数据时发生错误: x")
    page._on_error("t2", "采集数据时发生错误: x")

    errors = notifier.messages("error")
    assert len(errors) == 2
    assert set(page._error_bars) == {("t1", "采集数据时发生错误: x"),
                                     ("t2", "采集数据时发生错误: x")}

    errors[0].bar.close()
    assert ("t1", "采集数据时发生错误: x") not in page._error_bars

    page._on_error("t1", "采集数据时发生错误: x")
    assert len(notifier.messages("error")) == 3


def test_repeated_task_error_dedup_works_with_real_infobar(qapp, db, manager):
    """生产提示实现返回的真实 InfoBar：关闭信号同样能解除去重"""
    from qfluentwidgets import InfoBar
    page = make_monitor_page(db, manager, make_services(notifier=InfoBarNotifier()))
    page.resize(900, 700)
    page.show()
    try:
        for _ in range(3):
            page._on_error("t1", "采集数据时发生错误: x")
        page._on_error("t2", "采集数据时发生错误: x")

        bars = [w for w in page.findChildren(InfoBar) if not w.isHidden()]
        assert len(bars) == 2

        page._error_bars[("t1", "采集数据时发生错误: x")].close()
        assert ("t1", "采集数据时发生错误: x") not in page._error_bars
    finally:
        page.close()
