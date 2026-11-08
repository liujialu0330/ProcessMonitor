"""
导出命名模板（core.naming_template）：渲染规则表驱动、任务字段来源、各类文件的默认文件名，
应用配置项与设置页的"文件命名模板"卡片（实时预览、未知字段提示、保存往返、恢复默认），
以及三种导出的保存对话框确实用模板生成默认文件名。
"""
import itertools
import json
import os
import uuid
from datetime import datetime, timedelta

import pytest
from qfluentwidgets import qconfig

import app_config
from app_config import cfg
from core import naming_template as nt
from core.monitor_manager import MonitorManager
from core.naming_template import (
    DEFAULT_TEMPLATE, KIND_DETAIL, KIND_EVIDENCE, KIND_REPORT, KIND_SUMMARY, MAX_NAME_LENGTH,
    default_filename, fields_for_task, preview_filename, render_name, unknown_fields, uses_field)
from core.target_info import TargetInfo
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_analysis_page, make_services, make_setting_page
from tests.helpers import SyncHistoryRunner, process_events_until
from ui.widgets.analysis.toolbar import (EXPORT_CSV, EXPORT_EVIDENCE, EXPORT_IMAGE,
                                         EXPORT_REPORT)
from utils.metrics import MetricType

START = datetime(2026, 7, 16, 9, 30, 45)
# {date} 是导出时刻，不是任务开始时间 START
EXPORTED = datetime(2026, 7, 16, 18, 5, 9)
DATE = '20260716_180509'


def fields(**override):
    values = dict(process='demo', build='1.2.3.4', scenario='', note='回归', date=DATE,
                  kind='报告', pid='4321')
    values.update(override)
    return values


@pytest.fixture(autouse=True)
def _restore_template():
    """模板配置项是全进程单例：每个用例前后都复位（conftest 的复位表里没有它）。"""
    item = cfg.export_name_template
    item.value = item.defaultValue
    yield
    item.value = item.defaultValue


# ================================================================ 渲染规则（表驱动）


@pytest.mark.parametrize("template, override, expected", [
    # 默认模板：字段齐全
    (DEFAULT_TEMPLATE, {}, f"demo_1.2.3.4_回归_{DATE}"),
    # 空字段连同旁边的分隔符一起去掉：中间、开头、结尾、连续多个、全空
    (DEFAULT_TEMPLATE, dict(build=''), f"demo_回归_{DATE}"),
    (DEFAULT_TEMPLATE, dict(build='', note=''), f"demo_{DATE}"),
    (DEFAULT_TEMPLATE, dict(process=''), f"1.2.3.4_回归_{DATE}"),
    (DEFAULT_TEMPLATE, dict(process='', build=''), f"回归_{DATE}"),
    ('{process}_{note}', dict(note=''), "demo"),
    ('{date}_{note}_{build}', dict(note='', build=''), DATE),
    (DEFAULT_TEMPLATE, dict(process='', build='', note='', date=''), "task"),
    # 字段值里空白折成 "_"，字段值自己带的 "__" 不动
    (DEFAULT_TEMPLATE, dict(note='内存  增长\t明显'), f"demo_1.2.3.4_内存_增长_明显_{DATE}"),
    ('{process}_{note}', dict(note='a__b'), "demo_a__b"),
    # 字面文字：保留、同种分隔符折叠、其它分隔符与文字不动
    ('PM_{process}_{date}', {}, f"PM_demo_{DATE}"),
    ('{process}__{date}', {}, f"demo_{DATE}"),
    ('{process}_{build}_v1', dict(build=''), "demo_v1"),
    ('{process} ({build}) {date}', {}, f"demo (1.2.3.4) {DATE}"),
    ('{process}-{build}-{date}', dict(build=''), f"demo-{DATE}"),
    ('{kind}-{process}', {}, "报告-demo"),
    # Windows 非法字符与控制字符 → "-"（字段值、字面文字都一样）
    ('{process}_{note}', dict(note='a:b*c?d'), "demo_a-b-c-d"),
    ('{process}_{note}', dict(note='x"y<z>w|v'), "demo_x-y-z-w-v"),
    ('{process}_{note}', dict(note='p\\q/r'), "demo_p-q-r"),
    ('{process}_{note}', dict(note='a\x01b\x7fc'), "demo_a-b-c"),
    ('{process}/{date}', {}, f"demo-{DATE}"),
    # Windows 保留名加后缀（不分大小写，带不带扩展名都算）
    ('{process}', dict(process='con'), "con_file"),
    ('{process}', dict(process='PRN'), "PRN_file"),
    ('{process}', dict(process='Com1'), "Com1_file"),
    ('{process}', dict(process='lpt9'), "lpt9_file"),
    ('{process}', dict(process='NUL'), "NUL_file"),
    ('{process}', dict(process='console'), "console"),
    ('{process}', dict(process='com10'), "com10"),
    # 中文进程名
    (DEFAULT_TEMPLATE, dict(process='微信', build='', note='聊天内存'), f"微信_聊天内存_{DATE}"),
    # 未知字段原样保留（含花括号），已知字段照常渲染
    ('{process}_{foo}_{date}', {}, f"demo_{{foo}}_{DATE}"),
    ('{process}_{}_{date}', {}, f"demo_{{}}_{DATE}"),
    # 没有场景来源：场景恒空
    ('{process}_{scenario}_{date}', {}, f"demo_{DATE}"),
    # 类别字段
    ('{process}_{kind}_{date}', dict(kind='证据包'), f"demo_证据包_{DATE}"),
])
def test_render_name_table(template, override, expected):
    assert render_name(template, fields(**override)) == expected


def test_render_never_leaves_double_or_edge_underscores():
    """任意字段组合为空时都不出现 "__"、开头或结尾的 "_"（与 "-" 的写法同理）。"""
    names = ('process', 'build', 'note', 'date')
    for template in ('{process}_{build}_{note}_{date}', '{process}-{build}-{note}-{date}',
                     '{build}_{process}_{date}_{note}', '[{build}]_{process}_{note}'):
        for empties in itertools.chain.from_iterable(
                itertools.combinations(names, n) for n in range(len(names) + 1)):
            result = render_name(template, fields(**{name: '' for name in empties}))
            assert '__' not in result and '--' not in result, (template, empties, result)
            assert not result.startswith(('_', '-')) and not result.endswith(('_', '-')), (
                template, empties, result)
            assert result


def test_render_total_length_is_capped_and_keeps_the_date():
    long_process = 'x' * 300
    result = render_name(DEFAULT_TEMPLATE, fields(process=long_process))
    assert len(result) <= MAX_NAME_LENGTH
    assert result.endswith(DATE) and result.startswith('x')
    # 几个字段同时很长：最长的先被缩短，其余字段尽量保留
    result = render_name(DEFAULT_TEMPLATE, fields(process='p' * 200, build='v' * 50,
                                                  note='n' * 20))
    assert len(result) <= MAX_NAME_LENGTH
    assert 'v' * 50 in result and 'n' * 20 in result and result.endswith(DATE)
    # 字面文字过长没有字段可缩时硬截断
    assert len(render_name('a' * 500, fields())) <= MAX_NAME_LENGTH
    # 保留名加后缀之后同样不超长
    assert len(render_name('{process}', fields(process='con'))) <= MAX_NAME_LENGTH


def test_render_edge_cases():
    assert render_name('', fields()) == f"demo_1.2.3.4_回归_{DATE}"     # 空模板按默认
    assert render_name('{process}', {}) == "task"                       # 字段缺失按空
    assert render_name('{process}_', fields()) == "demo"                # 结尾分隔符去掉
    assert render_name('.{process}.', fields()) == "demo"               # 点不能结尾


def test_unknown_fields_and_uses_field():
    assert unknown_fields(DEFAULT_TEMPLATE) == []
    assert unknown_fields('{process}_{foo}_{bar}_{foo}') == ['{foo}', '{bar}']
    assert unknown_fields('{}') == ['{}']
    assert unknown_fields('{Process}') == ['{Process}']                 # 字段名区分大小写
    assert uses_field('{process}_{kind}', 'kind') and not uses_field(DEFAULT_TEMPLATE, 'kind')
    assert not uses_field('{kinds}', 'kind')


# ================================================================ 任务字段来源


def make_task(**override):
    values = dict(task_id=str(uuid.uuid4()), pid=4321, process_name='Demo.EXE',
                  metric_types=[MetricType.MEMORY_PRIVATE], interval=1.0, start_time=START,
                  end_time=START + timedelta(minutes=5), status='stopped', note='建模  200 次',
                  meta_json=TargetInfo(file_version='1.2.3.4').to_json())
    values.update(override)
    return MonitorTask(**values)


def test_fields_for_task_sources():
    task = make_task()
    got = fields_for_task(task, kind=KIND_REPORT, now=EXPORTED)
    assert got == dict(process='Demo', build='1.2.3.4', scenario='', note='建模 200 次',
                       date=DATE, kind='报告', pid='4321')
    # 版本：任务自己的 build 列（参考库预留）优先于被测目标元数据的文件版本
    task.build = '9.9'
    assert fields_for_task(task)['build'] == '9.9'
    # 取不到版本（旧任务、没有版本资源）为空；进程组任务没有 PID
    assert fields_for_task(make_task(meta_json=None))['build'] == ''
    assert fields_for_task(make_task(meta_json='不是 JSON'))['build'] == ''
    assert fields_for_task(make_task(pid=0))['pid'] == ''
    # 备注截断到 20 字；进程名只去掉结尾的 .exe
    assert fields_for_task(make_task(note='长' * 50))['note'] == '长' * 20
    assert fields_for_task(make_task(process_name='my.exe.exe'))['process'] == 'my.exe'
    assert fields_for_task(make_task(process_name='微信.exe'))['process'] == '微信'
    # 日期取导出时刻，与任务开始时间无关（含没有开始时间的任务）
    assert fields_for_task(make_task(start_time=None), now=datetime(2030, 1, 2, 3, 4, 5))[
        'date'] == '20300102_030405'
    assert fields_for_task(task, now=datetime(2030, 1, 2, 3, 4, 5))['date'] == '20300102_030405'


def test_date_defaults_to_the_current_time_when_no_export_time_is_given():
    before = datetime.now().replace(microsecond=0)
    got = datetime.strptime(fields_for_task(make_task())['date'], '%Y%m%d_%H%M%S')
    assert before <= got <= datetime.now()


def test_exporting_twice_gives_different_default_names():
    # 同一任务按不同范围连续导出：两次的默认名不同，不会每次都弹覆盖确认
    task = make_task()
    first = default_filename(DEFAULT_TEMPLATE, task, KIND_REPORT, now=EXPORTED)
    second = default_filename(DEFAULT_TEMPLATE, task, KIND_REPORT,
                              now=EXPORTED + timedelta(seconds=7))
    assert first != second and first.count('20260716_') == second.count('20260716_') == 1


def test_default_filename_suffixes_depend_on_kind_field():
    task = make_task()
    stem = f"Demo_1.2.3.4_建模_200_次_{DATE}"
    # 模板里没有 {kind}：沿用报告 _报告.png、摘要 _摘要.csv 的后缀
    assert default_filename(DEFAULT_TEMPLATE, task, KIND_REPORT, now=EXPORTED) == stem + "_报告.png"
    assert default_filename(DEFAULT_TEMPLATE, task, KIND_DETAIL, now=EXPORTED) == stem + ".csv"
    assert default_filename(DEFAULT_TEMPLATE, task, KIND_SUMMARY, now=EXPORTED) == stem + "_摘要.csv"
    assert default_filename(DEFAULT_TEMPLATE, task, KIND_EVIDENCE, now=EXPORTED) == stem + ".zip"
    # 有 {kind}：类别已经在名字里，只加扩展名
    template = '{process}_{kind}_{date}'
    assert default_filename(template, task, KIND_REPORT, now=EXPORTED) == f"Demo_报告_{DATE}.png"
    assert default_filename(template, task, KIND_DETAIL, now=EXPORTED) == f"Demo_明细_{DATE}.csv"
    assert default_filename(template, task, KIND_SUMMARY, now=EXPORTED) == f"Demo_摘要_{DATE}.csv"
    assert default_filename(template, task, KIND_EVIDENCE, now=EXPORTED) == f"Demo_证据包_{DATE}.zip"


def test_preview_uses_sample_task():
    assert preview_filename(DEFAULT_TEMPLATE) == "chrome_120.0.6099.109_内存回归_20260716_094530_报告.png"
    assert preview_filename('{process}_{kind}') == "chrome_报告.png"


# ================================================================ 配置项


def test_default_template_is_single_source():
    assert app_config.DEFAULT_EXPORT_NAME_TEMPLATE == DEFAULT_TEMPLATE
    assert cfg.export_name_template.defaultValue == DEFAULT_TEMPLATE
    assert app_config.get_export_name_template() == DEFAULT_TEMPLATE


@pytest.mark.parametrize("bad", ["", "   ", None, 123, "x" * 500])
def test_template_validator_falls_back_to_default(bad):
    validator = cfg.export_name_template.validator
    assert not validator.validate(bad)
    assert validator.correct(bad) == DEFAULT_TEMPLATE
    assert validator.validate("{process}_{date}")


def test_template_set_and_get_round_trip_through_file(tmp_path):
    path = tmp_path / "config.json"
    qconfig.load(str(path), cfg)
    app_config.set_export_name_template("  {process}_{kind}_{date}  ")
    assert app_config.get_export_name_template() == "{process}_{kind}_{date}"
    assert json.loads(path.read_text(encoding="utf-8"))["Export"]["NameTemplate"] == (
        "{process}_{kind}_{date}")
    app_config.set_export_name_template("   ")                  # 清空 = 回到默认
    assert app_config.get_export_name_template() == DEFAULT_TEMPLATE


# ================================================================ 设置页卡片


@pytest.fixture
def setting_page(qapp, db, tmp_path):
    qconfig.load(str(tmp_path / "setting.json"), cfg)
    page = make_setting_page(db, MonitorManager(db=db))
    page.resize(1000, 900)
    yield page
    page.close()
    page.deleteLater()


def test_setting_card_lives_in_export_group_and_shows_default(setting_page):
    page = setting_page
    card = page.name_template_card
    assert card.parent() is page.export_group
    assert card.titleLabel.text() == "文件命名模板"
    assert page.name_template_edit.text() == DEFAULT_TEMPLATE
    assert card.contentLabel.text() == (
        "预览：chrome_120.0.6099.109_内存回归_20260716_094530_报告.png")
    assert page.name_template_reset_button.text() == "恢复默认"


def test_setting_card_preview_is_live_but_saving_waits_for_editing_finished(setting_page):
    page = setting_page
    edit = page.name_template_edit
    edit.setText("{process}_{kind}_{date}")
    assert page.name_template_card.contentLabel.text() == "预览：chrome_报告_20260716_094530.png"
    # 键入过程中只刷新预览，不写配置
    assert app_config.get_export_name_template() == DEFAULT_TEMPLATE
    edit.editingFinished.emit()
    assert app_config.get_export_name_template() == "{process}_{kind}_{date}"


def test_setting_card_round_trip_reopen_page_and_file(setting_page, db, tmp_path):
    page = setting_page
    page.name_template_edit.setText("{process}-{date}")
    page.name_template_edit.editingFinished.emit()
    saved = json.loads((tmp_path / "setting.json").read_text(encoding="utf-8"))
    assert saved["Export"]["NameTemplate"] == "{process}-{date}"
    reopened = make_setting_page(db, MonitorManager(db=db))
    try:
        assert reopened.name_template_edit.text() == "{process}-{date}"
        assert reopened.name_template_card.contentLabel.text() == (
            "预览：chrome-20260716_094530_报告.png")
    finally:
        reopened.close()
        reopened.deleteLater()


def test_setting_card_warns_about_unknown_fields_and_keeps_them(setting_page):
    page = setting_page
    page.name_template_edit.setText("{process}_{foo}_{date}")
    content = page.name_template_card.contentLabel.text()
    assert content.splitlines()[0] == "预览：chrome_{foo}_20260716_094530_报告.png"
    assert "未知字段 {foo} 将原样写进文件名" in content and "{process}" in content
    assert page.name_template_card.height() > 70
    page.name_template_edit.setText("{process}_{date}")
    assert "未知字段" not in page.name_template_card.contentLabel.text()
    assert page.name_template_card.height() == 70


def test_setting_card_blank_input_and_reset_button(setting_page):
    page = setting_page
    page.name_template_edit.setText("{process}_{date}")
    page.name_template_edit.editingFinished.emit()
    page.name_template_edit.setText("   ")
    page.name_template_edit.editingFinished.emit()                 # 清空：回到默认并回填输入框
    assert app_config.get_export_name_template() == DEFAULT_TEMPLATE
    assert page.name_template_edit.text() == DEFAULT_TEMPLATE

    page.name_template_edit.setText("{note}")
    page.name_template_edit.editingFinished.emit()
    page.name_template_reset_button.click()
    assert page.name_template_edit.text() == DEFAULT_TEMPLATE
    assert app_config.get_export_name_template() == DEFAULT_TEMPLATE
    assert page.name_template_card.contentLabel.text().startswith(
        "预览：chrome_120.0.6099.109_内存回归_")


# ================================================================ 三种导出的默认文件名


BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE


@pytest.fixture
def export_env(qapp, db):
    manager = MonitorManager(db=db)
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
                       metric_types=[MEM], interval=1.0, start_time=BASE,
                       end_time=BASE + timedelta(seconds=600), status="stopped", note="建模 200 次")
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=i),
                                   102400.0 + i * 10.0, MEM) for i in range(600)])
    db.tasks.save_meta(task.task_id, TargetInfo(pid=4321, file_version="1.2.3.4").to_json())
    services = make_services(clock=FakeClock(BASE + timedelta(hours=2)))
    templates = [DEFAULT_TEMPLATE]
    page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                              defer=lambda fn: fn())
    page.export_flow._name_template = lambda: templates[0]
    page.resize(1280, 1600)
    page.show()
    page.select_task(task.task_id)
    yield page, services, templates
    page.auto_refresh.stop()
    process_events_until(lambda: services.threads.entries() == [], timeout=10)
    page.close()


def _default_path(page, services, kind):
    services.dialogs.push('ask_save_path', None)                    # 用户取消，只看默认名
    page.toolbar.export_requested.emit(kind)
    return services.dialogs.last_call('ask_save_path')['default_path']


@pytest.mark.parametrize("kind, suffix", [
    (EXPORT_REPORT, "_报告.png"), (EXPORT_IMAGE, "_报告.png"),
    (EXPORT_CSV, ".csv"), (EXPORT_EVIDENCE, ".zip"),
])
def test_save_dialogs_use_the_naming_template(export_env, kind, suffix):
    page, services, templates = export_env
    stem = "demo_1.2.3.4_建模_200_次_20260716_180000"
    assert os.path.basename(_default_path(page, services, kind)) == stem + suffix
    # 换模板立即生效（每次导出现读）
    templates[0] = "{process}-{date}"
    assert os.path.basename(_default_path(page, services, kind)) == "demo-20260716_180000" + suffix


def test_kind_field_names_each_file_by_its_own_kind(export_env, tmp_path):
    page, services, templates = export_env
    templates[0] = "{process}_{kind}_{date}"
    assert os.path.basename(_default_path(page, services, EXPORT_REPORT)) == (
        "demo_报告_20260716_180000.png")
    assert os.path.basename(_default_path(page, services, EXPORT_CSV)) == (
        "demo_明细_20260716_180000.csv")
    assert os.path.basename(_default_path(page, services, EXPORT_EVIDENCE)) == (
        "demo_证据包_20260716_180000.zip")

    # 报告三件套：没改默认名时 CSV 与摘要各取自己的类别
    out = tmp_path / "out"
    out.mkdir()
    services.dialogs.push('ask_save_path', str(out / "demo_报告_20260716_180000.png"))
    page.toolbar.export_requested.emit(EXPORT_REPORT)
    assert process_events_until(
        lambda: services.threads.entries() == [] and services.notifier.messages('success'),
        timeout=30)
    assert sorted(os.listdir(out)) == [
        "demo_报告_20260716_180000.png", "demo_摘要_20260716_180000.csv",
        "demo_明细_20260716_180000.csv"]


def test_kind_field_with_renamed_report_keeps_same_base(export_env, tmp_path):
    page, services, templates = export_env
    templates[0] = "{process}_{kind}_{date}"
    out = tmp_path / "out"
    out.mkdir()
    services.dialogs.push('ask_save_path', str(out / "自己起的名.png"))
    page.toolbar.export_requested.emit(EXPORT_REPORT)
    assert process_events_until(
        lambda: services.threads.entries() == [] and services.notifier.messages('success'),
        timeout=30)
    assert sorted(os.listdir(out)) == ["自己起的名.csv", "自己起的名.png", "自己起的名_摘要.csv"]
