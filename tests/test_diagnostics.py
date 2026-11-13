"""
诊断信息与诊断包（core.diagnostics）：文本含各节中文标签与关键数值；最近 50 行日志；
一节失败不连累其他节；诊断包清单与 app.log 截尾；启动阶段计时。
"""
import os
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import core.diagnostics as diagnostics
from core.diagnostics import (StartupTrace, bundle_members, collect_diagnostics,
                              write_diagnostic_bundle)
from core.self_monitor import SelfMonitor
from core.snapshot_hub import HubStats
from data.models import MonitorTask, QualityStats
from utils.metrics import MetricType

NOW = datetime(2026, 11, 7, 10, 30, 0)


class FixedClock:
    def __init__(self, mono: float = 0.0):
        self.mono = mono

    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return self.mono


class FakeHub:
    def __init__(self, broken=False):
        self.broken = broken

    def stats(self):
        if self.broken:
            raise RuntimeError("快照中心坏了")
        return HubStats(samples=120, hits=480, failures=1, avg_ms=11.25, last_ms=9.5)

    def available(self):
        return True


@pytest.fixture
def env(tmp_path, db):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text(
        "".join(f"日志第 {i} 行\n" for i in range(100)), encoding='utf-8')
    (log_dir / "crash.log").write_bytes(b"Timeout (0:00:10)!\nThread 0x1\n")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"interval": 1.0}', encoding='utf-8')
    paths = SimpleNamespace(data_dir=str(tmp_path), source='env', db_path=db.db_path,
                            log_dir=str(log_dir), config_path=str(config_path))

    task = MonitorTask(task_id='abcdef1234567890', pid=4321, process_name='demo.exe',
                       metric_types=['memory_private'], interval=1.0,
                       start_time=NOW - timedelta(hours=1), end_time=NOW, status='stopped')
    db.save_task(task)
    db.tasks.save_quality(task.task_id, QualityStats(12370, 12345, 20, 12, 5))
    db.save_task(MonitorTask(task_id='0000000000', pid=1, process_name='old.exe',
                             metric_types=['memory_private'], interval=1.0,
                             start_time=NOW - timedelta(days=1), end_time=None,
                             status='stopped'))

    clock = FixedClock()
    monitor = SelfMonitor(clock=clock, sampler=lambda: {
        MetricType.MEMORY_PRIVATE: 81920.0, MetricType.NUM_HANDLES: 512.0,
        MetricType.NUM_THREADS: 24.0, MetricType.NUM_GDI_OBJECTS: 60.0,
        MetricType.NUM_USER_OBJECTS: 40.0})
    for _ in range(3):
        monitor.tick()
        clock.mono += 60
    ctx = SimpleNamespace(paths=paths, db=db, hub=FakeHub(), self_monitor=monitor)
    return SimpleNamespace(ctx=ctx, paths=paths, log_dir=log_dir, tmp_path=tmp_path)


def test_diagnostics_text_has_all_sections(env):
    text = collect_diagnostics(env.ctx, clock=FixedClock())
    assert text.startswith("进程监控助手 诊断信息（生成于 2026-11-07 10:30:00）")
    for title in ("【版本与环境】", "【数据目录】", "【数据库】", "【任务与数据质量】",
                  "【快照中心】", "【自身资源】", "【界面看门狗】", "【崩溃日志】",
                  "【最近 50 行日志】"):
        assert title in text
    assert "应用版本: " in text and "psutil: " in text and "Qt: " in text
    assert "目录来源: env" in text
    assert "结构版本: 5" in text
    assert "库中任务: 2（有数据质量记录 1）" in text
    assert ("abcdef12 demo.exe 2026-11-07 09:30:00 采集完成率 99.8%"
            "（12,345 / 12,370 周期，跳过 20，调度延迟 p95 12 ms），采集 p95 5 ms") in text
    assert "取样 120 次，命中缓存 480 次，失败 1 次，平均 11.2 ms" in text
    assert "私有字节 81,920 KB，句柄 512" in text
    assert "缓冲样本: 3 / 1440" in text
    assert "私有字节判定: insufficient" in text
    assert "crash.log: 30 B" in text and "crash.log.1: 不存在" in text
    # 最近 50 行：第 50–99 行
    assert "日志第 99 行" in text and "日志第 50 行" in text
    assert "日志第 49 行" not in text


def test_failed_section_does_not_break_others(env):
    text = collect_diagnostics(env.ctx, hub=FakeHub(broken=True))
    assert "【快照中心】\n  读取失败: 快照中心坏了" in text
    assert "库中任务: 2" in text


def test_missing_parts_are_reported_not_raised():
    text = collect_diagnostics()
    assert "【数据库】\n  未提供" in text
    assert "【最近 50 行日志】\n  日志目录未知" in text
    with pytest.raises(TypeError):
        collect_diagnostics(unknown_part=1)


def test_manager_workers_quality_is_listed(env):
    worker = SimpleNamespace(task_id='fedcba9876543210', process_name='live.exe', pid=99,
                             state=SimpleNamespace(value='running'),
                             quality_snapshot=lambda: QualityStats(100, 100, 0, 1, 2, 3))
    manager = SimpleNamespace(get_all_tasks=lambda: [worker],
                              snapshot_hub=lambda: FakeHub())
    text = collect_diagnostics(manager=manager, log_dir=str(env.log_dir))
    assert "内存中任务: 1" in text
    assert ("fedcba98 live.exe PID 99 [running] 采集完成率 100.0%（100 / 100 周期，"
            "调度延迟 p95 1 ms），采集 p95 2 ms，写库 p95 3 ms") in text
    assert "取样 120 次" in text   # 没给 hub 时取 manager.snapshot_hub()


def test_bundle_contains_expected_members(env):
    out = env.tmp_path / "out"
    path = write_diagnostic_bundle(str(out), env.ctx, clock=FixedClock())
    assert os.path.basename(path) == "diagnostics_20261107_103000.zip"
    assert os.listdir(out) == ["diagnostics_20261107_103000.zip"]   # 没有残留临时文件
    with zipfile.ZipFile(path) as zf:
        assert sorted(zf.namelist()) == ["app.log", "config.json", "crash.log",
                                         "diagnostics.txt"]
        text = zf.read("diagnostics.txt").decode('utf-8')
        assert "【任务与数据质量】" in text
        assert zf.read("config.json") == b'{"interval": 1.0}'
        assert zf.read("app.log") == (env.log_dir / "app.log").read_bytes()


def test_bundle_keeps_only_tail_of_app_log(env, monkeypatch):
    monkeypatch.setattr(diagnostics, 'BUNDLE_LOG_TAIL_BYTES', 64)
    (env.log_dir / "crash.log.1").write_text("旧的崩溃日志", encoding='utf-8')
    path = write_diagnostic_bundle(str(env.tmp_path / "out"), env.ctx, clock=FixedClock())
    with zipfile.ZipFile(path) as zf:
        assert "crash.log.1" in zf.namelist()
        tail = zf.read("app.log")
        assert len(tail) == 64
        assert tail == (env.log_dir / "app.log").read_bytes()[-64:]


def test_bundle_members_skip_missing_files(tmp_path):
    paths = SimpleNamespace(log_dir=str(tmp_path / "nope"),
                            config_path=str(tmp_path / "nope.json"))
    assert bundle_members(paths=paths) == []


# ---------------------------------------------------------------- 启动阶段计时


def test_startup_trace_report():
    clock = FixedClock(mono=10.0)
    trace = StartupTrace(clock=clock)
    clock.mono = 10.120
    assert abs(trace.mark("解析路径") - 0.120) < 1e-9
    clock.mono = 10.500
    trace.mark("打开数据库")
    stages = trace.stages
    assert [s[0] for s in stages] == ["解析路径", "打开数据库"]
    assert abs(stages[1][1] - 380.0) < 1e-6 and abs(stages[1][2] - 500.0) < 1e-6
    report = trace.report()
    assert report.splitlines()[0] == "启动阶段计时（毫秒）"
    assert "打开数据库" in report and "380.0" in report and "500.0" in report
    assert "（无记录）" in StartupTrace(clock=clock).report()
