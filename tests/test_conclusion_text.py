"""
复制结论文字（core.conclusion_text）与分析页的"复制结论 ▾""复制路径"：

- 文字：元数据一行、整体结论与结论条同一句、各指标一行的数值与摘要表单元格一致、分段摘要、
  进程组成员结论；Markdown 表格列数一致、竖线转义；
- 分析页：结论条的"复制结论 ▾"下拉两项（纯文本 / Markdown），复制的内容取自结论条与摘要表
  当前显示的内容，成功提示；没有数据时不复制；
- 导出完成提示上的"复制路径"：单个文件复制完整路径，多个文件复制所在文件夹。
"""
import dataclasses
import os
import re
import uuid
from datetime import datetime, timedelta

import pytest

import tests.test_segment_verdict as seg
from core import conclusion_text as ct
from core.conclusion_text import (
    FORMAT_MARKDOWN, FORMAT_TEXT, MemberLine, MetricLine, build_conclusion, member_lines, render)
from core.monitor_manager import MonitorManager
from core.target_info import TargetInfo
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_analysis_page, make_services
from tests.helpers import SyncHistoryRunner, process_events_until
from tests.test_segment_verdict import FLAT, MEM, RISING, SHORT, _analysis, _seed
from ui import formatting as fmt
from ui.widgets.analysis.export_flow import COPY_PATH_TITLE, NO_DATA
from ui.widgets.analysis.toolbar import EXPORT_CSV, EXPORT_REPORT
from utils.metrics import MetricType

ACCESS_VIOLATION_LABEL = "异常退出 0xC0000005（访问冲突）"


@pytest.fixture(autouse=True)
def _crash_label(monkeypatch):
    monkeypatch.setattr(seg, 'CRASH_LABEL', ACCESS_VIOLATION_LABEL)


# ================================================================ 纯文字（不依赖 Qt）


def banner_of(task, analysis):
    """分析页结论条用的那条结论（verdict_summary → overall_summary），与页面同一函数。"""
    items = [(m, s if s.points else None) for m, s in analysis.snapshots.items()]
    return fmt.overall_summary(fmt.verdict_summary(items), analysis,
                               fmt.task_overall_verdict(task, analysis))


def table_rows(analysis):
    return [fmt.build_table_row(m, s) for m, s in analysis.snapshots.items()]


def conclusion_for(task, analysis, **options):
    summary = banner_of(task, analysis)
    return build_conclusion(
        task, analysis, verdict=summary.verdict, sentence=summary.sentence,
        metrics=[MetricLine.from_texts(r.texts, r.note) for r in table_rows(analysis)], **options)


@pytest.fixture
def rising(db):
    task, _ = _seed(db, [RISING], metrics=(MEM, seg.IO), baseline_at=1)
    for offset, label in ((200, "第 1 轮"), (400, "第 2 轮")):
        db.markers.add(task.task_id, seg.BASE + timedelta(seconds=offset), label, 'user')
    task = dataclasses.replace(
        task, note="建模  200 次", end_time=seg.BASE + timedelta(seconds=699),
        meta_json=TargetInfo(file_version="1.2.3.4").to_json())
    return task, _analysis(db, task)


def test_header_line_has_all_metadata(rising):
    task, analysis = rising
    lines = ct.to_text(conclusion_for(task, analysis)).splitlines()
    assert lines[0] == ("app.exe · 文件版本 1.2.3.4 · PID 100 · 2026-11-06 09:00:00 ~ 09:11:39 · "
                        "时长 11分钟 · 采集间隔 1 秒 · 备注：建模 200 次")


def test_header_omits_missing_parts_and_describes_running_and_group_tasks(db):
    task, _ = _seed(db, [FLAT])
    analysis = _analysis(db, task)
    # 没有文件版本、没有备注；进行中的任务结束写"进行中"，时长取分析范围的跨度
    header = ct.to_text(conclusion_for(task, analysis)).splitlines()[0]
    assert header == ("app.exe · PID 100 · 2026-11-06 09:00:00 ~ 进行中 · 时长 11分钟 · "
                      "采集间隔 1 秒")
    # 跨天结束时写完整日期
    over_night = dataclasses.replace(task, end_time=seg.BASE + timedelta(days=1, hours=1))
    assert "09:00:00 ~ 2026-11-07 10:00:00 · 时长 1天1小时" in ct.to_text(
        conclusion_for(over_night, analysis))
    # 进程树 / 同名进程：写目标类型，不写 PID
    tree = dataclasses.replace(task, target_kind='tree', pid=777)
    assert "进程树 · 根 PID 777" in ct.header_parts(tree, analysis, None)
    named = dataclasses.replace(task, target_kind='name', pid=0)
    parts = ct.header_parts(named, analysis, None)
    assert "同名进程" in parts and not any(p.startswith("PID") for p in parts)


def test_overall_line_is_exactly_the_banner_sentence(db):
    """整体结论一行 = 结论条的判定胶囊 + 主句，一字不差（多段 + 中途崩溃的复杂句子也一样）。"""
    for specs, crashes in (([RISING], ()), ([FLAT], ()), ([RISING, FLAT], ()),
                           ([FLAT, RISING, SHORT], (1,)), ([SHORT], ())):
        task, _ = _seed(db, specs, crashes=crashes)
        analysis = _analysis(db, task)
        summary = banner_of(task, analysis)
        conclusion = conclusion_for(task, analysis)
        expected = f"整体结论：{summary.verdict}。{summary.sentence}"
        assert ct.overall_line(conclusion) == expected
        assert expected in ct.to_text(conclusion).splitlines()
        assert f"**整体结论**：{summary.verdict}。{summary.sentence}" in ct.to_markdown(
            conclusion).splitlines()


def test_metric_lines_carry_the_summary_table_cells(rising):
    task, analysis = rising
    rows = table_rows(analysis)
    conclusion = conclusion_for(task, analysis)
    text_lines = [line for line in ct.to_text(conclusion).splitlines()
                  if line.startswith("- ") and "：" in line]
    assert len(text_lines) == len(rows) == 2
    assert any(r.text('per_round') not in ('--', '—') for r in rows)    # 不让每轮均增的断言空转
    for row, line in zip(rows, text_lines):
        assert line.startswith(f"- {row.text('metric')}：{row.text('verdict')}")
        for column in ('baseline', 'current', 'delta'):
            assert row.text(column) in line
        if row.text('per_round') not in ('--', '—'):
            assert f"每轮均增 {row.text('per_round')}" in line
        if row.text('rate') not in ('--', '—'):
            assert row.text('rate') in line
    # 持续上涨的行写着判定与增速
    assert "持续上涨" in text_lines[0] and "KB/分" in text_lines[0]


def test_metric_text_line_exact_format():
    line = MetricLine("私有字节", "持续上涨", "100 MB", "155 MB", "+55 MB (+55.0%)", "+5 MB",
                      "+35 MB/小时 · R² 0.99")
    assert ct.metric_text_line(line) == (
        "私有字节：持续上涨，基线 100 MB → 当前 155 MB（较基线 +55 MB (+55.0%)），"
        "每轮均增 +5 MB，增速 +35 MB/小时 · R² 0.99")


def test_stable_rows_leave_out_placeholder_dashes(db):
    task, _ = _seed(db, [FLAT])
    analysis = _analysis(db, task)
    line = ct.metric_text_line(MetricLine.from_texts(table_rows(analysis)[0].texts))
    assert line.startswith("私有字节：平稳，基线 ") and "—" not in line and "--" not in line


def test_rows_without_data_show_the_reason(db):
    line = ct.metric_text_line(MetricLine.from_texts(
        fmt.build_table_row(MEM, None).texts, note="所选范围内没有该指标的数据"))
    assert line == "私有字节：所选范围内没有该指标的数据"
    assert ct.metric_text_line(MetricLine.from_texts(fmt.build_table_row(MEM, None).texts)) == (
        "私有字节：无数据")


def test_segments_summary_only_when_restarted_or_crashed(db):
    plain, _ = _seed(db, [RISING])
    assert ct.to_text(conclusion_for(plain, _analysis(db, plain))).count("分段：") == 0
    restarted, _ = _seed(db, [RISING, FLAT])
    analysis = _analysis(db, restarted)
    text = ct.to_text(conclusion_for(restarted, analysis))
    assert f"分段：{ct.segments_summary(analysis)}" in text.splitlines()
    assert "第 1 段 持续上涨（私有字节）" in text and "第 2 段 平稳" in text
    assert f"**分段**：{ct.segments_summary(analysis)}" in ct.to_markdown(
        conclusion_for(restarted, analysis)).splitlines()
    crashed, _ = _seed(db, [FLAT, FLAT], crashes=(1,))
    crash_text = ct.to_text(conclusion_for(crashed, _analysis(db, crashed)))
    assert "中途异常退出 1 次" in crash_text.split("分段：")[1]


def test_basis_line_follows_the_conclusion(rising):
    task, analysis = rising
    text = ct.to_text(conclusion_for(task, analysis, basis="11分钟 · 700 点")).splitlines()
    assert text[2] == "统计依据：11分钟 · 700 点"
    assert text[1].startswith("整体结论：")
    assert "统计依据" not in ct.to_text(conclusion_for(task, analysis))


# ---------------------------------------------------------------- Markdown


def split_row(line):
    """按未转义的竖线切一行表格，返回单元格。"""
    assert line.startswith('|') and line.endswith('|')
    return [cell.strip() for cell in re.split(r'(?<!\\)\|', line)[1:-1]]


def tables_of(markdown):
    """文本里的全部表格 [(表头, 对齐行, 数据行们)]；表格必须连续、以空行或文末结束。"""
    tables, current = [], []
    for line in markdown.splitlines() + ['']:
        if line.startswith('|'):
            current.append(line)
        elif current:
            tables.append(current)
            current = []
    result = []
    for lines in tables:
        header, align, *body = [split_row(line) for line in lines]
        result.append((header, align, body))
    return result


def test_markdown_tables_are_well_formed(rising):
    task, analysis = rising
    markdown = ct.to_markdown(conclusion_for(task, analysis))
    (header, align, body), = tables_of(markdown)
    assert header == list(ct.METRIC_HEADERS)
    assert all(re.fullmatch(r':?-{3,}:?', cell) for cell in align) and len(align) == len(header)
    assert len(body) == 2 and all(len(row) == len(header) for row in body)
    # 表格前后各空一行：Markdown 渲染器才认它是表格
    lines = markdown.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith('|'))
    assert lines[first - 1] == ''
    # 单元格就是摘要表的文本
    for row, cells in zip(table_rows(analysis), body):
        assert cells[0] == row.text('metric') and cells[1] == row.text('verdict')
        assert cells[2] == row.text('baseline') and cells[3] == row.text('current')
        assert cells[4] == row.text('delta')


def test_markdown_escapes_pipes_and_newlines_in_cells():
    conclusion = ct.Conclusion(
        header="a", verdict="平稳", sentence="x", basis="",
        metrics=(MetricLine("名|字", "平稳", "1 | 2", "3\n4", "", "", ""),),
        segments="", members=(MemberLine("a|b.exe", 2, "平稳", ()),))
    first, second = tables_of(ct.to_markdown(conclusion))
    assert len(first[0]) == len(first[1]) == len(first[2][0]) == len(ct.METRIC_HEADERS)
    assert first[2][0][:4] == ["名\\|字", "平稳", "1 \\| 2", "3 4"]
    assert first[2][0][4:] == ["—", "—", "—"]            # 空单元格写 —，列数不变
    assert second[2][0] == ["a\\|b.exe", "2", "平稳", "—"]


def test_render_dispatch_and_unknown_format(rising):
    task, analysis = rising
    conclusion = conclusion_for(task, analysis)
    assert render(conclusion, FORMAT_TEXT) == ct.to_text(conclusion)
    assert render(conclusion, FORMAT_MARKDOWN) == ct.to_markdown(conclusion)
    assert '|' not in ct.to_text(conclusion)
    with pytest.raises(ValueError):
        render(conclusion, 'html')


# ================================================================ 分析页


BASE = datetime(2026, 7, 16, 16, 0, 0)
HANDLES = MetricType.NUM_HANDLES
EXE = r"C:\Apps\demo.exe"


def seed_page_task(db, manager):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
                       metric_types=[MEM, HANDLES], interval=1.0, start_time=BASE,
                       end_time=BASE + timedelta(seconds=600), status="stopped",
                       note="建模 200 次")
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(seconds=i),
                  102400.0 + i * 10.0 if m == MEM else 500.0, m)
        for i in range(600) for m in (MEM, HANDLES)])
    manager.add_marker(task.task_id, "基线", BASE + timedelta(seconds=100))
    db.tasks.save_meta(task.task_id, TargetInfo(pid=4321, exe_path=EXE,
                                                file_version="1.2.3.4").to_json())
    return task


@pytest.fixture
def env(qapp, db):
    manager = MonitorManager(db=db)
    services = make_services(clock=FakeClock(BASE + timedelta(hours=2)))
    task = seed_page_task(db, manager)
    page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                              defer=lambda fn: fn())
    page.resize(1280, 1600)
    page.show()
    page.select_task(task.task_id)
    yield page, task, services
    page.auto_refresh.stop()
    process_events_until(lambda: services.threads.entries() == [], timeout=10)
    page.close()


def copied(services):
    return services.dialogs.last_call('copy_text')['text']


def test_banner_has_copy_menu_with_text_and_markdown(env):
    page, _task, _services = env
    banner = page.verdict_banner
    assert banner.copy_button.isVisible() and banner.copy_button.text() == "复制结论"
    assert [a.text() for a in banner.copy_menu.actions()] == ["纯文本", "Markdown"]
    assert list(banner.copy_actions) == [FORMAT_TEXT, FORMAT_MARKDOWN]
    # 没有结论时按钮隐藏
    banner.clear()
    assert not banner.copy_button.isVisible() and banner.summary() is None


@pytest.mark.parametrize("fmt_key, label", [(FORMAT_TEXT, "纯文本"), (FORMAT_MARKDOWN, "Markdown")])
def test_copy_conclusion_matches_banner_and_summary_table(env, fmt_key, label):
    page, task, services = env
    page.verdict_banner.copy_actions[fmt_key].trigger()          # 用户从下拉里选
    text = copied(services)
    notice = services.notifier.last('success')
    assert (notice.title, notice.content) == ("复制成功", f"结论已复制到剪贴板（{label}）")
    assert notice.parent is page

    banner = page.verdict_banner
    assert banner.verdict() and banner.sentence()
    overall = f"{banner.verdict()}。{banner.sentence()}"
    assert (f"整体结论：{overall}" if fmt_key == FORMAT_TEXT else f"**整体结论**：{overall}") in (
        text.splitlines())
    assert text.splitlines()[0].startswith("demo.exe · 文件版本 1.2.3.4 · PID 4321 · ")
    assert "备注：建模 200 次" in text.splitlines()[0]
    assert f"统计依据：{banner.basis().removeprefix('依据：')}" in text.splitlines()

    rows = page.summary_table.rows()
    assert len(rows) == 2
    if fmt_key == FORMAT_TEXT:
        lines = [line for line in text.splitlines() if line.startswith("- ")]
        assert len(lines) == len(rows)
        for row, line in zip(rows, lines):
            assert line.startswith(f"- {row.text('metric')}：{row.text('verdict')}")
            for column in ('baseline', 'current', 'delta'):
                assert row.text(column) in line
    else:
        (header, _align, body), = tables_of(text)
        assert len(body) == len(rows)
        for row, cells in zip(rows, body):
            assert cells[:5] == [row.text('metric'), row.text('verdict'), row.text('baseline'),
                                 row.text('current'), row.text('delta')]


def test_copy_conclusion_follows_what_the_page_shows(env):
    """切到另一个范围，结论条与摘要表变了，复制的内容也随之变。"""
    page, _task, services = env
    page.vm.set_range('10m')
    page.verdict_banner.copy_actions[FORMAT_TEXT].trigger()
    after = copied(services)
    assert f"整体结论：{page.verdict_banner.verdict()}。{page.verdict_banner.sentence()}" in after
    assert page.verdict_banner.basis().removeprefix('依据：') in after


def test_copy_conclusion_without_data_warns(qapp, db):
    manager = MonitorManager(db=db)
    services = make_services(clock=FakeClock(BASE))
    page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                              defer=lambda fn: fn())
    page.show()
    try:
        assert page.export_flow.copy_conclusion(FORMAT_TEXT) is False
        assert services.notifier.last('warning').title == NO_DATA[0]
        assert services.dialogs.calls_of('copy_text') == []
    finally:
        page.close()


def test_copy_conclusion_failure_is_reported(env):
    page, _task, services = env
    services.dialogs.push('copy_text', OSError("剪贴板被占用"))
    assert page.export_flow.copy_conclusion(FORMAT_TEXT) is False
    assert services.notifier.last('error').title == "复制失败"
    assert services.notifier.messages('success') == []


# ---------------------------------------------------------------- 进程组：成员结论

from tests.test_group_analysis import (  # noqa: E402,F401 夹具
    leaking_children, rebuilt_children, root, seesaw_members)
from tests.test_monitor_worker_clock import started  # noqa: E402,F401 夹具
from tests.test_group_analysis_page import (  # noqa: E402,F401 夹具
    make_page, manager, member_runner, open_group, runner, services)


def test_group_conclusion_lists_members_in_total_view_only(make_page, seesaw_members, services):
    page = make_page()
    vm = open_group(page, seesaw_members)
    page.verdict_banner.copy_actions[FORMAT_TEXT].trigger()
    text = copied(services)
    members = text.split("成员结论：")[1].splitlines()[1:]
    assert members[0] == "- a.exe（1 个实例）：持续上涨（私有字节）"       # 持续上涨的在前
    assert [m.split("（")[0].removeprefix("- ") for m in members] == ["a.exe", "app.exe", "b.exe"]
    # 与成员结论折叠区的判定逐行一致
    panel = [(r[0], r[2]) for r in page.member_panel.row_texts()]
    assert [(m.removeprefix("- ").split("（")[0], m.split("：")[1].split("（")[0])
            for m in members] == panel
    # 合计视图的整体结论点名了上涨的成员，复制的就是这一句
    assert f"整体结论：{page.verdict_banner.verdict()}。{page.verdict_banner.sentence()}" in text

    page.verdict_banner.copy_actions[FORMAT_MARKDOWN].trigger()
    markdown = copied(services)
    header, _align, body = tables_of(markdown)[1]
    assert header == list(ct.MEMBER_HEADERS)
    assert body[0] == ["a.exe", "1", "持续上涨", "私有字节"]

    vm.select_member("a.exe")                                    # 成员视图：不附成员结论
    page.verdict_banner.copy_actions[FORMAT_TEXT].trigger()
    member_text = copied(services)
    assert "成员结论" not in member_text
    assert page.verdict_banner.sentence().startswith("成员 a.exe：")
    assert page.verdict_banner.sentence() in member_text


def test_member_lines_from_core_verdicts_alone(seesaw_members, db, make_page):
    page = make_page()
    open_group(page, seesaw_members)
    lines = member_lines(page.vm.last_analysis, {"a.exe": "A 进程"}, {"a.exe": 3})
    assert lines[0] == MemberLine("A 进程", 3, "持续上涨", ("私有字节",))
    assert [m.name for m in lines] == ["A 进程", "app.exe", "b.exe"]
    assert lines[1].instances is None and lines[1].verdict == "平稳"


# ================================================================ 复制路径


def export_and_wait(page, services, kind, chosen):
    services.dialogs.push('ask_save_path', chosen)
    page.toolbar.export_requested.emit(kind)
    assert process_events_until(
        lambda: services.threads.entries() == [] and services.notifier.messages('success'),
        timeout=30)


def test_copy_path_after_single_file_export(env, tmp_path):
    page, _task, services = env
    path = str(tmp_path / "明细 数据.csv")
    export_and_wait(page, services, EXPORT_CSV, path)
    flow = page.export_flow
    notice = services.notifier.last('success')
    assert notice.title == "导出成功"
    assert flow.open_folder_button in notice.bar.widgets
    assert flow.copy_path_button in notice.bar.widgets and flow.copy_path_button.text() == "复制路径"
    flow.copy_path_button.click()
    assert copied(services) == os.path.normpath(path)            # 单个文件：完整路径
    done = services.notifier.last('success')
    assert done.title == COPY_PATH_TITLE and "完整路径" in done.content


def test_copy_path_after_multi_file_export_copies_the_folder(env, tmp_path):
    page, _task, services = env
    out = tmp_path / "报告 输出"
    out.mkdir()
    export_and_wait(page, services, EXPORT_REPORT, str(out / "r_报告.png"))
    assert sorted(os.listdir(out)) == ["r.csv", "r_报告.png", "r_摘要.csv"]
    page.export_flow.copy_path_button.click()
    assert copied(services) == str(out)                          # 三个文件：所在文件夹
    assert "文件夹" in services.notifier.last('success').content


def test_copy_path_belongs_to_its_own_export(env, tmp_path):
    """先后两次导出：每次提示上的"复制路径"复制各自那次的路径。"""
    page, _task, services = env
    first, second = str(tmp_path / "第一次.csv"), str(tmp_path / "第二次.csv")
    export_and_wait(page, services, EXPORT_CSV, first)
    first_button = page.export_flow.copy_path_button
    services.notifier.clear()
    export_and_wait(page, services, EXPORT_CSV, second)
    assert page.export_flow.copy_path_button is not first_button
    first_button.click()
    assert copied(services) == os.path.normpath(first)
    page.export_flow.copy_path_button.click()
    assert copied(services) == os.path.normpath(second)


def test_copy_path_failure_is_reported(env, tmp_path):
    page, _task, services = env
    export_and_wait(page, services, EXPORT_CSV, str(tmp_path / "a.csv"))
    services.dialogs.push('copy_text', OSError("剪贴板被占用"))
    page.export_flow.copy_path_button.click()
    assert services.notifier.last('error').title == "复制失败"


def test_export_notices_go_through_route_with_meaningful_events(env, tmp_path):
    """导出流程的提示都经 Notifier.route：事件名区分成功 / 失败 / 复制，同类用事件名合并。"""
    from ui.services import (EVENT_COPY_DONE, EVENT_COPY_FAILED, EVENT_EXPORT_BLOCKED,
                             EVENT_EXPORT_DONE, EVENT_EXPORT_FAILED)
    page, _task, services = env
    export_and_wait(page, services, EXPORT_CSV, str(tmp_path / "a.csv"))
    done = services.notifier.last('success')
    assert (done.event, done.key) == (EVENT_EXPORT_DONE, EVENT_EXPORT_DONE)

    page.export_flow.copy_path_button.click()
    copied_notice = services.notifier.last('success')
    assert (copied_notice.event, copied_notice.key) == (EVENT_COPY_DONE, EVENT_COPY_DONE)

    services.dialogs.push('copy_text', OSError("剪贴板被占用"))
    page.export_flow.copy_path_button.click()
    assert services.notifier.last('error').event == EVENT_COPY_FAILED

    page.vm.export_failed.emit("导出失败：磁盘已满")
    assert services.notifier.last('error').event == EVENT_EXPORT_FAILED

    page.export_flow._warn(NO_DATA, 2000)
    blocked = services.notifier.last('warning')
    assert (blocked.event, blocked.key, blocked.duration) == (
        EVENT_EXPORT_BLOCKED, EVENT_EXPORT_BLOCKED, 2000)
