"""
进程组任务界面的离屏双主题截图（--visual 才运行，产物要人看）：

- group_form_pid / group_form_tree / group_form_name：新建任务表单的三种目标类型
  （进程树与同名进程多出"按成员数估算"的存储提示，同名进程的等待 / 启动选项置灰）；
- group_cards：任务面板里单个进程、进程树（成员种类超限）、同名进程三张卡片；
- group_analysis_totals：分析页（1280×820 主窗口）的合计视图——结论条点名上涨的成员、摘要表
  上限占比写"—"、成员结论折叠区；
- group_analysis_member：同一任务切到某个成员后的整页。

分析页的数据由真实的进程组采集线程（假时钟 + 假进程树）写进临时库：renderer.exe（两个实例）
内存持续上涨，gpu.exe 持续下降，合计持平——合计两项都"平稳"，但成员在涨。
断言只管"截出来了、尺寸对、关键控件可见、首屏装得下结论条与摘要表"，画得好不好由人看。
"""
import dataclasses

import pytest

from tests.doubles import FakePreferences, FakeProcessQuery, ScriptedDialogs
from tests.group_doubles import ROOT_PID, inst
from tests.helpers import process_events_until
from tests.test_group_analysis import root
from tests.test_group_worker import run_group
from tests.test_monitor_worker_clock import started  # noqa: F401
from tests.visual.test_pages_dual_theme import _FakeHotkeyBackend, _assert_pair
from tests.visual.snapshot import DEFAULT_SIZE
from utils.metrics import LEAK_CHECK_PRESET, MetricType

SIZE = DEFAULT_SIZE
PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "CADModeler.exe", 1002.5),
             (1003, "notepad.exe", 1003.5)]
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
GDI = MetricType.NUM_GDI_OBJECTS
COUNT = 'process_count'


def _frames(t, n):
    """app.exe 持平；renderer.exe 两个实例内存各涨 200 KB/周期；gpu.exe 降 400 KB/周期。"""
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
    worker = run_group(ctx.db, started, _frames, metrics=(MEM, HANDLES, GDI, COUNT),
                       cycles=140, interval=5.0, process_name="app.exe")
    yield ctx, services, hotkey, worker.task_id
    process_events_until(lambda: not services.threads.entries(), timeout=10)
    ctx.manager.stop_all_tasks()
    ctx.manager.join_all(3000)
    ctx.db.close()


# ---------------------------------------------------------------- 表单


def _form_builder(kind):
    def build():
        from ui.widgets.monitor.new_task_form import NewTaskForm
        form = NewTaskForm(FakeProcessQuery(PROCESSES), FakePreferences(default_interval=3))
        form.picker.refresh()
        form.set_metrics_summary(LEAK_CHECK_PRESET)
        form.kind_segmented.setCurrentItem(kind)
        form.picker.set_text("chrome.exe" if kind == 'name' else "CADModeler.exe · PID 1002")
        form.note_input.setText("过夜浸泡测试")
        return form
    return build


@pytest.mark.parametrize("kind", ["pid", "tree", "name"])
def test_new_task_form_kinds(visual_snapshot, kind):
    light, dark = visual_snapshot(_form_builder(kind), f"group_form_{kind}", (1180, 320))
    for path in (light, dark):
        assert path.is_file()


# ---------------------------------------------------------------- 卡片


def _cards_builder():
    from datetime import datetime, timedelta

    from core.group_aggregator import MemberSnapshot
    from ui.components.task_card import TaskCard
    from ui.widgets.monitor.task_board import TaskBoard

    def build():
        now = datetime(2026, 11, 7, 10, 30, 0)
        board = TaskBoard()
        # 进程树的成员名种类可能超过上限（20 种）；同名进程只有一种成员名，不会有这个提示
        tree_members = [MemberSnapshot(f'worker{i}.exe', f"worker{i}.exe", 1) for i in range(19)]
        tree_members.insert(0, MemberSnapshot('renderer.exe', "renderer.exe", 4))
        specs = [
            ("pid", "demo.exe", 4321, [MEM, GDI], {MEM: 524288.0, GDI: 1450.0}, None, False),
            ("tree", "app.exe", 1002, [MEM, GDI, COUNT],
             {MEM: 884736.0, GDI: 1000.0, COUNT: 23.0}, tree_members, True),
            ("name", "chrome.exe", 0, [MEM, COUNT], {MEM: 1572864.0, COUNT: 37.0},
             [MemberSnapshot('chrome.exe', "chrome.exe", 37)], False),
        ]
        for index, (kind, name, pid, metrics, values, members, truncated) in enumerate(specs):
            card = TaskCard(f"t{index}", name, pid, metrics, parent=board, interval=2.0,
                            note="过夜浸泡", target_kind=kind, members_truncated=truncated)
            board.add_card(card)
            card.set_started_at(now - timedelta(minutes=95))
            card.update_values(values, now - timedelta(minutes=90))
            card.update_values({m: v * 1.05 for m, v in values.items()}, now)
            if members is not None:
                card.set_members(members, truncated)
        board.set_counts(3, 5)
        return board
    return build


def test_group_cards(visual_snapshot):
    light, dark = visual_snapshot(_cards_builder(), "group_cards", (1180, 760))
    for path in (light, dark):
        assert path.is_file()


# ---------------------------------------------------------------- 分析页


def _analysis_builder(context, member):
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
        if member is not None:
            assert vm.select_member(member)
        process_events_until(lambda: False, timeout=0.3)
        viewport = page.viewport()
        from PyQt5.QtCore import QPoint
        for widget in (page.verdict_banner, page.summary_table):
            top = widget.mapTo(viewport, QPoint(0, 0)).y()
            assert top >= 0 and top + widget.height() <= viewport.height(), widget

    return build, settle


def test_analysis_totals_view(visual_snapshot, context):
    build, settle = _analysis_builder(context, None)
    light, dark = visual_snapshot(build, "group_analysis_totals", SIZE, settle=settle)
    _assert_pair(light, dark)


@pytest.mark.parametrize("member, name", [(None, "group_analysis_totals_full"),
                                         ('renderer.exe', "group_analysis_member_full")])
def test_analysis_full_page(visual_snapshot, context, member, name):
    """整页长图（窗口拉高到 1700）：看焦点图、小图与折叠行；不要求首屏断言。"""
    ctx, services, hotkey, task_id = context
    build, _settle = _analysis_builder(context, member)

    def settle(window):
        page = window.analysis_page
        page.select_task(task_id)
        vm = page.vm
        assert process_events_until(lambda: vm.last_analysis is not None, timeout=15)
        assert process_events_until(lambda: not services.threads.entries(), timeout=10)
        if member is not None:
            assert vm.select_member(member)
        process_events_until(lambda: False, timeout=0.3)
    light, dark = visual_snapshot(build, name, (1280, 1700), settle=settle)
    assert light.is_file() and dark.is_file()


def test_analysis_member_view(visual_snapshot, context):
    build, settle = _analysis_builder(context, 'renderer.exe')
    light, dark = visual_snapshot(build, "group_analysis_member", SIZE, settle=settle)
    _assert_pair(light, dark)
