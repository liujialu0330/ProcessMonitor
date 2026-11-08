"""
分析页"导出报告 ▾ → 证据包（.zip）"的完整流程（离屏、真实后台线程）：默认文件名取
设置页的命名模板（core.naming_template）；三件套先写到临时工作目录、再连同 task.json / README.txt /
日志摘录打成一个 zip；完成提示带"打开文件夹"；导出登记 begin_export/end_export 与线程
登记表；失败常驻提示、不留半个 zip、工作目录清理干净。
"""
import json
import os
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta

import pytest

from core.evidence import LOG_EXCERPT_NAME, README_NAME, TASK_JSON_NAME
from core.monitor_manager import MonitorManager
from core.report import task_basename
from core.target_info import TargetInfo
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_analysis_page, make_services
from tests.helpers import SyncHistoryRunner, process_events_until
from ui.widgets.analysis.export_flow import EVIDENCE_DIALOG_TITLE, EVIDENCE_FILTER
from ui.widgets.analysis.toolbar import EXPORT_EVIDENCE, EXPORT_ITEMS
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
NOW = BASE + timedelta(hours=2)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
METRICS = [MEM, HANDLES]
SAMPLES = 600
EXE = r"C:\Apps\demo.exe"


def _seed(db, manager):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
                       metric_types=list(METRICS), interval=1.0, start_time=BASE,
                       end_time=BASE + timedelta(seconds=SAMPLES), status="stopped",
                       note="建模 200 次")
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(seconds=i),
                  102400.0 + i * 10.0 if m == MEM else 500.0, m)
        for i in range(SAMPLES) for m in METRICS])
    manager.add_marker(task.task_id, "基线", BASE + timedelta(seconds=100))
    db.add_marker(task.task_id, BASE + timedelta(seconds=300), "第 1 轮")
    info = TargetInfo(pid=4321, exe_path=EXE, file_version="1.2.3.4",
                      tool_version="2.0.0-beta.1")
    assert db.tasks.save_meta(task.task_id, info.to_json())
    return task


@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / "logs" / "app.log"
    path.parent.mkdir()
    return path


@pytest.fixture
def work_root(tmp_path, monkeypatch):
    """证据包临时工作目录的上级（系统临时目录换成用例目录，便于确认已清理）。"""
    root = tmp_path / "systemp"
    root.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(root))
    return root


@pytest.fixture
def env(qapp, db, log_file, work_root):
    manager = MonitorManager(db=db)
    services = make_services(clock=FakeClock(NOW), log_path=lambda: str(log_file))
    task = _seed(db, manager)
    short = task.task_id[:8]
    log_file.write_text(
        f"2026-07-16 16:00:00 [INFO] core.monitor_task: 任务启动: task_id={task.task_id}\n"
        "2026-07-16 16:05:00 [WARNING] core.collector: 指标暂不可用\n"
        "2026-07-16 15:00:00 [ERROR] other: 时间段之外\n"
        f"2026-07-16 16:06:00 [INFO] other: 无关的行 {short[:4]}\n",
        encoding='utf-8')
    page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                              defer=lambda fn: fn())
    page.resize(1280, 1600)
    page.show()
    page.select_task(task.task_id)
    yield page, task, services, manager
    page.auto_refresh.stop()
    process_events_until(lambda: services.threads.entries() == [], timeout=10)
    page.close()


def _wait_done(services, level):
    return process_events_until(
        lambda: services.threads.entries() == [] and services.notifier.messages(level),
        timeout=30)


def test_menu_has_evidence_item():
    assert (EXPORT_EVIDENCE, "证据包（.zip）") in [(k, text) for k, _icon, text in EXPORT_ITEMS]


def test_evidence_export_writes_complete_zip(env, tmp_path, work_root):
    page, task, services, manager = env
    out = tmp_path / "out"
    out.mkdir()
    chosen = str(out / "证据")      # 不带扩展名：自动补 .zip
    services.dialogs.push('ask_save_path', chosen)
    begun = []
    real_begin = manager.begin_export
    manager.begin_export = lambda: begun.append(1) or real_begin()

    page.toolbar.export_requested.emit(EXPORT_EVIDENCE)

    ask = services.dialogs.last_call('ask_save_path')
    assert ask['title'] == EVIDENCE_DIALOG_TITLE and ask['name_filter'] == EVIDENCE_FILTER
    assert os.path.basename(ask['default_path']) == "demo_1.2.3.4_建模_200_次_20260716_1600.zip"
    assert page.export_status_label.text() == "正在导出…"
    assert begun == [1] and manager.has_running_exports()
    assert [e.name for e in services.threads.entries()] == ["导出报告"]
    assert _wait_done(services, 'success')
    assert not manager.has_running_exports()

    zip_path = chosen + ".zip"
    assert os.listdir(out) == ["证据.zip"]
    base = task_basename(task)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        assert names == {f"{base}_报告.png", f"{base}.csv", f"{base}_摘要.csv",
                         TASK_JSON_NAME, LOG_EXCERPT_NAME, README_NAME}
        payload = json.loads(archive.read(TASK_JSON_NAME).decode('utf-8'))
        readme = archive.read(README_NAME).decode('utf-8-sig')
        excerpt = archive.read(LOG_EXCERPT_NAME).decode('utf-8-sig')
        png = archive.read(f"{base}_报告.png")
        detail = archive.read(f"{base}.csv").decode('utf-8-sig')

    assert payload['schema_version'] == 1
    assert payload['task']['task_id'] == task.task_id
    assert [m['key'] for m in payload['verdict']['metrics']] == METRICS
    assert payload['verdict']['overall'] in ('rising', 'stable', 'insufficient')
    assert payload['target']['exe_path'] == EXE
    assert sorted(payload['files']) == sorted(names)
    # 判定数值取自分析页当前的快照：与摘要表同一份数据
    mem = next(m for m in payload['verdict']['metrics'] if m['key'] == MEM)
    snapshot = page.vm.last_analysis.snapshots[MEM]
    assert mem['last'] == snapshot.stats['last'] and mem['samples'] == snapshot.stats['count']
    assert readme.splitlines()[0].startswith("结论：")
    assert f"被测程序: {EXE}" in readme
    assert "任务启动" in excerpt and "指标暂不可用" in excerpt
    assert "时间段之外" not in excerpt and "无关的行" not in excerpt
    assert png.startswith(b"\x89PNG")
    assert detail.startswith("# ")

    notice = services.notifier.last('success')
    assert notice.title == "导出成功" and "证据.zip" in notice.content
    page.export_flow.open_folder_button.click()
    assert services.dialogs.last_call('open_folder')['path'] == str(out)
    # 临时工作目录已删除
    assert os.listdir(work_root) == []


def test_evidence_export_asks_before_overwrite(env, tmp_path):
    page, _task, services, _manager = env
    target = tmp_path / "已有.zip"
    target.write_bytes(b"old")
    services.dialogs.push('ask_save_path', str(target))
    services.dialogs.push('confirm', False)
    page.toolbar.export_requested.emit(EXPORT_EVIDENCE)
    assert str(target) in services.dialogs.last_call('confirm')['content']
    assert services.threads.entries() == []
    assert target.read_bytes() == b"old"


def test_evidence_export_failure_leaves_nothing_behind(env, tmp_path, work_root):
    page, _task, services, manager = env
    chosen = str(tmp_path / "不存在的目录" / "证据.zip")
    services.dialogs.push('ask_save_path', chosen)
    page.toolbar.export_requested.emit(EXPORT_EVIDENCE)
    assert _wait_done(services, 'error')
    notice = services.notifier.last('error')
    assert notice.title == "导出失败" and notice.content.startswith("生成证据包失败：")
    assert not os.path.exists(chosen)
    assert os.listdir(work_root) == []
    assert not manager.has_running_exports()
    assert page.toolbar.export_button.isEnabled()


def test_evidence_without_log_path_omits_excerpt(qapp, db, tmp_path, work_root):
    manager = MonitorManager(db=db)
    services = make_services(clock=FakeClock(NOW))
    task = _seed(db, manager)
    page = make_analysis_page(db, manager, services, worker_runner=SyncHistoryRunner(db),
                              defer=lambda fn: fn())
    try:
        page.resize(1280, 1600)
        page.show()
        page.select_task(task.task_id)
        chosen = str(tmp_path / "e.zip")
        services.dialogs.push('ask_save_path', chosen)
        page.toolbar.export_requested.emit(EXPORT_EVIDENCE)
        assert _wait_done(services, 'success')
        with zipfile.ZipFile(chosen) as archive:
            assert LOG_EXCERPT_NAME not in archive.namelist()
    finally:
        page.auto_refresh.stop()
        process_events_until(lambda: services.threads.entries() == [], timeout=10)
        page.close()
