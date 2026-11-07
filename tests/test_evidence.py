"""
证据包：文件名；zip 清单齐全；task.json 可解析且判定数值等于分析快照的字段（不重算）；
README 首行整体判定与每指标行；日志摘录的时间段/关键字/续行/上限与轮转备份；
失败（目录不存在、写入中途失败、替换失败、缺输入文件、分析失败）不留半个 zip、不动已有目标。
"""
import json
import os
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

import config
import core.evidence as evidence
from core.analysis.intervals import mean_round_delta
from core.analysis.trend import Verdict
from core.evidence import (LOG_EXCERPT_NAME, README_NAME, TASK_JSON_NAME, EvidenceError,
                           build_evidence_package, evidence_filename, log_excerpt_lines)
from core.history_query import TaskAnalysis, build_request, run_history_query
from core.target_info import TargetInfo
from data.models import DataPoint, MonitorTask, QualityStats
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 8, 0, 0)
NOW = datetime(2026, 7, 16, 9, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
SAMPLES = 1200      # 每秒一点，20 分钟：跨度与点数都过判定门槛


class _Clock:
    def __init__(self, now=NOW):
        self._now = now

    def now(self):
        return self._now


def _seed(db, *, mem_slope=10.0, samples=SAMPLES, note="回归 200 次"):
    """私有字节按 mem_slope KB/秒线性上涨，句柄数恒定 500；基线 60 秒，三轮打点。"""
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
        metric_types=[MEM, HANDLES], interval=1.0, start_time=BASE,
        end_time=BASE + timedelta(seconds=samples), status="stopped", note=note,
        create_time=BASE.timestamp() - 30)
    db.save_task(task)
    db.save_data_points(
        [DataPoint(task.task_id, BASE + timedelta(seconds=i), 100_000.0 + mem_slope * i, MEM)
         for i in range(samples)]
        + [DataPoint(task.task_id, BASE + timedelta(seconds=i), 500.0, HANDLES)
           for i in range(samples)])
    if samples >= 1100:
        db.markers.add(task.task_id, BASE + timedelta(seconds=60), "基线", 'baseline')
        for offset, label in ((400, "第 1 轮"), (800, "第 2 轮"), (1100, "第 3 轮")):
            db.markers.add(task.task_id, BASE + timedelta(seconds=offset), label, 'user')
    return db.tasks.get(task.task_id)


def _analysis(db, task):
    return TaskAnalysis(task.task_id, 'all', {
        m: run_history_query(db, build_request(task.task_id, m, 'all', analysis=True))
        for m in task.metric_types})


def _target():
    return TargetInfo(pid=4321, exe_path=r"C:\Demo\demo.exe", file_version="1.2.3.4",
                      cmdline=("demo.exe", "--auto"), tool_version="2.0.0-beta.1",
                      tool_is_admin=False, os_version="Windows 11 10.0.26200",
                      logical_cores=16, total_memory_bytes=32 * 1024 ** 3,
                      captured_at=BASE)


def _three_files(directory, base="demo_4321_20260716_080000"):
    paths = (os.path.join(directory, base + "_报告.png"),
             os.path.join(directory, base + ".csv"),
             os.path.join(directory, base + "_摘要.csv"))
    for path, content in zip(paths, (b"\x89PNG fake", "# 进程: demo.exe\n时间\n".encode("utf-8-sig"),
                                     "指标\n".encode("utf-8-sig"))):
        with open(path, "wb") as f:
            f.write(content)
    return paths


def _write_log(path, task_id):
    short = task_id[:8]
    rotated = [
        "2026-07-16 07:59:00 [INFO] core: 早于任务开始 " + short,
        "2026-07-16 08:00:05 [INFO] core.monitor_task: 任务启动 task_id=" + task_id,
    ]
    current = [
        "2026-07-16 08:01:00 [INFO] core: 与本任务无关的行",
        "2026-07-16 08:02:00 [WARNING] core.collector: 采集超时",
        "2026-07-16 08:03:00 [ERROR] core.x: 出错了",
        "Traceback (most recent call last):",
        '  File "x.py", line 1',
        "2026-07-16 08:04:00 [INFO] core: 普通行",
        "  不属于匹配行的续行",
        "2026-07-16 08:20:01 [ERROR] core: 晚于任务结束",
    ]
    with open(path + ".1", "w", encoding="utf-8") as f:
        f.write("\n".join(rotated) + "\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(current) + "\n")


def _build(db, task, tmp_path, *, log=True, out_name="证据.zip", analysis=None, **kw):
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    png, detail, summary = _three_files(str(tmp_path))
    log_path = None
    if log:
        log_path = str(tmp_path / "app.log")
        _write_log(log_path, task.task_id)
    args = dict(report_png=png, detail_csv=detail, summary_csv=summary,
                out_path=str(out_dir / out_name), log_path=log_path, clock=_Clock())
    args.update(kw)
    return build_evidence_package(db, task, analysis or _analysis(db, task), **args), out_dir


def _read(zip_path, name, encoding="utf-8"):
    with zipfile.ZipFile(zip_path) as archive:
        return archive.read(name).decode(encoding)


# ---------------------------------------------------------------- 文件名


def test_evidence_filename():
    task = MonitorTask(task_id="t", pid=1, process_name="De:mo.EXE", metric_types=[MEM],
                       interval=1.0, start_time=BASE, end_time=None, status="stopped",
                       note="  回归 200 次 / 夜间 ")
    assert evidence_filename(task, _Clock()) == "De_mo_回归_200_次___夜间_20260716_090000.zip"
    assert (evidence_filename(replace(task, note=""), _Clock(), build="1.2.3.4", scenario="打开关闭")
            == "De_mo_1.2.3.4_打开关闭_20260716_090000.zip")
    # 备注过长截断
    long = evidence_filename(replace(task, note="长" * 100), _Clock())
    assert long == "De_mo_" + "长" * evidence.FILENAME_NOTE_MAX_CHARS + "_20260716_090000.zip"


# ---------------------------------------------------------------- 内容


def test_package_contents_and_verdict_match_snapshots(db, tmp_path):
    task = _seed(db)
    assert db.tasks.save_meta(task.task_id, _target().to_json())
    assert db.tasks.save_plan(task.task_id, '{"schema_version": 1, "timed_marker_seconds": 600}')
    assert db.tasks.save_quality(task.task_id, QualityStats(1200, 1199, 1, 12, 5))
    analysis = _analysis(db, task)
    # 传入的任务对象是旧的（没有元数据/方案/质量）：由库补全
    path, out_dir = _build(db, task, tmp_path, analysis=analysis)

    assert os.listdir(out_dir) == ["证据.zip"]
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert archive.testzip() is None
    assert names == ["demo_4321_20260716_080000_报告.png", "demo_4321_20260716_080000.csv",
                     "demo_4321_20260716_080000_摘要.csv", TASK_JSON_NAME, LOG_EXCERPT_NAME,
                     README_NAME]

    payload = json.loads(_read(path, TASK_JSON_NAME))
    assert payload['schema_version'] == 1
    assert payload['generated_at'] == "2026-07-16T09:00:00.000"
    assert payload['tool'] == {'name': config.APP_NAME, 'version': config.APP_VERSION}
    assert payload['task']['task_id'] == task.task_id
    assert payload['task']['start_time'] == "2026-07-16T08:00:00.000"
    assert payload['task']['process_create_time'] == "2026-07-16T07:59:30.000"
    assert payload['target'] == json.loads(_target().to_json())
    assert payload['quality']['planned_cycles'] == 1200
    assert payload['quality']['completion_ratio'] == pytest.approx(1199 / 1200)
    assert payload['plan'] == {"schema_version": 1, "timed_marker_seconds": 600}
    assert [m['label'] for m in payload['markers']] == ["基线", "第 1 轮", "第 2 轮", "第 3 轮"]
    assert payload['markers'][0]['kind'] == 'baseline'
    assert payload['analysis'] == {'range_key': 'all', 'baseline_only': True}
    assert payload['files'] == names

    verdict = payload['verdict']
    assert verdict['overall'] == 'rising'
    assert [m['key'] for m in verdict['metrics']] == [MEM, HANDLES]
    for item in verdict['metrics']:
        snapshot = analysis.snapshot(item['key'])
        assert item['verdict'] == str(snapshot.judgement.verdict)
        assert item['reason'] == snapshot.judgement.reason
        assert item['confidence'] == snapshot.judgement.confidence
        assert item['r2'] == snapshot.judgement.fit.r2
        assert item['rate_per_hour'] == snapshot.stats['slope_per_hour']
        assert item['first'] == snapshot.stats['first']
        assert item['last'] == snapshot.stats['last']
        assert item['samples'] == snapshot.stats['count']
        assert item['baseline'] == snapshot.baseline.value
        assert item['start'] == snapshot.baseline.value and item['start_source'] == 'baseline'
        assert item['delta'] == snapshot.stats['last'] - snapshot.baseline.value
        assert item['rate_per_round'] == mean_round_delta(snapshot.rounds)
        assert item['rounds'] == len(snapshot.rounds) == 3
        assert [r['delta'] for r in item['round_deltas']] == [r.delta for r in snapshot.rounds]
        assert item['rate_display'] == {'value': snapshot.rate.value, 'ci': snapshot.rate.ci,
                                        'unit_suffix': snapshot.rate.unit_suffix}
    mem, handles = verdict['metrics']
    assert mem['verdict'] == 'rising' and handles['verdict'] == 'stable'
    # 每秒 10 KB → 每小时 36000 KB；20 分钟跨度显示单位为 /分，ci 换算到每小时
    assert mem['rate_per_hour'] == pytest.approx(36000.0)
    snapshot = analysis.snapshot(MEM)
    assert mem['ci'] == pytest.approx(snapshot.rate.ci * 60)
    assert mem['rate_per_round'] == pytest.approx(10.0 * (340 + 400 + 300) / 3)


def test_readme_text(db, tmp_path):
    task = _seed(db)
    db.tasks.save_meta(task.task_id, _target().to_json())
    path, _ = _build(db, db.tasks.get(task.task_id), tmp_path)
    readme = _read(path, README_NAME, "utf-8-sig")
    lines = readme.split("\r\n")
    assert lines[0] == "结论：疑似泄漏——私有字节持续上涨"
    assert lines[1].startswith("私有字节：持续上涨 · 增速 +35.16 MB/小时 · R² 1.00")
    assert "R² 1.00" in lines[1] and "每轮 +3.39 MB（3 轮）" in lines[1]
    assert "较基线 +11.1 MB（98.26 MB → 109.37 MB）" in lines[1]
    assert lines[2].startswith("句柄数：平稳 · ")
    assert "较基线 ±0（500 个 → 500 个）" in lines[2]
    assert "任务：demo.exe（PID 4321）" in lines
    assert "备注：回归 200 次" in lines
    assert "时间：2026-07-16 08:00:00 ~ 2026-07-16 08:20:00" in lines
    assert r"被测程序: C:\Demo\demo.exe（文件版本 1.2.3.4）" in lines
    files_at = lines.index("文件清单：")
    assert [line.split("  —  ")[0].strip() for line in lines[files_at + 1:files_at + 7]] == [
        "demo_4321_20260716_080000_报告.png", "demo_4321_20260716_080000.csv",
        "demo_4321_20260716_080000_摘要.csv", TASK_JSON_NAME, LOG_EXCERPT_NAME, README_NAME]
    assert "生成时间：2026-07-16 09:00:00" in lines
    assert f"工具版本：{config.APP_NAME} {config.APP_VERSION}" in lines


def test_metric_line_variants():
    base = dict(key=MEM, name="私有字节", value_class='level', verdict='rising', reason='rising',
                rate_per_hour=36000.0, ci=1200.0, r2=0.93, rate_per_round=None, rounds=0,
                delta=2048.0, start=1024.0, start_source='first', baseline=None, last=3072.0)
    assert evidence.metric_line(base) == (
        "私有字节：持续上涨 · 增速 +35.16 MB/小时（±1.17 MB/小时） · R² 0.93 · "
        "较开始 +2 MB（1 MB → 3 MB）")
    assert evidence.metric_line({**base, 'verdict': 'not_judged', 'value_class': 'rate'}) == (
        "私有字节：累计量（不判定趋势） · 较开始 +2 MB（1 MB → 3 MB）")
    assert evidence.metric_line({**base, 'verdict': 'insufficient', 'reason': 'no_data'}) == (
        "私有字节：无数据")
    assert evidence.metric_line({**base, 'delta': None, 'start': None}).endswith("当前 3 MB")


def test_readme_first_line_for_stable_and_insufficient(db, tmp_path):
    stable = _seed(db, mem_slope=0.0)
    (tmp_path / "a").mkdir()
    path, _ = _build(db, stable, tmp_path / "a", log=False)
    assert _read(path, README_NAME, "utf-8-sig").split("\r\n")[0] == "结论：未见持续上涨"
    assert json.loads(_read(path, TASK_JSON_NAME))['verdict']['overall'] == 'stable'

    short = _seed(db, samples=20)
    (tmp_path / "b").mkdir()
    path, _ = _build(db, short, tmp_path / "b", log=False)
    assert _read(path, README_NAME, "utf-8-sig").startswith(
        "结论：数据不足，无法判定（采集时长或样本数不够）\r\n私有字节：数据不足")
    payload = json.loads(_read(path, TASK_JSON_NAME))
    assert payload['verdict']['overall'] == 'insufficient'
    assert payload['verdict']['metrics'][0]['reason'] == 'few_points'
    assert payload['verdict']['metrics'][0]['start_source'] == 'first'
    # 没有元数据、方案与质量：对应字段为 null，摘要不出被测程序行
    assert payload['target'] is None and payload['plan'] is None and payload['quality'] is None


def test_without_log_path_no_excerpt(db, tmp_path):
    task = _seed(db)
    path, _ = _build(db, task, tmp_path, log=False)
    with zipfile.ZipFile(path) as archive:
        assert LOG_EXCERPT_NAME not in archive.namelist()
    assert LOG_EXCERPT_NAME not in json.loads(_read(path, TASK_JSON_NAME))['files']
    assert LOG_EXCERPT_NAME not in _read(path, README_NAME, "utf-8-sig")


def test_unparseable_plan_kept_raw(db, tmp_path):
    task = _seed(db)
    db.tasks.save_plan(task.task_id, "{坏")
    path, _ = _build(db, task, tmp_path, log=False)
    payload = json.loads(_read(path, TASK_JSON_NAME))
    assert payload['plan'] is None and payload['plan_raw'] == "{坏"


# ---------------------------------------------------------------- 日志摘录


def test_log_excerpt_filters_by_range_id_and_level(db, tmp_path):
    task = _seed(db)
    path, _ = _build(db, task, tmp_path)
    text = _read(path, LOG_EXCERPT_NAME, "utf-8-sig")
    body = text.split("\r\n\r\n", 1)[1].rstrip("\r\n").split("\r\n")
    assert body == [
        "2026-07-16 08:00:05 [INFO] core.monitor_task: 任务启动 task_id=" + task.task_id,
        "2026-07-16 08:02:00 [WARNING] core.collector: 采集超时",
        "2026-07-16 08:03:00 [ERROR] core.x: 出错了",
        "Traceback (most recent call last):",
        '  File "x.py", line 1',
    ]
    assert task.task_id[:8] in text.split("\r\n")[1]


def test_log_excerpt_limit_and_missing_file(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("\n".join(f"2026-07-16 08:00:{i % 60:02d} [ERROR] x: {i}" for i in range(30)),
                   encoding="utf-8")
    lines, total, found = log_excerpt_lines(str(log), "abcdefgh-1", BASE, NOW, max_lines=10)
    assert len(lines) == 10 and total == 30 and found
    assert lines[0].endswith(": 0")
    assert log_excerpt_lines(str(tmp_path / "none.log"), "x", BASE, NOW) == ([], 0, False)
    text = evidence.log_excerpt_text(str(tmp_path / "none.log"),
                                     MonitorTask("abcdefgh-1", 1, "a.exe", [MEM], 1.0, BASE, NOW,
                                                 "stopped"), NOW)
    assert evidence.LOG_MISSING in text


# ---------------------------------------------------------------- 失败不留半个 zip


def test_missing_output_directory_leaves_nothing(db, tmp_path):
    task = _seed(db)
    png, detail, summary = _three_files(str(tmp_path))
    before = sorted(os.listdir(tmp_path))
    with pytest.raises(OSError):
        build_evidence_package(db, task, _analysis(db, task), report_png=png, detail_csv=detail,
                               summary_csv=summary, out_path=str(tmp_path / "无此目录" / "x.zip"),
                               log_path=None, clock=_Clock())
    assert sorted(os.listdir(tmp_path)) == before


def test_failure_mid_write_removes_tmp_and_keeps_target(db, tmp_path, monkeypatch):
    task = _seed(db)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "证据.zip"
    target.write_bytes(b"old")
    real = zipfile.ZipFile.writestr

    def _fail_on_readme(self, info, data, *args, **kwargs):
        name = info.filename if isinstance(info, zipfile.ZipInfo) else info
        if name == README_NAME:
            raise OSError("磁盘已满")
        return real(self, info, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'writestr', _fail_on_readme)
    with pytest.raises(OSError, match="磁盘已满"):
        _build(db, task, tmp_path, log=False)
    assert os.listdir(out_dir) == ["证据.zip"]
    assert target.read_bytes() == b"old"


def test_replace_failure_removes_tmp_and_keeps_target(db, tmp_path, monkeypatch):
    task = _seed(db)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "证据.zip"
    target.write_bytes(b"old")

    def _deny(src, dst):
        raise PermissionError("目标被占用")

    monkeypatch.setattr(evidence.os, 'replace', _deny)
    with pytest.raises(PermissionError):
        _build(db, task, tmp_path, log=False)
    assert os.listdir(out_dir) == ["证据.zip"]
    assert target.read_bytes() == b"old"


def test_missing_input_or_failed_analysis_writes_nothing(db, tmp_path):
    task = _seed(db)
    analysis = _analysis(db, task)
    with pytest.raises(EvidenceError, match="缺少文件"):
        _build(db, task, tmp_path, log=False, report_png=str(tmp_path / "没有.png"))
    assert os.listdir(tmp_path / "out") == []

    broken = TaskAnalysis(task.task_id, 'all', {
        MEM: replace(analysis.snapshot(MEM), error="disk I/O error", storage_error=True)})
    with pytest.raises(EvidenceError, match="disk I/O error"):
        _build(db, task, tmp_path, log=False, analysis=broken)
    assert os.listdir(tmp_path / "out") == []


def test_verdict_values_are_taken_from_snapshot_not_recomputed(db, tmp_path):
    """把快照里的判定换成别的值：task.json 跟着变，说明数值直接取自快照。"""
    task = _seed(db)
    analysis = _analysis(db, task)
    mem = analysis.snapshot(MEM)
    faked = replace(mem, judgement=replace(mem.judgement, verdict=Verdict.FLUCTUATING,
                                           reason='low_r2', confidence=0.123),
                    stats={**mem.stats, 'slope_per_hour': 42.0})
    analysis = TaskAnalysis(task.task_id, 'all', {**analysis.snapshots, MEM: faked})
    path, _ = _build(db, task, tmp_path, log=False, analysis=analysis)
    item = json.loads(_read(path, TASK_JSON_NAME))['verdict']['metrics'][0]
    assert (item['verdict'], item['reason'], item['confidence'], item['rate_per_hour']) == (
        'fluctuating', 'low_r2', 0.123, 42.0)



# ---------------------------------------------------------------- 统一判定口径：被测进程退出


def test_target_exit_before_stop_condition_is_target_exited(db, tmp_path):
    """停止原因由调用方传入：被测进程在方案的停止条件达成之前退出 → 整体 target_exited，
    与 pmctl verdict 同一口径（core.verdict）。"""
    from core.contracts import StopReason
    task = _seed(db)
    assert db.tasks.save_plan(task.task_id, '{"schema_version": 1, "stop_after_rounds": 5}')
    path, _ = _build(db, task, tmp_path, log=False, stop_reason=StopReason.PROCESS_EXITED)
    payload = json.loads(_read(path, TASK_JSON_NAME))
    assert payload['verdict']['overall'] == 'target_exited'
    assert payload['verdict']['stop_reason'] == 'process_exited'
    # 各指标的结论照常给出（仅供参考）
    assert {m['verdict'] for m in payload['verdict']['metrics']} == {'rising', 'stable'}
    assert _read(path, README_NAME, "utf-8-sig").split("\r\n")[0] == evidence.OVERALL_TARGET_EXITED


def test_target_exit_after_stop_condition_is_judged_normally(db, tmp_path):
    """停止条件（3 轮）已经达成之后才退出：照常判定，停止原因另附。"""
    from core.contracts import StopReason
    task = _seed(db)
    assert db.tasks.save_plan(task.task_id, '{"schema_version": 1, "stop_after_rounds": 3}')
    path, _ = _build(db, task, tmp_path, log=False, stop_reason=StopReason.PROCESS_EXITED)
    payload = json.loads(_read(path, TASK_JSON_NAME))
    assert payload['verdict']['overall'] == 'rising'
    assert payload['verdict']['stop_reason'] == 'process_exited'


def test_unknown_stop_reason_keeps_previous_payload(db, tmp_path):
    """调用方不给停止原因：按普通判定，task.json 不出现 stop_reason（与修改前一致）。"""
    task = _seed(db)
    path, _ = _build(db, task, tmp_path, log=False)
    verdict = json.loads(_read(path, TASK_JSON_NAME))['verdict']
    assert verdict['overall'] == 'rising' and 'stop_reason' not in verdict


def test_evidence_and_pmctl_verdict_agree(db):
    """同一份分析：证据包的整体判定与命令行 compute_verdict 一致（含被测进程退出）。"""
    from core.contracts import StopReason
    from core.ipc.commands import compute_verdict
    for slope in (10.0, 0.0):
        task = _seed(db, mem_slope=slope)
        analysis = _analysis(db, task)
        for reason, before in ((None, False), (StopReason.PROCESS_EXITED, True),
                               (StopReason.PROCESS_EXITED, False), (StopReason.USER, True)):
            ours = evidence.analysis_verdict(analysis, stop_reason=reason,
                                             stopped_before_condition=before)['overall']
            theirs = compute_verdict(analysis, reason, stopped_before_condition=before)['verdict']
            assert ours == theirs, (slope, reason, before)
