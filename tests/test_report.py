"""
报告导出：文件命名、CSV 元数据与空洞空行、摘要 CSV 内容、ReportWorker 写文件（临时文件
+ 原子替换、取消与失败不留残留且不动已有目标、信号收尾、只用注入的 db）、报告图片的
文字与离屏渲染。
"""
import csv
import os
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtGui import QColor, QImage

from core.report_worker import ReportJob, ReportWorker
from core.history_query import TaskAnalysis, build_request, run_history_query
from core.report import (
    ELAPSED_COLUMN_NAME, METADATA_PREFIX, SUMMARY_HEADER, ReportPaths,
    build_report_csv_header, downsample_footnote, format_elapsed, gap_threshold_seconds,
    interval_spans, metadata_lines, paths_from_image_choice, report_rows, summary_rows,
    task_basename)
from core.workers.base import CancelToken
from data.models import DataPoint, Marker, MonitorTask
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
    expected = split_intervals(points, [Boundary(m.timestamp, m.label, 'user') for m in markers])
    got = snapshot.intervals
    assert [(i.start, i.end, i.sample_count, i.delta, i.peak) for i in got] == \
        [(i.start, i.end, i.sample_count, i.delta, i.peak) for i in expected]
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

    def _failing(task_id, batch_size=5000):
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
