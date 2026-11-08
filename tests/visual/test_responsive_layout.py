"""
响应式布局：800 / 1000 / 1280 / 1920 四档宽度的离屏双主题截图（--visual 才运行）。

高度 820；800 宽用高 700。覆盖监控页（任务卡片）、分析页（普通任务、进程组任务及其成员视图）。
分析页的数据：一条泄漏排查预设任务（私有字节与句柄数持续上涨，其余平稳），以及由真实的
进程组采集线程（假时钟 + 假进程树）写进临时库的进程组任务。

断言只管"截出来了、尺寸对、浅深不同、没有横向溢出"，画得好不好由人看。
"""
import dataclasses
from datetime import datetime, timedelta

import pytest
from PyQt5.QtWidgets import QScrollArea

from tests.doubles import FakeProcessQuery, ScriptedDialogs
from tests.group_doubles import ROOT_PID, inst
from tests.helpers import process_events_until
from tests.test_group_analysis import root
from tests.test_group_worker import run_group
from tests.test_monitor_worker_clock import started  # noqa: F401
from tests.visual.snapshot import load_image, pixel_difference_ratio
from tests.visual.test_pages_dual_theme import _FakeHotkeyBackend, _seed_leak_preset_task
from utils.metrics import MetricType

SIZES = {800: (800, 700), 1000: (1000, 820), 1280: (1280, 820), 1920: (1920, 820)}
# 各窗口宽度下页面区域（侧栏之外）所处的档位：800 的页面区域不足 900，1000 在 regular，
# 1280 扣掉展开的侧栏内容区约 1130，已达 wide（1100）
TIERS = {800: "compact", 1000: "regular", 1280: "wide", 1920: "wide"}
PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "CADModeler.exe", 1002.5)]
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
GDI = MetricType.NUM_GDI_OBJECTS
COUNT = 'process_count'
# 浅深两张图至少要有这么多像素不同
MIN_DIFF_RATIO = 0.01


def _frames(t, n):
    """renderer.exe 两个实例内存各涨 200 KB/周期，gpu.exe 降 400 KB/周期，合计持平。"""
    def values(memory, handles, gdi):
        return dict(memory_private=memory, num_handles=handles, num_gdi_objects=gdi)
    return [
        root(**values(150000.0, 800.0, 300.0)),
        inst('renderer.exe', 5001, ppid=ROOT_PID, **values(200000.0 + 200 * n, 400.0, 150.0)),
        inst('renderer.exe', 5002, ppid=ROOT_PID, **values(200000.0 + 200 * n, 400.0, 150.0)),
        inst('gpu.exe', 5003, ppid=ROOT_PID, **values(300000.0 - 400 * n, 300.0, 100.0)),
    ]


@pytest.fixture
def context(qapp, visual_data_dir, started):  # noqa: F811
    from app.bootstrap import build_context
    from ui.hotkey import MarkerHotkey

    ctx = build_context(visual_data_dir, retention_days=0)
    services = dataclasses.replace(
        ctx.services, process_query=FakeProcessQuery(PROCESSES),
        dialogs=ScriptedDialogs(ask_close_action='quit'))
    hotkey = MarkerHotkey(_FakeHotkeyBackend())
    hotkey.bind("Ctrl+Alt+M")
    group_worker = run_group(ctx.db, started, _frames, metrics=(MEM, HANDLES, GDI, COUNT),
                             cycles=140, interval=5.0, process_name="app.exe")
    pid_task_id = _seed_leak_preset_task(ctx.db, ctx.manager)
    yield ctx, services, hotkey, pid_task_id, group_worker.task_id
    process_events_until(lambda: not services.threads.entries(), timeout=10)
    ctx.manager.stop_all_tasks()
    ctx.manager.join_all(3000)
    ctx.db.close()


def _window_builder(ctx, services, hotkey, page_attr):
    def build():
        from ui.main_window import MainWindow
        window = MainWindow(ctx.db, ctx.manager, services, hotkey=hotkey)
        window.about_group.check_update = lambda *args, **kwargs: None
        window.switchTo(getattr(window, page_attr))
        return window
    return build


def assert_no_horizontal_overflow(page) -> None:
    """页面没有横向滚动条，内容也没有比可见区域更宽。"""
    assert isinstance(page, QScrollArea)
    bar = page.horizontalScrollBar()
    assert bar.maximum() == 0, f"页面出现横向滚动：{bar.maximum()}"
    assert page.widget().width() <= page.viewport().width()


def _settle_analysis(task_id, member=None, expand=False):
    def settle(window):
        page = window.analysis_page
        page.select_task(task_id)
        vm = page.vm
        assert process_events_until(lambda: vm.last_analysis is not None
                                    and vm.current_task_id == task_id, timeout=15)
        assert process_events_until(lambda: not window.services.threads.entries(), timeout=10)
        if member is not None:
            assert vm.select_member(member)
        if expand:
            page.trend_panel.expand_all()
        process_events_until(lambda: False, timeout=0.3)
        assert_no_horizontal_overflow(page)
        assert page.breakpoints.tier == TIERS[window.width()]
    return settle


def _assert_pair(light, dark, size) -> None:
    for path in (light, dark):
        image = load_image(path)
        assert (image.width(), image.height()) == size, path
    assert pixel_difference_ratio(light, dark) > MIN_DIFF_RATIO


@pytest.mark.parametrize("width", sorted(SIZES))
def test_analysis_pid_task_widths(visual_snapshot, context, width):
    ctx, services, hotkey, pid_task_id, _group_id = context
    build = _window_builder(ctx, services, hotkey, "analysis_page")
    light, dark = visual_snapshot(build, f"e1_analysis_{width}", SIZES[width],
                                  settle=_settle_analysis(pid_task_id))
    _assert_pair(light, dark, SIZES[width])


@pytest.mark.parametrize("width", sorted(SIZES))
def test_analysis_group_task_widths(visual_snapshot, context, width):
    ctx, services, hotkey, _pid_id, group_id = context
    build = _window_builder(ctx, services, hotkey, "analysis_page")
    light, dark = visual_snapshot(build, f"e1_analysis_group_{width}", SIZES[width],
                                  settle=_settle_analysis(group_id))
    _assert_pair(light, dark, SIZES[width])


@pytest.mark.parametrize("width", sorted(SIZES))
def test_analysis_group_member_widths(visual_snapshot, context, width):
    ctx, services, hotkey, _pid_id, group_id = context
    build = _window_builder(ctx, services, hotkey, "analysis_page")
    light, dark = visual_snapshot(build, f"e1_analysis_member_{width}", SIZES[width],
                                  settle=_settle_analysis(group_id, 'renderer.exe'))
    _assert_pair(light, dark, SIZES[width])


@pytest.mark.parametrize("width", sorted(SIZES))
def test_analysis_full_page_widths(visual_snapshot, context, width):
    """整页长图（窗口拉高到 2000）：看焦点图、小倍数列数与折叠行。"""
    ctx, services, hotkey, pid_task_id, _group_id = context
    build = _window_builder(ctx, services, hotkey, "analysis_page")
    light, dark = visual_snapshot(build, f"e1_analysis_full_{width}", (width, 2000),
                                  settle=_settle_analysis(pid_task_id, expand=True))
    _assert_pair(light, dark, (width, 2000))


def _add_cards(board) -> None:
    from core.group_aggregator import MemberSnapshot
    from ui.components.task_card import TaskCard

    now = datetime(2026, 11, 7, 10, 30, 0)
    specs = [
        ("pid", "demo.exe", 4321, [MEM, GDI], {MEM: 524288.0, GDI: 1450.0}, None),
        ("tree", "app.exe", 1002, [MEM, GDI, COUNT],
         {MEM: 884736.0, GDI: 1000.0, COUNT: 23.0},
         [MemberSnapshot('renderer.exe', "renderer.exe", 4)]),
    ]
    for index, (kind, name, pid, metrics, values, members) in enumerate(specs):
        card = TaskCard(f"t{index}", name, pid, metrics, parent=board, interval=2.0,
                        note="过夜浸泡", target_kind=kind)
        board.add_card(card)
        card.set_started_at(now - timedelta(minutes=95))
        card.update_values(values, now - timedelta(minutes=90))
        card.update_values({m: v * 1.05 for m, v in values.items()}, now)
        if members is not None:
            card.set_members(members, False)
    board.set_counts(2, 2)


@pytest.mark.parametrize("width", sorted(SIZES))
def test_monitor_page_widths(visual_snapshot, context, width):
    ctx, services, hotkey, _pid_id, _group_id = context
    build = _window_builder(ctx, services, hotkey, "monitor_page")

    def settle(window):
        assert process_events_until(
            lambda: not window.monitor_page.form.picker.is_loading())
        _add_cards(window.monitor_page.board)
        process_events_until(lambda: False, timeout=0.3)
        assert_no_horizontal_overflow(window.monitor_page)
    light, dark = visual_snapshot(build, f"e1_monitor_{width}", SIZES[width], settle=settle)
    _assert_pair(light, dark, SIZES[width])
