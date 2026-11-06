"""
监控页组装与连线：控件信号 → Presenter → manager 命令，以及关键可见状态（进程列表、
字段错误、卡片形态、配额标题、空状态、表单折叠、对外信号）。

业务规则（重复 PID、停止确认文案、打点时刻、重新监控三种情况、错误去重等）的细节
由 tests/test_monitor_presenter.py 用假视图覆盖；本文件只验证真实控件连起来后走得通。
页面依赖全部经 UiServices 注入，manager 用真实 MonitorManager（不启动采集线程）。
"""
import os

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QWidget

from core.contracts import StopReason, UnavailableReason
from core.monitor_manager import MonitorManager
from tests.doubles import DEFAULT_FAKE_NOW, FakePreferences, FakeProcessQuery
from tests.factories import make_monitor_page, make_services
from tests.helpers import stop_and_join
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
    mgr = MonitorManager(db=db)
    yield mgr
    stop_and_join(mgr)


@pytest.fixture
def started(manager, monkeypatch):
    """记录 start_task 调用而不真的启动采集线程"""
    calls = []
    monkeypatch.setattr(manager, "start_task", lambda tid: calls.append(tid) or True)
    return calls


@pytest.fixture
def services():
    return make_services(process_query=FakeProcessQuery(FAKE_PROCESSES))


@pytest.fixture
def page(qapp, db, manager, services, started):
    widget = make_monitor_page(db, manager, services)
    widget.resize(900, 700)
    widget.show()
    yield widget
    widget.close()


def _add_card(manager, metrics=None, note="", pid=None, name="demo.exe"):
    """经 manager 创建（不启动）任务：task_added 信号驱动页面建卡"""
    task_id = manager.create_task(
        pid or os.getpid(), name,
        metrics or [MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS], 1.0, note=note)
    assert task_id
    return task_id


def _select(page, pid):
    picker = page.form.picker
    assert picker.combo.select_data(next(t for t in picker.targets() if t.pid == pid))


# ---------- 依赖注入与默认值 ----------

def test_page_uses_injected_dependencies(page, db, manager, services):
    assert page.manager is manager
    assert page.services is services
    assert page.db is db
    assert page.presenter.manager is manager


def test_constructor_requires_db_manager_and_services(qapp):
    """不再兜底自建 Database / MonitorManager：三者缺一即构造失败"""
    from ui.pages.monitor_page import MonitorPage
    with pytest.raises(TypeError):
        MonitorPage()


def test_startup_lists_processes_selects_none_and_form_is_expanded(page):
    picker = page.form.picker
    assert [t.pid for t in picker.targets()] == [1001, 1002, 1003]
    assert picker.text() == "" and picker.combo.current_data() is None
    assert page.form.details.isVisible() and not page.form.expand_button.isVisible()
    assert page.board.empty_state.isVisible()
    assert page.summary_label.text() == "运行中 0/5"


def test_default_metrics_is_leak_check_preset(page):
    assert page.form.metrics() == list(LEAK_CHECK_PRESET)
    assert "泄漏排查预设" in page.form.metric_summary_label.text()


def test_remembered_metrics_and_default_interval_are_restored(qapp, db, manager):
    prefs = FakePreferences(last_metrics=[MetricType.CPU_PERCENT, MetricType.MEMORY_RSS],
                            default_interval=7)
    widget = make_monitor_page(db, manager, make_services(preferences=prefs))
    assert widget.form.metrics() == [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]
    assert widget.form.interval_spinbox.value() == 7
    widget.set_default_interval(9)
    assert widget.form.interval_spinbox.value() == 9
    widget.close()


def test_metric_button_opens_dialog_and_updates_summary(page, services):
    chosen = [MetricType.NUM_THREADS, MetricType.MEMORY_PRIVATE]
    services.dialogs.push("choose_metrics", chosen)

    page.form.metric_button.click()

    assert services.dialogs.last_call("choose_metrics")["parent"] is page.window()
    assert page.form.metrics() == chosen
    assert "已选 2 项" in page.form.metric_summary_label.text()
    assert services.preferences.remembered == [chosen]


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


# ---------- 开始监控 ----------

def test_start_button_creates_task_card_and_collapses_form(page, manager, started, monkeypatch):
    calls = []
    real_create = manager.create_task
    monkeypatch.setattr(manager, "create_task",
                        lambda *a, **k: calls.append((a, k)) or real_create(*a, **k))
    _select(page, 1002)
    page.form.note_input.setText("  建模 200 次  ")

    page.form.start_button.click()

    args, kwargs = calls[0]
    assert args[:2] == (1002, "beta.exe")
    assert args[3] == float(page.form.interval_spinbox.value())
    # 带上列表里该进程的创建时间，任务启动时据此确认 PID 未被复用
    assert kwargs == {"note": "建模 200 次", "create_time": 1002.5}
    assert len(started) == 1 and list(page.board.cards()) == started
    card = page.board.cards()[started[0]]
    assert "建模 200 次" in card.note_label.text() and not card.note_label.isHidden()
    # 有任务后表单折叠成一行，空状态隐藏
    assert page.form.is_collapsed() and page.form.expand_button.isVisible()
    assert not page.board.empty_state.isVisible()


def test_typed_pid_in_search_box_is_used_without_create_time(page, manager, services,
                                                             monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: calls.append((a, k)))
    QTest.keyClicks(page.form.picker.combo.line_edit, "1003")

    page.form.start_button.click()

    assert calls[0][0][:2] == (1003, "MPrintExp.exe")
    assert calls[0][1]["create_time"] is None
    assert ("name_of", 1003) in services.process_query.calls


def test_enter_in_search_box_selects_first_match(page):
    line_edit = page.form.picker.combo.line_edit
    QTest.keyClicks(line_edit, "MPRINT")
    QTest.keyClick(line_edit, Qt.Key_Return)
    assert page.form.picker.resolve().pid == 1003


@pytest.mark.parametrize("text, expected", [
    ("", "请输入进程名或 PID，或从列表中选择进程"),
    ("不存在的进程", "输入的内容与进程列表不匹配，请从下拉列表中选择，或直接输入 PID"),
    ("999999", "进程 PID 999999 不存在或无法访问"),
])
def test_invalid_target_shows_error_under_field_and_clears_on_edit(
        page, manager, services, monkeypatch, text, expected):
    created = []
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: created.append(a))
    page.form.picker.set_text(text)

    page.form.start_button.click()

    assert created == []
    assert page.form.picker.error_label.isVisible()
    assert page.form.picker.error_text() == expected
    assert services.notifier.notices == []

    QTest.keyClicks(page.form.picker.combo.line_edit, "x")
    assert not page.form.picker.error_label.isVisible()


def test_page_has_no_separate_pid_box(page):
    """PID 直接在搜索框输入（P10）：表单里除搜索框外只剩备注一个单行输入框"""
    from qfluentwidgets import EditableComboBox, LineEdit
    edits = [w for w in page.form.findChildren(LineEdit)
             if not isinstance(w, EditableComboBox)]
    assert edits == [page.form.note_input]


# ---------- 卡片按钮连线 ----------

def test_data_updated_shows_values_delta_and_count(page, manager):
    task_id = _add_card(manager)
    card = page.board.cards()[task_id]
    private, gdi = MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS

    manager.data_updated.emit(task_id, {private: 1024.0, gdi: 100.0})
    manager.data_updated.emit(task_id, {private: 3072.0, gdi: 103.0})

    assert card.value_labels[private].text() == "3 MB"
    # 无基线：比开始时（相对时间取注入时钟，替身时钟不走，故为 0 秒前）
    assert card.delta_labels[private].text() == "比开始时（0 秒前）+2 MB"
    assert card.delta_labels[gdi].text() == "比开始时（0 秒前）+3 个"
    assert card.trend_delta_label.text() == "比开始时（0 秒前）+2 MB"
    assert card.count == 2
    assert card.count_label.text() == "已运行 0 秒 · 每 1 秒 · 2 次采集"


def test_metrics_unavailable_marks_card(page, manager, services):
    task_id = _add_card(manager)
    manager.metrics_unavailable.emit(
        task_id, {MetricType.NUM_GDI_OBJECTS: UnavailableReason.ACCESS_DENIED})
    card = page.board.cards()[task_id]
    assert card.value_labels[MetricType.NUM_GDI_OBJECTS].text() == "不可用"
    assert "以管理员身份运行" in services.notifier.last("warning").content


def test_stop_button_confirms_then_requests_stop(page, manager, services, monkeypatch):
    task_id = _add_card(manager)
    card = page.board.cards()[task_id]
    stops = []
    monkeypatch.setattr(manager, "stop_task", lambda tid: stops.append(tid) or True)

    services.dialogs.push("confirm", False, True)
    card.stop_button.click()
    assert stops == [] and card.stop_button.isEnabled()
    card.stop_button.click()
    assert stops == [task_id]
    assert not card.stop_button.isEnabled() and not card.pause_button.isEnabled()
    assert services.dialogs.last_call("confirm")["parent"] is page.window()


def test_pause_button_toggles_card(page, manager, monkeypatch):
    task_id = _add_card(manager)
    card = page.board.cards()[task_id]
    monkeypatch.setattr(manager, "pause_task", lambda tid: True)
    monkeypatch.setattr(manager, "resume_task", lambda tid: True)

    card.pause_button.click()
    assert card.is_paused and card.pause_button.text() == "恢复"
    card.pause_button.click()
    assert not card.is_paused and card.pause_button.text() == "暂停"


def test_marker_button_writes_marker_via_manager(page, manager, db, services):
    task_id = _add_card(manager)
    services.dialogs.push("ask_marker_label", "预热完成", "第 1 轮")
    card = page.board.cards()[task_id]

    card.marker_button.click()
    card.marker_button.click()

    markers = db.get_markers(task_id)
    # 首个打点默认"基线"：改了名称仍是基线；之后默认"第 1 轮"
    assert [(m.label, m.kind) for m in markers] == [("预热完成", "baseline"),
                                                     ("第 1 轮", "user")]
    assert markers[0].timestamp == DEFAULT_FAKE_NOW
    calls = services.dialogs.calls_of("ask_marker_label")
    assert [c["default_label"] for c in calls] == ["基线", "第 1 轮"]
    assert (calls[0]["process_name"], calls[0]["pid"]) == ("demo.exe", os.getpid())
    assert services.notifier.last().level == "success"
    # 卡片进入"较基线"模式，基线值要等基线之后的 5 个样本
    assert card.baseline is not None
    assert card.delta_labels[MetricType.MEMORY_PRIVATE].text() == "基线采样中"
    for value in (1024.0, 2048.0, 3072.0, 4096.0, 5120.0):
        manager.data_updated.emit(task_id, {MetricType.MEMORY_PRIVATE: value})
    manager.data_updated.emit(task_id, {MetricType.MEMORY_PRIVATE: 6144.0})
    assert card.delta_labels[MetricType.MEMORY_PRIVATE].text() == "较基线 +3 MB"


# ---------- 任务结束 ----------

def test_stopped_card_is_kept_in_finished_group(page, manager, monkeypatch):
    task_id = _add_card(manager, note="备注A")
    other_id = _add_card(manager)
    card = page.board.cards()[task_id]
    manager.data_updated.emit(task_id, {MetricType.MEMORY_PRIVATE: 1024.0})
    manager.data_updated.emit(task_id, {MetricType.MEMORY_PRIVATE: 2048.0})
    monkeypatch.setattr(manager, "running_task_count", lambda: 1)

    manager.task_stopped.emit(task_id, StopReason.USER)

    # 卡片保留并即时转"已结束"；未启动的任务没有线程可等，core 侧立即移除
    assert card.is_finished and page.board.cards()[task_id] is card
    assert manager.get_task_info(task_id) is None
    assert card.value_labels[MetricType.MEMORY_PRIVATE].text() == "2 MB"
    assert "用户停止" in card.finished_detail_label.text()
    # 结束时间：同一天只写时分秒，完整日期在悬停提示里
    assert DEFAULT_FAKE_NOW.strftime("%H:%M:%S") in card.finished_detail_label.text()
    assert DEFAULT_FAKE_NOW.strftime("%Y-%m-%d %H:%M:%S") in card.finished_detail_label.toolTip()
    assert card.running_actions.isHidden() and not card.finished_actions.isHidden()
    # 配额只计运行中；已结束的单独计数并归入下方分组
    assert page.summary_label.text() == "运行中 1/5 · 已结束 1"
    assert page.board.finished_label.isVisible()
    assert other_id in page.board.cards()


def test_abnormal_stop_signals_main_window(page, manager, services):
    task_id = _add_card(manager)
    received = []
    page.task_ended_abnormally.connect(lambda title, content: received.append(content))

    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)

    assert received and "进程已终止" in received[0] and "demo.exe" in received[0]
    assert services.notifier.last().parent is page
    assert "进程已终止" in page.board.cards()[task_id].finished_detail_label.text()


def test_pause_and_resume_write_system_markers_and_card_ticks(page, manager, db):
    """经真实 manager 暂停/恢复：写入系统打点，markers_changed 驱动卡片刷新刻度"""
    task_id = _add_card(manager)
    card = page.board.cards()[task_id]
    # 不起采集线程：直接把任务置为运行态，暂停/恢复只改状态并写系统打点
    from core.monitor_task import WorkerState
    assert manager.get_task(task_id)._transition((WorkerState.WAITING,), WorkerState.RUNNING)

    card.pause_button.click()
    card.pause_button.click()

    assert [m.kind for m in db.get_markers(task_id)] == ["pause", "resume"]
    assert card.status_label.text() == "● 运行中"


def test_history_button_emits_page_signal(page, manager):
    task_id = _add_card(manager)
    manager.task_stopped.emit(task_id, StopReason.USER)
    received = []
    page.history_requested.connect(received.append)

    page.board.cards()[task_id].history_button.click()

    assert received == [task_id]


def test_close_button_removes_card_and_expands_form(page, manager):
    task_id = _add_card(manager)
    manager.task_stopped.emit(task_id, StopReason.USER)
    assert page.form.is_collapsed()

    page.board.cards()[task_id].close_button.click()

    assert task_id not in page.board.cards()
    assert page.board.empty_state.isVisible()
    assert not page.form.is_collapsed() and not page.form.expand_button.isVisible()


def test_restart_button_creates_new_task_for_single_match(page, manager, services, started):
    task_id = _add_card(manager, metrics=[MetricType.NUM_HANDLES], note="第二轮")
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    services.process_query.processes.append((4321, "demo.exe", 777.25))

    page.board.cards()[task_id].restart_button.click()

    assert len(started) == 1
    new_card = page.board.cards()[started[0]]
    assert (new_card.pid, new_card.note, new_card.metric_types) == (
        4321, "第二轮", [MetricType.NUM_HANDLES])


def test_restart_without_match_fills_search_box(page, manager, services):
    task_id = _add_card(manager)
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)

    page.board.cards()[task_id].restart_button.click()

    assert page.form.picker.text() == "demo.exe"
    assert "未找到" in services.notifier.last("warning").title


def test_exited_process_is_cleared_from_form(page, manager, services):
    """被测进程退出后，表单里不能一直留着已不存在的进程（再点开始才报"进程不存在"）"""
    task_id = _add_card(manager, pid=1002)
    _select(page, 1002)

    services.process_query.kill(1002)
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)

    assert page.form.picker.text() == ""
    assert all(t.pid != 1002 for t in page.form.picker.targets())


def test_page_never_touches_task_threads(page, manager, monkeypatch):
    """建卡、重复 PID 判断都只用 manager 的快照/查询接口，不取任务线程对象"""
    monkeypatch.setattr(manager, "get_task", lambda tid: pytest.fail("界面不应取任务线程"))
    monkeypatch.setattr(manager, "get_all_tasks", lambda: pytest.fail("界面不应取任务线程"))

    task_id = _add_card(manager, metrics=[MetricType.NUM_THREADS], note="快照")
    card = page.board.cards()[task_id]
    assert (card.process_name, card.note, card.metric_types) == (
        "demo.exe", "快照", [MetricType.NUM_THREADS])
    _select(page, 1001)
    page.form.start_button.click()


# ---------- 错误提示 ----------

def test_repeated_task_error_dedup_works_with_real_infobar(qapp, db, manager):
    """生产提示实现返回的真实 InfoBar：关闭信号同样能解除去重"""
    from qfluentwidgets import InfoBar
    page = make_monitor_page(db, manager, make_services(notifier=InfoBarNotifier()))
    page.resize(900, 700)
    page.show()
    try:
        for _ in range(3):
            manager.error_occurred.emit("t1", "采集数据时发生错误: x")
        manager.error_occurred.emit("t2", "采集数据时发生错误: x")

        bars = [w for w in page.findChildren(InfoBar) if not w.isHidden()]
        assert len(bars) == 2

        bars[0].close()
        assert len(page.presenter.shown_errors()) == 1
    finally:
        page.close()


# ---------- 页头、快捷键与自动刷新 ----------

def test_header_title_subtitle_summary_and_refresh(page, services):
    from ui.pages.monitor_page import PAGE_SUBTITLE, PAGE_TITLE, REFRESH_TEXT
    assert (PAGE_TITLE, REFRESH_TEXT) == ("监控", "刷新进程")
    assert PAGE_SUBTITLE == "选择被测进程开始记录；每轮操作后点“打点”即可量化泄漏"
    assert page.refresh_button.text() == REFRESH_TEXT
    query = services.process_query
    before = sum(1 for name, _ in query.calls if name == 'list_processes')
    page.refresh_button.click()
    assert sum(1 for name, _ in query.calls if name == 'list_processes') == before + 1


def test_summary_follows_tasks(page, manager):
    task_id = _add_card(manager)
    assert page.summary_label.text() == "运行中 0/5"
    manager.task_stopped.emit(task_id, StopReason.USER)
    assert page.summary_label.text() == "运行中 0/5 · 已结束 1"


def test_shortcuts_only_while_visible(page, services):
    assert page.shortcut_keys() == ["F5", "Ctrl+F"]
    assert all(s.isEnabled() for s in page._shortcuts)
    page.hide()
    assert not any(s.isEnabled() for s in page._shortcuts)
    page.show()
    query = services.process_query
    before = sum(1 for name, _ in query.calls if name == 'list_processes')
    page._shortcuts[0].activated.emit()
    assert sum(1 for name, _ in query.calls if name == 'list_processes') == before + 1


def test_ctrl_f_focuses_search(page):
    page.activateWindow()
    page._shortcuts[1].activated.emit()
    assert page.form.picker.combo.line_edit.hasFocus() or \
        QWidget.focusWidget(page) is page.form.picker.combo.line_edit


def test_clear_all_finished_removes_only_finished_cards(page, manager):
    done = _add_card(manager)
    alive = _add_card(manager, pid=1001, name="alpha.exe")
    manager.task_stopped.emit(done, StopReason.USER)
    page.board.clear_finished_button.click()
    assert list(page.board.cards()) == [alive]
    assert page.summary_label.text() == "运行中 0/5"


def test_card_analysis_button_requests_analysis(page, manager):
    task_id = _add_card(manager)
    requested = []
    page.history_requested.connect(requested.append)
    page.board.cards()[task_id].analysis_button.click()
    assert requested == [task_id]
