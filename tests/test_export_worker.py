"""
ExportWorker 用例（offscreen）
覆盖：大数据量（>=5万行）流式导出与一次性 list(pivot_rows(...)) 输出逐字节一致；
取消后写了一半的CSV文件被删除、线程正常退出（不挂起）。
"""
import csv
import os
import uuid
from datetime import datetime, timedelta

from PyQt5.QtWidgets import QApplication

from core.export import build_csv_header, pivot_rows
from core.export_worker import ExportWorker
from data.models import MonitorTask, DataPoint


def _make_task(**overrides) -> MonitorTask:
    defaults = dict(
        task_id=str(uuid.uuid4()),
        pid=4321,
        process_name="export_worker_test.exe",
        metric_types=["memory_rss"],
        interval=1.0,
        start_time=datetime.now(),
        end_time=None,
        status="stopped",
    )
    defaults.update(overrides)
    return MonitorTask(**defaults)


def _seed_points(db, task, n: int, base: datetime):
    """批量写入 n 条数据点（分批调用 save_data_points，避免单批参数过多）"""
    batch = []
    for i in range(n):
        batch.append(DataPoint(task_id=task.task_id, timestamp=base + timedelta(seconds=i),
                                value=float(i) * 1.1, metric_type="memory_rss"))
        if len(batch) >= 5000:
            db.save_data_points(batch)
            batch = []
    if batch:
        db.save_data_points(batch)


def test_export_worker_large_dataset_matches_one_shot_pivot_rows(qapp, db, db_path, tmp_path):
    """大数据量（5万行）流式导出内容与一次性 list(pivot_rows(...)) 输出逐字节一致"""
    task = _make_task()
    db.save_task(task)

    n = 50000
    _seed_points(db, task, n, datetime(2026, 1, 1))

    # 基准：一次性拉全量数据再喂给 pivot_rows
    all_points = db.get_task_data_points(task.task_id)
    assert len(all_points) == n
    header = build_csv_header(task)
    expected_rows = list(pivot_rows(task, all_points))

    expected_path = str(tmp_path / "expected_one_shot.csv")
    with open(expected_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in expected_rows:
            writer.writerow(row)

    # 实际：ExportWorker 后台线程流式导出
    save_path = str(tmp_path / "export_worker_out.csv")
    worker = ExportWorker(db_path, task, save_path)

    finished = {}
    worker.export_finished.connect(
        lambda p, rc, pc: finished.update(path=p, row_count=rc, point_count=pc))
    errors = []
    worker.error_occurred.connect(lambda msg: errors.append(msg))

    worker.start()
    ok = worker.wait(60000)
    QApplication.processEvents()  # 让跨线程排队的信号有机会被投递

    assert ok, "ExportWorker 未在超时前完成"
    assert not errors, f"导出报错: {errors}"
    assert finished.get('row_count') == len(expected_rows)
    assert finished.get('point_count') == n

    with open(save_path, 'rb') as f1, open(expected_path, 'rb') as f2:
        assert f1.read() == f2.read(), "流式导出内容与一次性导出内容逐字节不一致"


def test_export_worker_cancel_deletes_file_and_thread_exits(qapp, db, db_path, tmp_path):
    """取消导出：写了一半的CSV文件被删除，线程在超时前正常退出（不挂起）"""
    task = _make_task()
    db.save_task(task)

    n = 30000
    _seed_points(db, task, n, datetime(2026, 1, 1))

    save_path = str(tmp_path / "export_worker_cancel.csv")
    worker = ExportWorker(db_path, task, save_path)

    worker.start()
    worker.cancel()  # 大概率在导出仍在进行中时请求取消
    ok = worker.wait(30000)
    QApplication.processEvents()

    assert ok, "取消后线程未能在超时前退出"
    assert not os.path.exists(save_path), "取消后应删除写了一半的CSV文件"
