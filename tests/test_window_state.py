"""
窗口与工作状态记忆：纯函数 place_window / default_window_size（用替身屏幕列表）、配置存取的往返
与"没变不写盘"、主窗口的几何 / 最大化 / 当前页恢复、有运行中任务时固定监控页、防抖保存与
退出时保存、分析页上次任务 / 范围 / 视图的恢复与任务已删除时静默退回默认。
"""
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

import app_config
import config
from app.paths import AppPaths
from app_config import cfg
from core.history_query import RANGE_KEYS
from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock, FakePreferences, FakeWindowMemory, RecordingNotifier, ScriptedDialogs
from tests.factories import make_analysis_page, make_main_window, make_services
from tests.helpers import SyncHistoryRunner, dispose_widget, process_events_until, stop_and_join
from ui.services import ManagerTaskStatus
from ui.viewmodels.analysis_vm import TIME_RELATIVE, VIEW_OVERLAY
from ui.window_state import (
    DEFAULT_WINDOW_SIZE, MIN_VISIBLE_SIZE, PAGE_ANALYSIS, PAGE_MONITOR, PAGE_SETTING,
    AnalysisMemory, ConfigWindowMemory, WindowMemory, default_window_size, place_window,
)
from utils.metrics import MetricType

MIN = (config.WINDOW_MIN_WIDTH, config.WINDOW_MIN_HEIGHT)
SCREEN = (0, 0, 1920, 1040)                 # 1080p 去掉任务栏后的可用区
SECOND = (1920, 0, 1920, 1040)              # 右侧的第二块屏幕
LEFT = (-1280, 0, 1280, 984)                # 主屏左侧的第三块（负坐标）


# ========== 纯函数 ==========

def test_default_size_is_1280x820_on_large_screens_and_90_percent_on_small_ones():
    assert DEFAULT_WINDOW_SIZE == (1280, 820)
    assert default_window_size(SCREEN, MIN) == (1280, 820)
    assert default_window_size((0, 0, 1366, 728), MIN) == (1229, 655)       # 可用区 × 90%
    assert default_window_size((0, 0, 1280, 720), MIN) == (1152, 648)
    # 再小也不小于窗口的最小尺寸
    assert default_window_size((0, 0, 800, 600), MIN) == MIN


def test_no_record_centers_a_default_window_on_the_primary_screen():
    assert place_window(None, [SCREEN, SECOND], MIN) == (320, 110, 1280, 820)
    # 主屏不在原点（可用区在任务栏下方）时按可用区居中
    assert place_window(None, [(0, 40, 1366, 728)], MIN) == (68, 76, 1229, 655)


def test_saved_geometry_on_a_screen_is_kept():
    assert place_window((100, 120, 1100, 760), [SCREEN], MIN) == (100, 120, 1100, 760)
    # 在第二块屏幕上也沿用，包括负坐标的屏幕
    assert place_window((2000, 50, 1000, 700), [SCREEN, SECOND], MIN) == (2000, 50, 1000, 700)
    assert place_window((-1200, 20, 900, 700), [SCREEN, LEFT], MIN) == (-1200, 20, 900, 700)


@pytest.mark.parametrize("saved", [
    (3000, 100, 1000, 700),           # 记在第二块屏幕上，现在只剩一块
    (-1500, 100, 1000, 700),          # 记在左边的屏幕上
    (100, 2000, 1000, 700),           # 远在屏幕下方
    (1900 + 1000, 100, 1000, 700),    # 恰在屏幕右边缘之外
])
def test_geometry_outside_every_screen_falls_back_to_a_centered_default(saved):
    assert place_window(saved, [SCREEN], MIN) == (320, 110, 1280, 820)


def test_a_sliver_of_overlap_is_not_enough_but_a_visible_part_is():
    sliver = (SCREEN[2] - MIN_VISIBLE_SIZE[0] + 10, 100, 1000, 700)
    assert place_window(sliver, [SCREEN], MIN) == (320, 110, 1280, 820)
    visible = (SCREEN[2] - MIN_VISIBLE_SIZE[0] - 10, 100, 1000, 700)
    x, y, w, h = place_window(visible, [SCREEN], MIN)
    assert (w, h) == (1000, 700) and x + w <= SCREEN[0] + SCREEN[2]     # 挪回屏幕内


def test_oversized_or_overhanging_geometry_is_pulled_into_the_screen():
    assert place_window((-300, -50, 2400, 1200), [SCREEN], MIN) == (0, 0, 1920, 1040)
    assert place_window((1500, 900, 1000, 700), [SCREEN], MIN) == (920, 340, 1000, 700)


def test_saved_size_never_goes_below_the_minimum():
    assert place_window((10, 10, 300, 200), [SCREEN], MIN) == (10, 10, MIN[0], MIN[1])


def test_no_screen_information_is_treated_as_a_1080p_primary():
    assert place_window(None, [], MIN) == (320, 130, 1280, 820)


# ========== 配置存取 ==========

@pytest.fixture
def restore_cfg():
    items = [cfg.window_geometry, cfg.window_maximized, cfg.window_page, cfg.analysis_task,
             cfg.analysis_range, cfg.analysis_view_mode, cfg.analysis_time_mode]
    saved = [(item, item.value) for item in items]
    yield
    for item, value in saved:
        item.value = value


def test_config_memory_roundtrip(restore_cfg):
    store = ConfigWindowMemory(screens=lambda: [SCREEN])
    memory = WindowMemory(
        geometry=(100, 120, 1100, 760), maximized=True, page=PAGE_ANALYSIS,
        analysis=AnalysisMemory("t-1", "1h", VIEW_OVERLAY, TIME_RELATIVE))
    assert store.save(memory) is True
    assert store.load() == memory
    assert store.screens() == [SCREEN]


def test_config_memory_does_not_write_when_nothing_changed(restore_cfg, monkeypatch):
    store = ConfigWindowMemory()
    memory = WindowMemory(geometry=(1, 2, 900, 700), page=PAGE_SETTING)
    store.save(memory)
    writes = []
    monkeypatch.setattr(app_config.qconfig, "set", lambda *a, **k: writes.append(a))
    assert store.save(memory) is False
    assert writes == []


def test_config_memory_saves_all_changed_items_in_one_disk_write(restore_cfg, tmp_path):
    paths = AppPaths.under(str(tmp_path))
    app_config.load_app_config(paths)
    saves = []
    real_save = app_config.qconfig.save
    app_config.qconfig.save = lambda: (saves.append(1), real_save())
    try:
        ConfigWindowMemory().save(WindowMemory(
            geometry=(1, 2, 900, 700), maximized=True, page=PAGE_ANALYSIS,
            analysis=AnalysisMemory("t", "all", "small", "absolute")))
    finally:
        app_config.qconfig.save = real_save
    assert saves == [1]
    with open(paths.config_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["Window"] == {"Geometry": [1, 2, 900, 700], "Maximized": True,
                               "Page": "analysis"}
    assert saved["Analysis"]["LastTask"] == "t"


@pytest.mark.parametrize("bad", [[1, 2, 3], [1, 2, 0, 700], ["a", 1, 2, 3], "x", [1, 2, 3, True]])
def test_corrupted_geometry_in_config_means_no_record(restore_cfg, bad):
    cfg.window_geometry.value = bad
    assert ConfigWindowMemory().load().geometry is None


def test_unknown_page_key_falls_back_to_monitor(restore_cfg):
    cfg.window_page.value = "bogus"
    assert ConfigWindowMemory().load().page == PAGE_MONITOR


# ========== 主窗口 ==========

@pytest.fixture
def build_window(tmp_path, qapp, db):
    made = []

    def build(memory=None, *, running=False, **memory_kwargs):
        fake = memory or FakeWindowMemory(**memory_kwargs)
        manager = MonitorManager(db=db)
        if running:
            manager.has_running_tasks = lambda: True
        services = make_services(
            notifier=RecordingNotifier(), dialogs=ScriptedDialogs(),
            preferences=FakePreferences(), task_status=ManagerTaskStatus(manager),
            window_memory=fake)
        win = make_main_window(db, manager, services)
        win.about_group.check_update = lambda *args, **kwargs: None
        made.append((win, manager))
        return win, fake

    yield build
    for win, manager in made:
        win._state_timer.stop()
        stop_and_join(manager)
        dispose_widget(win)


def test_first_start_is_a_default_sized_centered_window_on_the_monitor_page(build_window):
    win, _ = build_window()
    assert win.geometry().getRect() == (320, 110, 1280, 820)
    assert not win.isMaximized()
    assert win.stackedWidget.currentWidget() is win.monitor_page


def test_geometry_roundtrip(build_window):
    win, fake = build_window()
    win.setGeometry(100, 120, 1100, 760)
    win._save_window_state()
    assert fake.memory.geometry == (100, 120, 1100, 760)

    again, _ = build_window(memory=fake)
    assert again.geometry().getRect() == (100, 120, 1100, 760)


def test_offscreen_geometry_falls_back_to_centered_default(build_window):
    saved = WindowMemory(geometry=(5000, 4000, 1000, 700))
    win, _ = build_window(memory=FakeWindowMemory(saved, screens=[SCREEN]))
    assert win.geometry().getRect() == (320, 110, 1280, 820)


def test_geometry_moves_back_when_the_second_monitor_is_gone(build_window):
    saved = WindowMemory(geometry=(2100, 100, 1000, 700))
    two, _ = build_window(memory=FakeWindowMemory(saved, screens=[SCREEN, SECOND]))
    assert two.geometry().getRect() == (2100, 100, 1000, 700)
    one, _ = build_window(memory=FakeWindowMemory(saved, screens=[SCREEN]))
    assert one.geometry().getRect() == (320, 110, 1280, 820)


def test_small_primary_screen_gets_ninety_percent_default(build_window):
    win, _ = build_window(screens=[(0, 0, 1366, 728)])
    assert win.geometry().getRect() == (68, 36, 1229, 655)


def test_maximized_roundtrip_keeps_the_restore_geometry(build_window, monkeypatch):
    # 无边框窗口基类在窗口状态变化时向系统查询窗口句柄；离屏平台没有真实句柄，换成空实现
    from qframelesswindow.utils import win32_utils
    monkeypatch.setattr(win32_utils, "isMaximized", lambda hwnd: False)
    win, fake = build_window()
    win.setGeometry(100, 120, 1100, 760)
    win.setWindowState(win.windowState() | Qt.WindowMaximized)
    win._save_window_state()
    assert fake.memory.maximized is True
    assert fake.memory.geometry == (100, 120, 1100, 760)       # 还原后的位置与大小

    again, _ = build_window(memory=fake)
    assert again.isMaximized()
    again.setWindowState(Qt.WindowNoState)
    assert again.geometry().getRect() == (100, 120, 1100, 760)


def test_page_roundtrip(build_window):
    win, fake = build_window()
    win.switchTo(win.setting_page)
    win._save_window_state()
    assert fake.memory.page == PAGE_SETTING
    again, _ = build_window(memory=fake)
    assert again.stackedWidget.currentWidget() is again.setting_page

    win.switchTo(win.analysis_page)
    win._save_window_state()
    third, _ = build_window(memory=fake)
    assert third.stackedWidget.currentWidget() is third.analysis_page


def test_running_tasks_pin_the_start_page_to_monitor(build_window):
    saved = WindowMemory(page=PAGE_SETTING)
    win, _ = build_window(memory=FakeWindowMemory(saved), running=True)
    assert win.stackedWidget.currentWidget() is win.monitor_page


def test_show_monitor_page_is_what_recovery_calls(build_window):
    win, _ = build_window(memory=FakeWindowMemory(WindowMemory(page=PAGE_SETTING)))
    assert win.stackedWidget.currentWidget() is win.setting_page
    win.show_monitor_page()
    assert win.stackedWidget.currentWidget() is win.monitor_page


def test_no_memory_service_means_no_persistence(tmp_path, qapp, db):
    manager = MonitorManager(db=db)
    services = make_services(preferences=FakePreferences(), notifier=RecordingNotifier(),
                             task_status=ManagerTaskStatus(manager))
    win = make_main_window(db, manager, services)
    try:
        win.switchTo(win.setting_page)
        win._save_window_state()                    # 没有记忆服务：什么都不做、不抛错
        assert win._state_tracking is False
    finally:
        stop_and_join(manager)
        dispose_widget(win)


def test_save_is_debounced_after_geometry_changes(build_window):
    win, fake = build_window()
    win.show()
    QApplication.processEvents()
    win._state_timer.stop()
    assert win._state_timer.isSingleShot() and win._state_timer.interval() == 1000
    win._state_timer.setInterval(40)
    fake.saves.clear()

    for i in range(6):                              # 一次拖动：很多次几何变化
        win.resize(1100 + i * 10, 760)
        win.move(50 + i * 5, 60)
    assert fake.saves == []                         # 过程中不写
    QTest.qWait(200)
    assert len(fake.saves) == 1                     # 停下后只写一次
    assert fake.memory.geometry[2:] == (1150, 760)


def test_page_change_also_schedules_a_save(build_window):
    win, fake = build_window()
    win.show()
    QApplication.processEvents()
    win._state_timer.setInterval(30)
    QTest.qWait(80)
    fake.saves.clear()
    win.switchTo(win.setting_page)
    QTest.qWait(150)
    assert [m.page for m in fake.saves] == [PAGE_SETTING]


def test_real_quit_saves_before_shutdown(build_window, monkeypatch):
    win, fake = build_window()
    order = []
    win.setGeometry(60, 70, 1000, 700)
    real_save = fake.save
    monkeypatch.setattr(fake, "save", lambda m: (order.append("save"), real_save(m))[1])
    monkeypatch.setattr(win, "_run_shutdown", lambda: order.append("shutdown") or True)
    monkeypatch.setattr(QApplication, "quit", MagicMock())
    win._really_quit = True
    win.close()
    assert order == ["save", "shutdown"]
    assert fake.memory.geometry == (60, 70, 1000, 700)


def test_save_failure_is_logged_and_does_not_break_quit(build_window, monkeypatch, caplog):
    win, fake = build_window()
    monkeypatch.setattr(fake, "save", MagicMock(side_effect=OSError("disk")))
    monkeypatch.setattr(win, "_run_shutdown", lambda: True)
    monkeypatch.setattr(QApplication, "quit", MagicMock())
    win._really_quit = True
    win.close()
    assert any("保存窗口状态失败" in r.getMessage() for r in caplog.records)
    QApplication.quit.assert_called_once()


# ========== 分析页 ==========

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE


def _seed(db, start=BASE, samples=120):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
                       metric_types=[MEM], interval=1.0, start_time=start,
                       end_time=start + timedelta(seconds=samples), status="stopped", note="")
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, start + timedelta(seconds=i),
                                   102400.0 + i, MEM) for i in range(samples)])
    return task


@pytest.fixture
def make_page(qapp, db):
    pages = []
    services = make_services(clock=FakeClock(BASE + timedelta(hours=2)))
    manager = MonitorManager(db=db)

    def build():
        page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                                  defer=lambda fn: fn())
        page.resize(1280, 1600)
        pages.append(page)
        return page

    yield build
    for page in pages:
        page.auto_refresh.stop()
        process_events_until(lambda: services.threads.entries() == [], timeout=10)
        page.close()


def test_analysis_memory_roundtrip(make_page, db):
    older = _seed(db, BASE)
    _seed(db, BASE + timedelta(hours=1))                    # 更新的任务：默认会自动选中它
    page = make_page()
    page.restore_memory(AnalysisMemory(older.task_id, "1h", VIEW_OVERLAY, TIME_RELATIVE))
    page.show()

    assert page.current_task_id == older.task_id            # 恢复用户上次选的，而不是最新
    assert page.vm.range_key == "1h" and page.toolbar.current_range() == "1h"
    assert page.vm.view_mode == VIEW_OVERLAY and page.vm.time_mode == TIME_RELATIVE
    assert page.memory_snapshot() == AnalysisMemory(older.task_id, "1h", VIEW_OVERLAY,
                                                    TIME_RELATIVE)

    again = make_page()
    again.restore_memory(page.memory_snapshot())
    again.show()
    assert again.current_task_id == older.task_id


def test_deleted_task_silently_falls_back_to_the_default(make_page, db):
    task = _seed(db)
    page = make_page()
    page.restore_memory(AnalysisMemory("deleted-task-id", "6h", "", ""))
    page.show()

    assert page.current_task_id == task.task_id             # 默认：自动选中最新任务
    assert page.vm.selection_is_auto is True
    assert page.services.notifier.messages() == []          # 静默：不弹任何提示
    assert page.vm.range_key == "6h"                        # 其余状态照常恢复
    assert page.memory_snapshot().task_id == ""             # 自动选中的不记


def test_auto_selected_task_and_custom_range_are_not_remembered(make_page, db):
    _seed(db)
    page = make_page()
    page.show()
    assert page.vm.selection_is_auto
    assert page.memory_snapshot().task_id == ""
    start = BASE + timedelta(seconds=10)
    assert page.vm.set_custom_range(start, start + timedelta(seconds=60))
    assert page.memory_snapshot().range_key == ""


def test_user_selected_task_is_remembered(make_page, db):
    first, second = _seed(db, BASE), _seed(db, BASE + timedelta(hours=1))
    page = make_page()
    page.show()
    page.select_task(first.task_id)
    assert page.memory_snapshot().task_id == first.task_id
    assert second.task_id != first.task_id


def test_unknown_memory_values_are_ignored(make_page, db):
    _seed(db)
    page = make_page()
    page.restore_memory(AnalysisMemory("", "weird", "weird", "weird"))
    page.show()
    assert page.vm.range_key in RANGE_KEYS
    assert page.vm.view_mode != "weird" and page.vm.time_mode != "weird"


def test_restored_selection_only_applies_when_nothing_was_chosen_yet(make_page, db):
    first, second = _seed(db, BASE), _seed(db, BASE + timedelta(hours=1))
    page = make_page()
    page.restore_memory(AnalysisMemory(first.task_id, "", "", ""))
    page.select_task(second.task_id)                         # 用户先点了别的（如从监控页跳转）
    page.show()
    assert page.current_task_id == second.task_id


# ========== 续采之后固定监控页（组合根） ==========

class _StubWindow:
    def __init__(self):
        from types import SimpleNamespace
        self.monitor_page = SimpleNamespace(presenter=SimpleNamespace(auto_marker=object()))
        self.shown_monitor = 0

    def show_monitor_page(self):
        self.shown_monitor += 1


@pytest.mark.parametrize("resumed, expected", [(("t1",), 1), ((), 0)])
def test_recovery_switches_to_the_monitor_page_only_when_tasks_were_resumed(
        qapp, monkeypatch, resumed, expected):
    from types import SimpleNamespace
    from app import bootstrap
    from core.session_recovery import RecoveryOutcome
    monkeypatch.setattr(bootstrap, "run_recovery",
                        lambda *a, **k: RecoveryOutcome(resumed, (), ()))
    monkeypatch.setattr("ui.dialogs.recover_dialog.default_prompt", lambda window: None)
    window = _StubWindow()
    ctx = SimpleNamespace(recovery_candidates=("candidate",))

    bootstrap._schedule_recovery(ctx, window)
    process_events_until(lambda: False, timeout=0.3)

    assert window.shown_monitor == expected
