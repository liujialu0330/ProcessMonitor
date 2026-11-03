"""
ExportWorker 用例（offscreen）
覆盖：大数据量（>=5万行）流式导出与一次性 list(pivot_rows(...)) 输出逐字节一致；
取消后写了一半的CSV文件被删除、线程正常退出（不挂起）。
"""
import csv
import os
import uuid
from datetime import datetime, timedelta

from PyQt5.QtCore import Qt
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
    # 导出文件末尾带「打点」列（该任务无打点，整列为空）
    header = build_csv_header(task, include_markers=True)
    expected_rows = list(pivot_rows(task, all_points, markers=[]))

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


# ========== 临时文件 + os.replace、信号时序 ==========

def _run_sync(worker):
    """同线程直接执行 run()（QThread 子类的 run 是普通方法），便于断言信号与文件状态"""
    results = {'finished': [], 'errors': []}
    worker.export_finished.connect(
        lambda p, rc, pc: results['finished'].append((p, rc, pc)), Qt.DirectConnection)
    worker.error_occurred.connect(results['errors'].append, Qt.DirectConnection)
    worker.run()
    return results


def _seeded_task(db, n=20):
    task = _make_task()
    db.save_task(task)
    _seed_points(db, task, n, datetime(2026, 1, 1))
    return task


def test_export_writes_tmp_then_replaces_target(qapp, db, db_path, tmp_path):
    task = _seeded_task(db)
    save_path = str(tmp_path / "out.csv")
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write("旧内容")
    worker = ExportWorker(db_path, task, save_path)

    results = _run_sync(worker)

    assert results['errors'] == []
    assert results['finished'] == [(save_path, 20, 20)]
    assert not os.path.exists(save_path + '.tmp')
    with open(save_path, encoding='utf-8-sig') as f:
        assert "旧内容" not in f.read()


def test_export_failure_keeps_existing_target_and_removes_tmp(qapp, db, db_path, tmp_path, monkeypatch):
    task = _seeded_task(db)
    save_path = str(tmp_path / "keep.csv")
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write("原有重要文件")

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟导出中途失败")

    monkeypatch.setattr('core.export_worker.pivot_rows', _boom)
    worker = ExportWorker(db_path, task, save_path)

    results = _run_sync(worker)

    assert results['finished'] == [] and len(results['errors']) == 1
    assert "导出失败" in results['errors'][0]
    assert not os.path.exists(save_path + '.tmp')
    with open(save_path, encoding='utf-8') as f:
        assert f.read() == "原有重要文件"


def test_export_cancel_keeps_existing_target(qapp, db, db_path, tmp_path):
    task = _seeded_task(db)
    save_path = str(tmp_path / "keep_cancel.csv")
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write("原有重要文件")
    worker = ExportWorker(db_path, task, save_path)
    worker.cancel()

    results = _run_sync(worker)

    assert results == {'finished': [], 'errors': []}   # 取消不发任何信号
    assert not os.path.exists(save_path + '.tmp')
    with open(save_path, encoding='utf-8') as f:
        assert f.read() == "原有重要文件"


def test_export_replace_failure_keeps_target_and_reports_error(qapp, db, db_path, tmp_path, monkeypatch):
    """目标被占用等导致 os.replace 失败：目标不变、临时文件被清理、报错"""
    task = _seeded_task(db)
    save_path = str(tmp_path / "locked.csv")
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write("被占用的旧文件")

    def _deny(src, dst):
        raise PermissionError("目标被占用")

    monkeypatch.setattr('core.export_worker.os.replace', _deny)
    worker = ExportWorker(db_path, task, save_path)

    results = _run_sync(worker)

    assert len(results['errors']) == 1 and results['finished'] == []
    assert not os.path.exists(save_path + '.tmp')
    with open(save_path, encoding='utf-8') as f:
        assert f.read() == "被占用的旧文件"


def test_export_signal_is_last_action(qapp, db, db_path, tmp_path):
    """信号发出时收尾已全部完成：目标文件已就位、临时文件已不存在"""
    task = _seeded_task(db)
    save_path = str(tmp_path / "order.csv")
    worker = ExportWorker(db_path, task, save_path)
    observed = {}

    def _on_finished(path, rows, points):
        observed['target_exists'] = os.path.exists(save_path)
        observed['tmp_exists'] = os.path.exists(save_path + '.tmp')

    worker.export_finished.connect(_on_finished, Qt.DirectConnection)
    worker.run()

    assert observed == {'target_exists': True, 'tmp_exists': False}


def test_export_error_signal_after_cleanup(qapp, db, db_path, tmp_path, monkeypatch):
    task = _seeded_task(db)
    save_path = str(tmp_path / "err_order.csv")
    monkeypatch.setattr('core.export_worker.pivot_rows',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    worker = ExportWorker(db_path, task, save_path)
    observed = {}
    worker.error_occurred.connect(
        lambda msg: observed.update(tmp_exists=os.path.exists(save_path + '.tmp')),
        Qt.DirectConnection)

    worker.run()

    assert observed == {'tmp_exists': False}


def test_export_metric_filter_and_finished_signal_triggers_after_thread_end(qapp, db, db_path, tmp_path):
    """指定 metric_type 时只导出该指标；QThread.finished 在 run() 返回之后才触发"""
    task = _make_task(metric_types=["memory_rss", "cpu_percent"])
    db.save_task(task)
    base = datetime(2026, 1, 1)
    db.save_data_points(
        [DataPoint(task.task_id, base + timedelta(seconds=i), float(i), "memory_rss") for i in range(5)]
        + [DataPoint(task.task_id, base + timedelta(seconds=i), 100.0 + i, "cpu_percent") for i in range(5)])
    save_path = str(tmp_path / "filtered.csv")
    worker = ExportWorker(db_path, task, save_path, metric_type="cpu_percent")
    finished_calls = []
    worker.export_finished.connect(lambda p, rc, pc: finished_calls.append((rc, pc)),
                                   Qt.DirectConnection)
    thread_done = []
    worker.finished.connect(lambda: thread_done.append(1))

    worker.start()
    assert worker.wait(30000)
    QApplication.processEvents()

    assert finished_calls == [(5, 5)]   # 只处理了 cpu_percent 的 5 个点
    assert thread_done == [1]


def test_export_worker_writes_markers_into_last_column(qapp, db, db_path, tmp_path):
    """导出文件末列为「打点」：标签写在打点时间所在或其后的第一行"""
    task = _make_task(metric_types=["memory_rss"])
    db.save_task(task)
    base = datetime(2026, 1, 1, 0, 0, 0)
    _seed_points(db, task, 5, base)
    db.add_marker(task.task_id, base + timedelta(seconds=1, milliseconds=500), "第1轮")
    db.add_marker(task.task_id, base + timedelta(seconds=3), "第2轮")

    save_path = str(tmp_path / "with_markers.csv")
    worker = ExportWorker(db_path, task, save_path)
    results = _run_sync(worker)
    assert results['errors'] == []

    with open(save_path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))
    assert rows[0][-1] == '打点'
    assert [row[-1] for row in rows[1:]] == ["", "", "第1轮", "第2轮", ""]
