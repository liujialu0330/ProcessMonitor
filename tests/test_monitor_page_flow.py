"""
监控页交互：默认预设与记住选择、进程选择规则、备注、较开始变化量、打点、停止确认、
任务结束后卡片保留、异常结束提醒。

所有会弹模态对话框的交互（确认停止、打点输入、重复 PID 确认等）都已抽成页面方法，
这里一律 monkeypatch，测试中不会阻塞。
"""
import os

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget

import ui.pages.monitor_page as monitor_module
from app_config import cfg
from core.monitor_manager import MonitorManager
from core.process_collector import ProcessCollector
from ui.components.metric_selector import MetricSelectorDialog
from ui.pages.monitor_page import MonitorPage
from utils.metrics import (
    AVAILABLE_METRICS, LEAK_CHECK_PRESET, MetricType, get_metric_description
)

FAKE_PROCESSES = [(1001, "alpha.exe"), (1002, "beta.exe"), (1003, "MPrintExp.exe")]


@pytest.fixture(autouse=True)
def _reset_last_metrics():
    cfg.last_metrics.value = list(LEAK_CHECK_PRESET)
    yield
    cfg.last_metrics.value = list(LEAK_CHECK_PRESET)


class _InfoRecorder:
    """替换 monitor_page 模块里的 InfoBar，记录各级提示的参数"""

    def __init__(self):
        self.calls = []

    def _record(self, level):
        def _call(**kwargs):
            self.calls.append((level, kwargs))
        return _call

    def __getattr__(self, name):
        if name in ("info", "success", "warning", "error"):
            return self._record(name)
        raise AttributeError(name)

    def levels(self):
        return [level for level, _ in self.calls]


@pytest.fixture
def infobar(monkeypatch):
    recorder = _InfoRecorder()
    monkeypatch.setattr(monitor_module, "InfoBar", recorder)
    return recorder


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def page(qapp, manager, monkeypatch):
    monkeypatch.setattr(
        ProcessCollector, "get_all_processes", staticmethod(lambda: list(FAKE_PROCESSES)))
    widget = MonitorPage(manager=manager)
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

def test_page_uses_injected_manager(page, manager):
    assert page.manager is manager


def test_default_metrics_is_leak_check_preset(page):
    assert page.selected_metrics == LEAK_CHECK_PRESET
    assert "泄漏排查预设" in page.metric_summary_label.text()


def test_remembered_metrics_are_restored_on_new_page(qapp, manager, monkeypatch):
    monkeypatch.setattr(
        ProcessCollector, "get_all_processes", staticmethod(lambda: []))
    cfg.last_metrics.value = [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]
    widget = MonitorPage(manager=manager)
    assert widget.selected_metrics == [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]
    widget.close()


def test_confirming_metric_dialog_remembers_selection(page, monkeypatch):
    class _FakeDialog:
        def __init__(self, selected, parent):
            pass

        def exec(self):
            return True

        def get_selected(self):
            return [MetricType.NUM_THREADS, MetricType.MEMORY_PRIVATE]

    monkeypatch.setattr(monitor_module, "MetricSelectorDialog", _FakeDialog)
    page._on_select_metric_clicked()

    assert page.selected_metrics == [MetricType.NUM_THREADS, MetricType.MEMORY_PRIVATE]
    assert cfg.get(cfg.last_metrics) == [MetricType.NUM_THREADS, MetricType.MEMORY_PRIVATE]


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


def test_start_without_any_selection_is_rejected(page, manager, infobar, monkeypatch):
    created = []
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: created.append(a))
    page._on_start_clicked()
    assert created == []
    assert infobar.levels() == ["warning"]


def test_unmatched_combo_text_without_pid_is_rejected(page, manager, infobar, monkeypatch):
    created = []
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: created.append(a))
    page.process_combo.setText("不存在的进程")
    page._on_start_clicked()
    assert created == []
    assert "不匹配" in infobar.calls[-1][1]["content"]


def test_combo_selection_is_used_when_pid_empty(page, manager, monkeypatch):
    started = []
    monkeypatch.setattr(
        page, "_start_monitoring", lambda *args: started.append(args))
    page.process_combo.setCurrentIndex(1)
    page.pid_input.clear()  # 用户清空了自动填入的 PID

    page._on_start_clicked()

    assert started[0][:2] == (1002, "beta.exe")


def test_pid_input_takes_precedence_over_combo(page, monkeypatch):
    started = []
    monkeypatch.setattr(
        page, "_start_monitoring", lambda *args: started.append(args))
    page.process_combo.setCurrentIndex(0)
    page.pid_input.setText(str(os.getpid()))

    page._on_start_clicked()

    assert started[0][0] == os.getpid()


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


def test_duplicate_pid_asks_for_confirmation(page, manager, monkeypatch):
    created = []
    monkeypatch.setattr(
        manager, "create_task", lambda *a, **k: created.append((a, k)) or None)
    monkeypatch.setattr(page, "_has_running_task_for_pid", lambda pid: True)

    asked = []
    monkeypatch.setattr(
        page, "_confirm_duplicate_pid", lambda pid, name: asked.append(pid) or False)
    assert page._start_monitoring(1001, "alpha.exe", [MetricType.NUM_THREADS], 1.0) is None
    assert asked == [1001] and created == []

    monkeypatch.setattr(page, "_confirm_duplicate_pid", lambda pid, name: True)
    page._start_monitoring(1001, "alpha.exe", [MetricType.NUM_THREADS], 1.0)
    assert len(created) == 1


def test_no_confirmation_when_pid_not_running(page, manager, monkeypatch):
    monkeypatch.setattr(manager, "create_task", lambda *a, **k: None)
    monkeypatch.setattr(
        page, "_confirm_duplicate_pid",
        lambda pid, name: pytest.fail("不应弹出重复 PID 确认"))
    page._start_monitoring(1001, "alpha.exe", [MetricType.NUM_THREADS], 1.0)


# ---------- 备注 ----------

def test_note_is_passed_to_create_task(page, manager, monkeypatch):
    calls = []
    monkeypatch.setattr(
        manager, "create_task", lambda *a, **k: calls.append((a, k)) or None)
    page.pid_input.setText(str(os.getpid()))
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
    page.pid_input.setText(str(os.getpid()))
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
    card.mark_unavailable({gdi: "权限不足"})
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
        page, manager, db, infobar, monkeypatch):
    task_id, card = _add_card(page, manager)
    defaults = []
    answers = iter(["", None, "第 3 轮"])  # 第一次用默认标签，第二次取消，第三次自定义

    def _ask(default_label):
        defaults.append(default_label)
        answer = next(answers)
        return default_label if answer == "" else answer

    monkeypatch.setattr(page, "_ask_marker_label", _ask)

    page._on_marker_task(task_id)
    page._on_marker_task(task_id)   # 取消：不写库、序号不变
    page._on_marker_task(task_id)

    assert defaults == ["标记 1", "标记 2", "标记 2"]
    assert [m.label for m in db.get_markers(task_id)] == ["标记 1", "第 3 轮"]
    assert card.next_marker_label() == "标记 3"
    assert infobar.levels() == ["success", "success"]
    assert "标记 1" in infobar.calls[0][1]["content"]


def test_marker_failure_is_reported(page, manager, infobar, monkeypatch):
    task_id, card = _add_card(page, manager)
    monkeypatch.setattr(page, "_ask_marker_label", lambda default: default)
    monkeypatch.setattr(manager, "add_marker", lambda tid, label: None)

    page._on_marker_task(task_id)

    assert infobar.levels() == ["error"]
    assert card.next_marker_label() == "标记 1"


# ---------- 停止确认 ----------

def test_stop_requires_confirmation(page, manager, monkeypatch):
    task_id, card = _add_card(page, manager)
    stops = []
    monkeypatch.setattr(manager, "stop_task", lambda tid: stops.append(tid) or True)

    monkeypatch.setattr(page, "_confirm_stop", lambda c: False)
    page._on_stop_task(task_id)
    assert stops == [] and card.stop_button.isEnabled()

    monkeypatch.setattr(page, "_confirm_stop", lambda c: True)
    page._on_stop_task(task_id)
    assert stops == [task_id]
    # 发出停止请求后卡片等 task_stopped：按钮禁用，防止窗口期内重复点击
    assert not card.stop_button.isEnabled()
    assert not card.pause_button.isEnabled()


def test_stop_returning_false_is_tolerated(page, manager, infobar, monkeypatch):
    task_id, card = _add_card(page, manager)
    monkeypatch.setattr(page, "_confirm_stop", lambda c: True)
    monkeypatch.setattr(manager, "stop_task", lambda tid: False)

    page._on_stop_task(task_id)

    assert card.stop_button.isEnabled()
    assert infobar.calls == []


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

def test_card_is_kept_in_finished_state_after_stop(page, manager, infobar):
    task_id, card = _add_card(page, manager, note="备注A")
    card.update_values({MetricType.MEMORY_PRIVATE: 1024.0, MetricType.NUM_GDI_OBJECTS: 10.0})
    card.update_values({MetricType.MEMORY_PRIVATE: 2048.0, MetricType.NUM_GDI_OBJECTS: 14.0})

    page._on_task_stopped(task_id, "用户停止")

    # 卡片保留并即时转"已结束"；core 侧任务对象延后一轮事件循环释放
    assert task_id in page.task_cards and card.is_finished
    assert manager.get_task(task_id) is not None
    QApplication.processEvents()
    assert manager.get_task(task_id) is None
    # 最后值与较开始保留
    assert card.value_labels[MetricType.MEMORY_PRIVATE].text() == "2 MB"
    assert card.delta_labels[MetricType.NUM_GDI_OBJECTS].text() == "较开始 +4 个"
    # 结束原因与时间；运行中操作隐藏、结束后操作可见
    assert "用户停止" in card.finished_detail_label.text()
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
    page._on_task_stopped(task_id, "进程已终止")
    text = page.tasks_label.text()
    assert "1/" in text and "运行中" in text and "1 个已结束" in text
    assert other_id in page.task_cards


def test_close_button_removes_card_and_shows_empty_state(page, manager):
    task_id, card = _add_card(page, manager)
    page._on_task_stopped(task_id, "用户停止")
    assert page.empty_state_label.isHidden()

    card.close_button.click()
    assert task_id not in page.task_cards
    assert not page.empty_state_label.isHidden()


def test_view_history_button_emits_request(page, manager):
    task_id, card = _add_card(page, manager)
    page._on_task_stopped(task_id, "用户停止")
    received = []
    page.history_requested.connect(received.append)

    card.history_button.click()

    assert received == [task_id]


def test_restart_with_single_match_creates_task_with_same_settings(page, manager, monkeypatch):
    task_id, card = _add_card(
        page, manager, metrics=[MetricType.NUM_HANDLES, MetricType.NUM_THREADS], note="第二轮")
    card.interval = 5.0
    page._on_task_stopped(task_id, "进程已终止")
    started = []
    monkeypatch.setattr(page, "_find_pids_by_name", lambda name: [4321])
    monkeypatch.setattr(page, "_start_monitoring", lambda *args: started.append(args))

    card.restart_button.click()

    assert started == [(
        4321, "demo.exe", [MetricType.NUM_HANDLES, MetricType.NUM_THREADS], 5.0, "第二轮")]


@pytest.mark.parametrize("pids, keyword", [([], "未找到"), ([11, 22], "找到 2 个")])
def test_restart_with_zero_or_many_matches_fills_search_box(
        page, manager, infobar, monkeypatch, pids, keyword):
    task_id, card = _add_card(page, manager)
    page._on_task_stopped(task_id, "进程已终止")
    infobar.calls.clear()
    monkeypatch.setattr(page, "_find_pids_by_name", lambda name: pids)
    monkeypatch.setattr(
        page, "_start_monitoring", lambda *args: pytest.fail("不应直接创建任务"))
    page.pid_input.setText("999")

    card.restart_button.click()

    assert page.process_combo.text() == "demo.exe"
    assert page.pid_input.text() == ""
    assert keyword in infobar.calls[-1][1]["title"] + infobar.calls[-1][1]["content"]


def test_find_pids_by_name_is_case_insensitive(page):
    assert page._find_pids_by_name("mprintexp.EXE") == [1003]
    assert page._find_pids_by_name("nothing.exe") == []


# ---------- 异常结束提醒 ----------

def test_abnormal_stop_is_prominent_and_signals_main_window(page, manager, infobar):
    task_id, card = _add_card(page, manager)
    received = []
    page.task_ended_abnormally.connect(lambda title, content: received.append(content))

    page._on_task_stopped(task_id, "进程已终止")

    level, kwargs = infobar.calls[-1]
    assert level == "warning" and kwargs["duration"] >= 10000
    assert received and "进程已终止" in received[0] and "demo.exe" in received[0]
    # 结束原因非"用户停止"时卡片也有对应文案
    assert "进程已终止" in card.finished_detail_label.text()


def test_user_stop_does_not_signal_abnormal(page, manager, infobar):
    task_id, _ = _add_card(page, manager)
    received = []
    page.task_ended_abnormally.connect(lambda *args: received.append(args))

    page._on_task_stopped(task_id, "用户停止")

    assert received == []
    assert infobar.calls[-1][0] == "info"


def test_find_pids_by_name_excludes_own_process(page, monkeypatch):
    """被测的同名进程退出后，"重新监控"不能因为只剩本程序同名而监控起自己。"""
    monkeypatch.setattr(ProcessCollector, "get_all_processes", staticmethod(
        lambda: [(os.getpid(), "python.exe"), (99999, "python.exe"), (12345, "other.exe")]))
    assert page._find_pids_by_name("PYTHON.EXE") == [99999]

    monkeypatch.setattr(ProcessCollector, "get_all_processes", staticmethod(
        lambda: [(os.getpid(), "python.exe")]))
    assert page._find_pids_by_name("python.exe") == []


def test_exited_process_is_cleared_from_form(page, manager, infobar, monkeypatch):
    """被测进程退出后，表单里不能一直留着已不存在的 PID（再点开始才报"进程不存在"）。"""
    task_id, _ = _add_card(page, manager, pid=1002)
    page.process_combo.setCurrentIndex(1)
    assert page.pid_input.text() == "1002"

    monkeypatch.setattr(ProcessCollector, "is_process_running", lambda self: False)
    monkeypatch.setattr(ProcessCollector, "get_all_processes", staticmethod(
        lambda: [(1001, "alpha.exe"), (1003, "MPrintExp.exe")]))
    page._on_task_stopped(task_id, "进程已终止")

    assert page.pid_input.text() == ""
    assert page.process_combo.currentIndex() == -1
    assert page.process_combo.text() == ""
    assert all(page.process_combo.itemData(i)[0] != 1002
               for i in range(page.process_combo.count()))


def test_form_is_kept_when_selection_is_another_or_still_alive(page, manager, infobar, monkeypatch):
    task_id, _ = _add_card(page, manager, pid=1002)
    other_id, _ = _add_card(page, manager, pid=1002)

    # 表单选的是别的进程：不动
    page.process_combo.setCurrentIndex(0)
    monkeypatch.setattr(ProcessCollector, "is_process_running", lambda self: False)
    page._on_task_stopped(task_id, "进程已终止")
    assert page.pid_input.text() == "1001"
    assert page.process_combo.currentIndex() == 0

    # 选的就是它，但进程还活着（如用户手动停止）：不动
    page.process_combo.setCurrentIndex(1)
    monkeypatch.setattr(ProcessCollector, "is_process_running", lambda self: True)
    page._on_task_stopped(other_id, "用户停止")
    assert page.pid_input.text() == "1002"
    assert page.process_combo.currentIndex() == 1
