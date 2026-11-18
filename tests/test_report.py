"""
报告导出：文件命名、CSV 元数据与空洞空行、摘要 CSV 内容、ReportWorker 写文件（临时文件
+ 原子替换、取消与失败不留残留且不动已有目标、信号收尾、只用注入的 db）、报告图片的
文字与离屏渲染。
"""
import csv
import json
import os
import uuid
from datetime import datetime, timedelta
from zipfile import ZipFile

import pytest
from PyQt5.QtGui import QColor, QImage

from core.report_worker import ReportJob, ReportWorker
from core.history_query import TaskAnalysis, build_request, run_history_query
from core.report import (
    ELAPSED_COLUMN_NAME, METADATA_PREFIX, SUMMARY_HEADER, ReportPaths,
    build_report_csv_header, downsample_footnote, format_elapsed, gap_threshold_seconds,
    export_scope, interval_spans, metadata_lines, paths_from_image_choice, report_rows, summary_rows,
    task_basename)
from core.workers.base import CancelToken
from data.errors import StorageError
from data.models import DataPoint, Marker, MonitorTask
from tests.helpers import process_events_until
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES


def _task(note="回归 200 次", metrics=(MEM, HANDLES)):
    return MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name="demo.exe",
        metric_types=list(metrics), interval=1.0, start_time=BASE,
        end_time=BASE + timedelta(minutes=10), status="stopped", note=note)


def _points(task, offsets):
    return [DataPoint(task.task_id, BASE + timedelta(seconds=s), 100.0 + s, m)
            for s in offsets for m in task.metric_types]


def _seed(db, offsets=range(100), markers=((10, "基线"), (40, "第 1 轮"), (70, "第 2 轮"))):
    task = _task()
    db.save_task(task)
    db.save_data_points(_points(task, offsets))
    for offset, label in markers:
        db.add_marker(task.task_id, BASE + timedelta(seconds=offset), label)
    return task


def _analysis(db, task):
    snapshots = {m: run_history_query(db, build_request(task.task_id, m, 'all', analysis=True))
                 for m in task.metric_types}
    return TaskAnalysis(task.task_id, 'all', snapshots)


# ---------------------------------------------------------------- 命名与元数据


def test_basename_and_paths(tmp_path):
    task = _task()
    task.process_name = 'a:b?.EXE'
    assert task_basename(task) == "a_b__4321_20260716_160000"
    paths = paths_from_image_choice(str(tmp_path / "x_报告.png"))
    assert paths == ReportPaths(str(tmp_path), "x")
    assert paths.all() == (str(tmp_path / "x_报告.png"), str(tmp_path / "x.csv"),
                           str(tmp_path / "x_摘要.csv"))
    assert paths_from_image_choice(str(tmp_path / "自定义.png")).basename == "自定义"


def test_metadata_lines_start_with_hash():
    lines = metadata_lines(_task(), exported_at=BASE + timedelta(hours=1), range_text="全部",
                           sample_count=1234, end_time=BASE + timedelta(minutes=10))
    assert all(line.startswith(METADATA_PREFIX) for line in lines)
    text = "\n".join(lines)
    for expected in ("进程: demo.exe (PID 4321)", "备注: 回归 200 次", "指标: 私有字节、句柄数",
                     "采集间隔: 1 秒", "范围: 全部", "采集次数: 1,234",
                     "导出时间: 2026-07-16 17:00:00", "说明: 空行表示"):
        assert expected in text
    running = metadata_lines(_task(note=""), exported_at=BASE, range_text="全部")
    assert "# 备注: 无" in running and "# 结束: 进行中" in running


def test_report_rows_elapsed_and_gap_blank_lines():
    task = _task()
    points = _points(task, [0, 1, 2, 10, 11])
    markers = [Marker(task.task_id, BASE + timedelta(seconds=1), "基线", 1, 'baseline')]
    rows = list(report_rows(task, points, markers, gap_threshold_seconds(task.interval)))
    assert build_report_csv_header(task) == [
        '时间', ELAPSED_COLUMN_NAME, '进程名称', 'PID', '私有字节(KB)', '句柄数(个)', '打点']
    assert [r[1] if r else None for r in rows] == ['0', '1', '2', None, '10', '11']
    assert rows[3] == []
    assert rows[1][-1] == "基线"
    assert rows[0][:4] == ['2026-07-16 16:00:00', '0', 'demo.exe', 4321]
    assert rows[0][4:6] == ['100.0000', '100']
    # 间隔未知时不插空行
    assert [] not in list(report_rows(task, points, markers, gap_threshold_seconds(0)))
    assert format_elapsed(1.5) == "1.500"


# ---------------------------------------------------------------- 摘要


def test_summary_rows_metric_by_interval(db):
    task = _seed(db)
    analysis = _analysis(db, task)
    rows = summary_rows(analysis.snapshots)
    assert len(SUMMARY_HEADER) == len(rows[0])
    by_metric = {}
    for row in rows:
        by_metric.setdefault(row[0], []).append(row)
    assert list(by_metric) == ["私有字节", "句柄数"]
    mem = by_metric["私有字节"]
    assert [r[2] for r in mem] == ["开始 → 基线", "基线 → 第 1 轮", "第 1 轮 → 第 2 轮",
                                   "第 2 轮 → 结束"]
    # 第 1 轮：[40, 70) 秒共 30 点，起值 140、终值 169、Δ 29
    first_round = mem[2]
    assert first_round[5:10] == ["30", 30, "140.0000", "169.0000", "29.0000"]
    assert first_round[12] == "0.0000"      # 与上轮（基线 → 第 1 轮 Δ 29）之差
    assert by_metric["句柄数"][2][7:10] == ["140", "169", "29"]
    assert mem[-1][6] == 30 and mem[-1][4] == "2026-07-16 16:01:39"


def test_intervals_match_split_intervals_semantics(db):
    """数据库聚合切出的区间与纯函数 split_intervals 的结果一致（左闭右开、末段闭区间）。"""
    from core.analysis.intervals import Boundary, split_intervals
    task = _seed(db)
    snapshot = _analysis(db, task).snapshot(MEM)
    points = [(BASE + timedelta(seconds=s), 100.0 + s) for s in range(100)]
    markers = db.markers.list(task.task_id)
    expected = split_intervals(points, [Boundary(m.timestamp, m.label, m.kind, m.id) for m in markers])
    got = snapshot.intervals
    assert got == expected
    assert interval_spans(got, markers)[0] == ("开始", "基线")


# ---------------------------------------------------------------- 线程


def _image():
    image = QImage(40, 20, QImage.Format_ARGB32)
    image.fill(QColor("#336699"))
    return image


def test_report_worker_writes_three_files(qapp, db, tmp_path):
    task = _seed(db, offsets=[*range(0, 50), *range(60, 100)])
    analysis = _analysis(db, task)
    paths = ReportPaths(str(tmp_path), task_basename(task))
    (tmp_path / os.path.basename(paths.csv)).write_text("旧文件", encoding="utf-8")
    job = ReportJob(task=task, csv_path=paths.csv, summary_path=paths.summary,
                    image_path=paths.image, image=_image(), metadata=("# 进程: demo.exe",),
                    summary_rows=tuple(tuple(r) for r in summary_rows(analysis.snapshots)),
                    gap_seconds=gap_threshold_seconds(task.interval))
    worker = ReportWorker(db, job)
    result = worker.execute(CancelToken())

    assert result.paths == paths.all()
    assert result.row_count == 90 and result.point_count == 180
    assert QImage(paths.image).size() == _image().size()
    with open(paths.csv, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["# 进程: demo.exe"]
    assert rows[1][:2] == ["时间", "已运行(秒)"]
    blank = [i for i, row in enumerate(rows) if not row]
    assert len(blank) == 1 and rows[blank[0] - 1][1] == "49" and rows[blank[0] + 1][1] == "60"
    with open(paths.summary, encoding="utf-8-sig") as f:
        summary = list(csv.reader(f))
    assert summary[1] == list(SUMMARY_HEADER)
    assert len(summary) == 2 + len(summary_rows(analysis.snapshots))
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


def test_report_worker_locks_csv_to_analyzed_baseline_snapshot(qapp, db, tmp_path):
    task = _seed(db, offsets=range(100), markers=())
    db.markers.add(task.task_id, BASE + timedelta(seconds=20), "基线", 'baseline')
    analysis = TaskAnalysis(task.task_id, 'baseline', {
        metric: run_history_query(db, build_request(task.task_id, metric, 'baseline', analysis=True))
        for metric in task.metric_types})
    scope = export_scope(analysis)
    assert (scope.since, scope.until, scope.sample_count) == (
        BASE + timedelta(seconds=20), BASE + timedelta(seconds=99), 80)

    # 模拟运行中任务在分析完成后继续写入：导出的明细必须仍与当前图和摘要一致。
    db.save_data_points(_points(task, [100, 101]))
    target = tmp_path / "范围锁定.csv"
    worker = ReportWorker(db, ReportJob(
        task=task, csv_path=str(target), data_since=scope.since, data_until=scope.until,
        metadata=tuple(metadata_lines(
            task, exported_at=BASE, range_text="自基线", sample_count=scope.sample_count,
            end_time=scope.until)),
        summary_rows=tuple(tuple(row) for row in summary_rows(analysis.snapshots))))
    result = worker.execute(CancelToken())

    with open(target, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header_index = next(i for i, row in enumerate(rows) if row and row[0] == '时间')
    data = [row for row in rows[header_index + 1:] if row]
    assert result.row_count == 80 and result.point_count == 160
    assert data[0][0] == "2026-07-16 16:00:20"
    assert data[-1][0] == "2026-07-16 16:01:39"
    assert all("16:00:00" not in row[0] for row in data)
    assert all("16:01:40" not in row[0] and "16:01:41" not in row[0] for row in data)
    assert "# 采集次数: 80" in [row[0] for row in rows if row]


def test_report_worker_cancel_keeps_existing_targets(qapp, db, tmp_path):
    task = _seed(db)
    tmp_path = tmp_path / "out"
    tmp_path.mkdir()
    target = tmp_path / "keep.csv"
    target.write_text("旧文件", encoding="utf-8")
    token = CancelToken()
    token.cancel()
    worker = ReportWorker(db, ReportJob(task=task, csv_path=str(target)))
    assert worker.execute(token) is None
    assert target.read_text(encoding="utf-8") == "旧文件"
    assert os.listdir(tmp_path) == ["keep.csv"]


def test_report_worker_image_failure_raises_and_cleans(qapp, db, tmp_path):
    task = _seed(db)
    tmp_path = tmp_path / "out"
    tmp_path.mkdir()
    worker = ReportWorker(db, ReportJob(task=task, image_path=str(tmp_path / "x.png"),
                                        image=None, csv_path=str(tmp_path / "x.csv")))
    with pytest.raises(OSError):
        worker.execute(CancelToken())
    assert os.listdir(tmp_path) == []


def _run_sync(worker):
    """同线程直接执行 run()（QThread 子类的 run 是普通方法），便于断言信号与文件状态"""
    from PyQt5.QtCore import Qt
    signals = {'result': [], 'failed': []}
    worker.result.connect(signals['result'].append, Qt.DirectConnection)
    worker.failed.connect(signals['failed'].append, Qt.DirectConnection)
    worker.run()
    return signals


def _out_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out


def test_report_worker_cancel_mid_write_removes_tmp_and_keeps_targets(
        qapp, db, tmp_path, monkeypatch):
    """写明细中途取消：已写出的临时文件（含摘要）全部删除、目标文件不变、不发任何信号"""
    import core.report_worker as report_worker_module
    task = _seed(db)
    out = _out_dir(tmp_path)
    csv_target, summary_target = out / "keep.csv", out / "keep_摘要.csv"
    csv_target.write_text("旧明细", encoding="utf-8")
    summary_target.write_text("旧摘要", encoding="utf-8")
    worker = ReportWorker(db, ReportJob(task=task, csv_path=str(csv_target),
                                        summary_path=str(summary_target)))
    real_rows = report_worker_module.report_rows
    seen_tmp = []

    def _cancel_after_first_row(*args, **kwargs):
        for i, row in enumerate(real_rows(*args, **kwargs)):
            if i == 1:
                seen_tmp.extend(sorted(p for p in os.listdir(out) if p.endswith(".tmp")))
                worker.cancel()
            yield row

    monkeypatch.setattr(report_worker_module, 'report_rows', _cancel_after_first_row)

    signals = _run_sync(worker)

    assert seen_tmp == ["keep.csv.tmp", "keep_摘要.csv.tmp"]   # 取消时临时文件确已写出
    assert signals == {'result': [], 'failed': []}
    assert sorted(os.listdir(out)) == ["keep.csv", "keep_摘要.csv"]
    assert csv_target.read_text(encoding="utf-8") == "旧明细"
    assert summary_target.read_text(encoding="utf-8") == "旧摘要"


def test_report_worker_replace_failure_keeps_target_and_emits_failed(
        qapp, db, tmp_path, monkeypatch):
    """目标被占用等导致 os.replace 失败：目标不变、临时文件被清理、发 failed 且不发 result"""
    task = _seed(db)
    out = _out_dir(tmp_path)
    target = out / "locked.csv"
    target.write_text("被占用的旧文件", encoding="utf-8")

    def _deny(src, dst):
        raise PermissionError("目标被占用")

    monkeypatch.setattr('core.report_worker.os.replace', _deny)
    worker = ReportWorker(db, ReportJob(task=task, csv_path=str(target)))

    signals = _run_sync(worker)

    assert signals['result'] == [] and len(signals['failed']) == 1
    assert "导出失败" in signals['failed'][0] and "目标被占用" in signals['failed'][0]
    assert os.listdir(out) == ["locked.csv"]
    assert target.read_text(encoding="utf-8") == "被占用的旧文件"


def test_report_worker_storage_error_mid_read_emits_failed(qapp, db, tmp_path, monkeypatch):
    """读库中途失败（仓储抛 StorageError）：走失败路径，不留临时文件、不发 result"""
    from data.errors import StorageError
    task = _seed(db)
    out = _out_dir(tmp_path)
    real = db.points.iter_batches

    def _failing(task_id, batch_size=5000, **_options):
        def gen():
            yield from list(real(task_id, batch_size))[:1]
            raise StorageError("流式读取数据点失败: disk I/O error")
        return gen()

    monkeypatch.setattr(db.points, 'iter_batches', _failing)
    worker = ReportWorker(db, ReportJob(task=task, csv_path=str(out / "x.csv")))

    signals = _run_sync(worker)

    assert signals['result'] == [] and len(signals['failed']) == 1
    assert "disk I/O error" in signals['failed'][0]
    assert os.listdir(out) == []


def test_report_worker_result_signal_is_last_action(qapp, db, tmp_path):
    """result 发出时收尾已全部完成：目标文件已就位、临时文件已不存在"""
    from PyQt5.QtCore import Qt
    task = _seed(db)
    out = _out_dir(tmp_path)
    target = out / "order.csv"
    worker = ReportWorker(db, ReportJob(task=task, csv_path=str(target)))
    observed = {}
    worker.result.connect(lambda _r: observed.update(files=sorted(os.listdir(out))),
                          Qt.DirectConnection)

    worker.run()

    assert observed == {'files': ["order.csv"]}


def test_report_worker_uses_injected_db_and_never_constructs_database(
        qapp, db, tmp_path, monkeypatch):
    """线程内不得新建 Database：构造会开写事务，与"清理并压缩数据库"抢写锁"""
    import core.report_worker as report_worker_module

    def _forbidden(*args, **kwargs):
        raise AssertionError("导出线程不应自行构造 Database")

    monkeypatch.setattr(report_worker_module, 'Database', _forbidden)
    task = _seed(db)
    target = tmp_path / "injected.csv"
    worker = ReportWorker(db, ReportJob(task=task, csv_path=str(target)))
    assert worker.db is db

    signals = _run_sync(worker)

    assert signals['failed'] == []
    assert len(signals['result']) == 1
    assert signals['result'][0].row_count == 100 and signals['result'][0].point_count == 200
    assert target.exists()


# ---------------------------------------------------------------- 图片


def test_report_text_and_png(qapp, db, tmp_path):
    from ui.widgets.analysis.report_sheet import build_report_text, render_report_image
    task = _seed(db)
    analysis = _analysis(db, task)
    text = build_report_text(db.get_task(task.task_id), analysis, "全部",
                             BASE + timedelta(hours=1))
    assert text.title == "demo.exe · PID 4321 · 回归 200 次 · 2 项指标：私有字节、句柄数"
    assert text.info == "范围：全部 · 时长 1分钟 · 采集间隔 1 秒 · 100 点"
    assert [line.split("：")[0] for line in text.conclusions] == ["私有字节", "句柄数"]
    assert text.footnote.startswith(downsample_footnote(200))

    image = render_report_image(text, analysis, width=1200)
    assert not image.isNull() and image.width() == 1200
    # 标题 + 信息 + 两行结论 + 一行小倍数图（两张）+ 脚注
    assert image.height() > 300
    background = image.pixel(1, 1)
    inked = sum(1 for x in range(0, 1200, 3) for y in range(0, image.height(), 3)
                if image.pixel(x, y) != background)
    assert inked > 1000          # 不是一张空白图：文字、坐标轴与曲线
    path = tmp_path / "r.png"
    assert image.save(str(path)) and QImage(str(path)).width() == 1200


# ---------------------------------------------------------------- 数据质量


def test_quality_text_and_metadata_line():
    from core.report import quality_text
    from data.models import QualityStats
    assert quality_text(None) == ""
    assert quality_text(QualityStats(0, 0, 0)) == ""
    assert (quality_text(QualityStats(12370, 12345, 0, 12, 5))
            == "采集完成率 99.8%（12,345 / 12,370 周期，调度延迟 p95 12 ms）")
    # 有缺采时舍入不会显示成满分；有跳过时注明
    assert (quality_text(QualityStats(100000, 99999, 1, None, None))
            == "采集完成率 99.9%（99,999 / 100,000 周期，跳过 1）")
    assert quality_text(QualityStats(50, 50, 0, 1, 1)).startswith("采集完成率 100.0%")

    task = _task()
    plain = metadata_lines(task, exported_at=BASE, range_text="全部")
    assert not any("数据质量" in line for line in plain)
    task.quality = QualityStats(12370, 12345, 0, 12, 5)
    lines = metadata_lines(task, exported_at=BASE, range_text="全部", sample_count=12345)
    assert ("# 数据质量: 采集完成率 99.8%（12,345 / 12,370 周期，调度延迟 p95 12 ms）"
            in lines)
    # 位置：采集次数之后、导出时间之前
    assert [line.split(":")[0] for line in lines[-4:]] == [
        "# 采集次数", "# 数据质量", "# 导出时间", "# 说明"]


# ---------------------------------------------------------------- 被测目标元数据


def test_metadata_lines_append_target_info_when_present():
    from core.target_info import TargetInfo
    task = _task()
    plain = metadata_lines(task, exported_at=BASE, range_text="全部")
    assert not any(line.startswith(("# 被测程序", "# 命令行", "# 环境")) for line in plain)

    task.meta_json = TargetInfo(exe_path=r"C:\Demo\demo.exe", file_version="1.2.3.4",
                                cmdline=("demo.exe", "--auto"), os_version="Windows 11 10.0.26200",
                                logical_cores=16, total_memory_bytes=32 * 1024 ** 3,
                                tool_version="2.0.0-beta.1", tool_is_admin=True).to_json()
    lines = metadata_lines(task, exported_at=BASE, range_text="全部", sample_count=10,
                           extra=["依据: 基线之后"])
    assert lines[-6:] == [
        "# 依据: 基线之后",
        r"# 被测程序: C:\Demo\demo.exe（文件版本 1.2.3.4）",
        "# 命令行: demo.exe --auto",
        "# 环境: Windows 11 10.0.26200 · 16 核 · 32 GB · 工具 2.0.0-beta.1（管理员）",
        "# 导出时间: 2026-07-16 16:00:00",
        lines[-1],
    ]
    assert lines[-1].startswith("# 说明: ")
    # 解析失败忽略，其余元数据照常
    task.meta_json = "{坏"
    assert metadata_lines(task, exported_at=BASE, range_text="全部") == plain


def test_report_image_info_line_appends_quality(qapp, db):
    """报告图片信息行（依据行）末尾追加数据质量一句话；没有质量数据时不追加。"""
    from data.models import QualityStats
    from ui.widgets.analysis.report_sheet import build_report_text, render_report_for
    task = _seed(db)
    analysis = _analysis(db, task)
    stored = db.get_task(task.task_id)
    plain = build_report_text(stored, analysis, "全部", BASE + timedelta(hours=1))
    assert "采集完成率" not in plain.info

    stored.quality = QualityStats(12370, 12345, 0, 12, 5)
    text = build_report_text(stored, analysis, "全部", BASE + timedelta(hours=1))

    assert text.info == (plain.info + " · 采集完成率 99.8%（12,345 / 12,370 周期，"
                         "调度延迟 p95 12 ms）")
    image = render_report_for(stored, analysis, BASE + timedelta(hours=1), "全部")
    assert not image.isNull()


# ---------------------------------------------------------------- 报告图片的结构化数据


def _seed_rounds(db, markers):
    """每秒一个点、线性上涨（私有字节每秒 +1、句柄数每秒 +2），按 markers 打点。"""
    task = _task()
    db.save_task(task)
    points = []
    for s in range(100):
        ts = BASE + timedelta(seconds=s)
        points.append(DataPoint(task.task_id, ts, 100.0 + s, MEM))
        points.append(DataPoint(task.task_id, ts, 50.0 + 2 * s, HANDLES))
    db.save_data_points(points)
    for offset, label, kind in markers:
        db.markers.add(task.task_id, BASE + timedelta(seconds=offset), label, kind=kind)
    return task


def test_round_delta_rows_one_row_per_round_between_markers(db):
    from core.report import round_delta_rows
    task = _seed_rounds(db, ((10, "基线", 'baseline'), (40, "第 1 轮", 'user'),
                             (70, "第 2 轮", 'user'), (90, "第 3 轮", 'user')))
    rows = round_delta_rows(_analysis(db, task))

    # 预热段（开始 → 基线）与末段（第 3 轮 → 结束）不是"一轮"，不出行
    assert [row.label for row in rows] == ["基线 → 第 1 轮", "第 1 轮 → 第 2 轮",
                                         "第 2 轮 → 第 3 轮"]
    assert [row.duration_seconds for row in rows] == [30.0, 30.0, 20.0]
    # 口径同打点区间表：左闭右开，Δ = 区间内末个样本 - 首个样本
    assert [row.deltas[MEM] for row in rows] == [29.0, 29.0, 19.0]
    assert [row.deltas[HANDLES] for row in rows] == [58.0, 58.0, 38.0]
    assert list(rows[0].deltas) == [MEM, HANDLES]
    # 与上轮差相对表内上一行；第一轮没有上一轮
    assert rows[0].vs_previous == {MEM: None, HANDLES: None}
    assert rows[1].vs_previous == {MEM: 0.0, HANDLES: 0.0}
    assert rows[2].vs_previous == {MEM: -10.0, HANDLES: -20.0}
    # 与分析页打点区间表同一份数据
    snapshot = _analysis(db, task).snapshots[MEM]
    by_start = {item.start: item for item in snapshot.intervals}
    for row in rows:
        assert by_start[row.start].delta == row.deltas[MEM]
        assert by_start[row.start].end == row.end


def test_round_delta_rows_need_two_markers(db):
    from core.report import round_delta_rows
    no_markers = _seed_rounds(db, ())
    assert round_delta_rows(_analysis(db, no_markers)) == []
    one = _seed_rounds(db, ((10, "基线", 'baseline'),))
    assert round_delta_rows(_analysis(db, one)) == []
    # 没有基线时用户打点之间同样是一轮；暂停/恢复不是区间边界
    users = _seed_rounds(db, ((20, "A", 'user'), (30, "暂停", 'pause'), (50, "B", 'user')))
    rows = round_delta_rows(_analysis(db, users))
    assert [row.label for row in rows] == ["A → B"]


def test_coincident_markers_keep_their_own_ids_and_round_names_after_rename(qapp, db, tmp_path):
    from core.report import round_delta_rows

    task = _seed_rounds(db, ((30, "同名", 'baseline'), (30, "暂停", 'pause'),
                             (30, "同名", 'user'), (30, "同名", 'user')))
    base, _pause, middle, last = db.markers.list(task.task_id)
    previous = _analysis(db, task).snapshot(MEM)
    assert db.markers.update_label(middle.id, "打开窗口")

    analysis = _analysis(db, task)
    snapshot = analysis.snapshot(MEM)
    assert [(i.start_marker_id, i.end_marker_id) for i in snapshot.intervals] == [
        (None, base.id), (base.id, middle.id), (middle.id, last.id), (last.id, None)]
    assert [(i.delta, i.sample_count) for i in snapshot.intervals] == [
        (i.delta, i.sample_count) for i in previous.intervals]
    assert [r.label for r in snapshot.rounds] == ["打开窗口", "同名"]
    assert [r.label for r in round_delta_rows(analysis)] == ["同名 → 打开窗口", "打开窗口 → 同名"]
    results, errors = _command_export(db, task, tmp_path, ['summary'])
    assert not errors
    with open(results[0]['paths'][0], encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(line for line in stream if not line.startswith(METADATA_PREFIX)))
    for metric in ('私有字节', '句柄数'):
        coincident = [row for row in rows if row['指标'] == metric
                      and row['起点'] == row['终点']]
        assert [row['区间'] for row in coincident] == ["同名 → 打开窗口", "打开窗口 → 同名"]


@pytest.mark.parametrize('source', ['pmctl', 'stage'])
def test_report_batch_freezes_marker_names_before_reading_other_metrics(
        qapp, db, tmp_path, monkeypatch, source):
    from core.automation.stage_snapshot import StageAnalysisWorker
    from core.ipc.commands import TaskAnalysisWorker

    task = _seed(db, markers=((30, "预热完成"),))
    original = db.markers.list
    reads = []

    def rename_after_read(task_id):
        markers = original(task_id)
        reads.append(task_id)
        if len(reads) == 1:
            db.markers.update_label(markers[0].id, "进入静置")
        return markers

    monkeypatch.setattr(db.markers, 'list', rename_after_read)
    if source == 'pmctl':
        worker = TaskAnalysisWorker(db, task.task_id, task.metric_types, analysis=True)
        analysis = worker.execute(CancelToken())
    else:
        worker = StageAnalysisWorker(db, task.task_id, str(tmp_path))
        _, analysis = worker.execute(CancelToken())
    assert reads == [task.task_id]
    assert original(task.task_id)[0].label == "进入静置"
    assert all(s.markers[0].label == "预热完成" for s in analysis.snapshots.values())
    assert {row[2] for row in summary_rows(analysis.snapshots)} == {
        "开始 → 预热完成", "预热完成 → 结束"}


@pytest.mark.parametrize('source', ['pmctl', 'stage'])
def test_batch_keeps_sample_cutoff_across_restart_and_member_queries(
        qapp, db, tmp_path, monkeypatch, source):
    from core.automation.stage_snapshot import StageAnalysisWorker
    from core.ipc.commands import TaskAnalysisWorker
    from utils.metrics import member_series_key

    member = member_series_key(HANDLES, 'worker.exe')
    task = _task()
    task.target_kind, task.status, task.end_time = 'tree', 'running', None
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=i), 100 + i, metric)
                         for i in range(30) for metric in (MEM, HANDLES, member)])
    db.markers.add(task.task_id, BASE + timedelta(seconds=10), "第一次重启", 'restart')
    db.markers.add(task.task_id, BASE + timedelta(seconds=29), "基线", 'baseline')
    cutoff = BASE + timedelta(seconds=29)
    aggregate = db.analytics.aggregates

    def restart_during_analysis(*args, **kwargs):
        result = aggregate(*args, **kwargs)
        monkeypatch.setattr(db.analytics, 'aggregates', aggregate)
        at = cutoff + timedelta(seconds=1)
        db.markers.add(task.task_id, at, "第二次重启", 'restart')
        db.save_data_points([DataPoint(task.task_id, at, 9999, m) for m in (MEM, HANDLES, member)])
        return result

    monkeypatch.setattr(db.analytics, 'aggregates', restart_during_analysis)
    if source == 'stage':
        _, analysis = StageAnalysisWorker(db, task.task_id, str(tmp_path)).execute(CancelToken())
    else:
        analysis = TaskAnalysisWorker(db, task.task_id, task.metric_types,
                                      analysis=True).execute(CancelToken())
        assert member in analysis.member_snapshots
        assert [part.number for part in analysis.segments] == [1, 2]
        assert analysis.segments[0].sample_count == 10
    for snap in (*analysis.snapshots.values(), *analysis.member_snapshots.values()):
        assert snap.task_last_at == snap.request.until == cutoff
        assert [m.label for m in snap.markers] == ["第一次重启", "基线"]
        assert snap.segment_count == 2 and snap.total_count == 30
        assert snap.baseline.value == 129 and snap.baseline.sample_count == 1
    fresh = run_history_query(db, build_request(task.task_id, HANDLES, 'all', analysis=True))
    assert fresh.segment_count == 3 and fresh.stats['count'] == 1


def _command_export(db, task, directory, kinds, writer_factory=None, *, evidence=False):
    from app.bootstrap import render_report_image_for_ipc
    from core.ipc.commands import CommandContext, CommandDispatcher
    from core.monitor_manager import MonitorManager

    manager = MonitorManager(db=db)
    dispatcher = CommandDispatcher(app_version='2.0.3')
    dispatcher.set_context(CommandContext(manager, db, None,
                                          report_renderer=render_report_image_for_ipc,
                                          export_worker_factory=writer_factory))
    results, errors = [], []
    run = dispatcher.start_export(task.task_id, str(directory), kinds, evidence=evidence,
                                  on_done=results.append,
                                  on_failed=lambda code, message: errors.append((code, message)))
    assert process_events_until(lambda: (results or errors) and not run._workers, timeout=10)
    assert not manager.has_running_exports()
    return results, errors


@pytest.mark.parametrize('failed_metric', [MEM, HANDLES])
def test_failed_metric_aborts_combined_export_and_a_retry_writes_markers(
        qapp, db, tmp_path, monkeypatch, failed_metric):
    task = _seed(db)
    output = tmp_path / 'reports'
    aggregate = db.analytics.aggregates

    def read(task_id, metric, **kwargs):
        if metric == failed_metric:
            raise StorageError("指标读取失败")
        return aggregate(task_id, metric, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(db.analytics, 'aggregates', read)
        results, errors = _command_export(db, task, output, ['csv', 'summary'])
    assert not results and len(errors) == 1 and "指标读取失败" in errors[0][1]
    assert list(output.iterdir()) == []
    results, errors = _command_export(db, task, output, ['csv', 'summary'])
    assert results and not errors
    with open(next(p for p in results[0]['paths'] if '摘要' not in os.path.basename(p)),
              encoding='utf-8-sig', newline='') as file:
        records = csv.DictReader(line for line in file if not line.startswith('# '))
        assert [r['打点'] for r in records if r['打点']] == ["基线", "第 1 轮", "第 2 轮"]


@pytest.mark.parametrize('initial_count', [0, 30])
def test_running_command_csv_and_summary_use_the_same_snapshot(
        qapp, db, tmp_path, initial_count):
    task = _seed(db, offsets=range(initial_count), markers=((3, "已有打点"),))
    task.status, task.end_time = 'running', None
    db.save_task(task)
    jobs = []

    def start_writer(database, job):
        jobs.append(job)
        db.add_marker(task.task_id, BASE + timedelta(seconds=30), "新打点")
        db.save_data_points(_points(task, [30, 31]))
        return ReportWorker(database, job)

    results, errors = _command_export(db, task, tmp_path, ['csv', 'summary'], start_writer)
    assert not errors and results[0]['rows'] == initial_count
    assert [m.label for m in jobs[0].markers] == ["已有打点"]
    assert jobs[0].data_until == (BASE + timedelta(seconds=29) if initial_count else None)
    with open(jobs[0].csv_path, encoding='utf-8-sig', newline='') as file:
        rows = list(csv.DictReader(line for line in file if not line.startswith('# ')))
    assert len(rows) == initial_count
    assert [r['打点'] for r in rows if r['打点']] == (["已有打点"] if initial_count else [])
    with open(jobs[0].summary_path, encoding='utf-8-sig', newline='') as file:
        summary = list(csv.reader(line for line in file if not line.startswith(METADATA_PREFIX)))
    assert summary[0] == list(SUMMARY_HEADER)
    assert bool(summary[1:]) == bool(initial_count)
    results, errors = _command_export(db, task, tmp_path, ['csv'])
    assert not errors and results[0]['rows'] == initial_count + 2
    with open(results[0]['paths'][0], encoding='utf-8-sig') as file:
        assert "新打点" in file.read()


def test_empty_task_evidence_keeps_csv_headers_and_insufficient_verdict(qapp, db, tmp_path):
    task = _seed(db, offsets=(), markers=())
    results, errors = _command_export(db, task, tmp_path, [], evidence=True)
    assert not errors and results[0]['rows'] == 0

    with ZipFile(results[0]['paths'][0]) as archive:
        assert any(name.endswith('.png') for name in archive.namelist())
        csv_names = [name for name in archive.namelist() if name.endswith('.csv')]
        assert len(csv_names) == 2
        for name in csv_names:
            lines = archive.read(name).decode('utf-8-sig').splitlines()
            assert f'{METADATA_PREFIX}采集次数: 0' in lines
            actual = list(csv.reader(line for line in lines if not line.startswith(METADATA_PREFIX)))
            expected = SUMMARY_HEADER if name.endswith('_摘要.csv') else build_report_csv_header(task)
            assert actual == [list(expected)]
        verdict = json.loads(archive.read('task.json'))['verdict']
        assert verdict['overall'] == 'insufficient'
        assert len(verdict['metrics']) == len(task.metric_types)
        assert all(metric['reason'] == 'no_data' and metric['samples'] == 0
                   for metric in verdict['metrics'])


def test_metadata_items_fields_and_optional_parts(db):
    from core.report import metadata_items
    from core.target_info import TargetInfo
    from data.models import QualityStats
    task = _seed_rounds(db, ())
    analysis = _analysis(db, task)
    stored = db.get_task(task.task_id)

    items = metadata_items(stored, analysis)
    assert items == [
        ("开始", "2026-07-16 16:00:00"),
        ("结束", "2026-07-16 16:10:00"),
        ("时长", "1分钟"),
        ("采集周期", "1 秒"),
        ("指标数", "2 项：私有字节、句柄数"),
        ("点数", "100"),
    ]

    stored.end_time = None
    stored.quality = QualityStats(12370, 12345, 0, 12, 5)
    stored.meta_json = TargetInfo(exe_path=r"C:\Demo\demo.exe", file_version="1.2.3.4",
                                  cmdline=("demo.exe", "--auto"), os_version="Windows 11",
                                  logical_cores=16, total_memory_bytes=32 * 1024 ** 3,
                                  tool_version="2.0.0", tool_is_admin=False).to_json()
    items = dict(metadata_items(stored, analysis))
    assert items["结束"] == "进行中"
    assert items["数据质量"].startswith("采集完成率 99.8%")
    assert items["被测程序"] == r"C:\Demo\demo.exe（文件版本 1.2.3.4）"
    assert items["环境"].startswith("Windows 11 · 16 核")
    # 命令行只进 CSV 元数据，不进报告图片
    assert "命令行" not in items


def test_duration_text_matches_ui_format_duration():
    from core.report import duration_text
    from ui.formatting import format_duration
    for seconds in (0, 45, 59.9, 60, 754, 3600, 8100, 86399, 86400, 97200, -5):
        assert duration_text(seconds) == format_duration(seconds)
