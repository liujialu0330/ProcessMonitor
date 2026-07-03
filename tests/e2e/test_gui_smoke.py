"""
GUI 端到端冒烟测试（tests/e2e/，零生产代码改动）

链路：创建监控任务 -> 采集数据 -> 停止任务（落库）-> 历史页查看（图表+表格）->
导出页导出 CSV -> 关闭窗口（完整线程收尾）。

双跑法：
    - 默认 offscreen 断言态（CI 可跑）：`pytest tests/e2e/test_gui_smoke.py -q`
    - 真窗口观察态（发布前人工冒烟）：
      `$env:QT_QPA_PLATFORM='windows'; $env:GUI_SMOKE_VISIBLE='1'; \
       python -m pytest tests/e2e/test_gui_smoke.py -s`
      断言与 offscreen 模式完全一致，仅在关键步骤间追加停顿方便人工观察。

隔离核实：db 注入 tmp 库（不碰项目真实 data\\monitor.db）、不写 logs（MainWindow
不调用 setup_logging）、monkeypatch os.startfile 与 about_page.check_update
避免真联网/真弹资源管理器。
"""
import csv
import os
import time

from PyQt5.QtTest import QTest

from ui.main_window import MainWindow
from utils.metrics import MetricType

# 真窗口观察态开关：GUI_SMOKE_VISIBLE=1 时追加停顿，断言逻辑不变
VISIBLE = os.environ.get('GUI_SMOKE_VISIBLE') == '1'

# 轮询上限（秒）
POLL_TIMEOUT = 15


def _pause(ms=800):
    """真窗口模式下追加停顿，便于人工观察；offscreen 断言态跳过，不影响断言。"""
    if VISIBLE:
        QTest.qWait(ms)


def _poll_until(condition, timeout=POLL_TIMEOUT, interval_ms=100):
    """轮询等待 condition() 为真，最长等待 timeout 秒；每轮 QTest.qWait 驱动事件循环，
    让跨线程排队的信号（如 data_updated/task_stopped）有机会被投递处理。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QTest.qWait(interval_ms)
    return condition()


def test_gui_smoke_full_lifecycle(qapp, e2e_db, tmp_path, monkeypatch):
    """完整链路冒烟：创建任务 -> 采集 -> 停止落库 -> 历史页查看 -> 导出 CSV -> 关闭窗口"""
    # 拦截真实副作用：导出/更新下载完成后不真弹资源管理器
    startfile_calls = []
    monkeypatch.setattr(os, 'startfile', lambda path: startfile_calls.append(path),
                         raising=False)

    window = MainWindow(db=e2e_db)
    # 覆写为 no-op：防止 3 秒后 QTimer 触发真实联网检查更新；MainWindow 内部 QTimer
    # lambda 以 silent=True 调用，no-op 必须接受 **kwargs
    window.about_page.check_update = lambda *args, **kwargs: None

    window.show()
    _pause()

    try:
        # ========== 1. 监控页：创建并启动监控任务（绕过进程下拉框与指标对话框） ==========
        monitor_page = window.monitor_page
        monitor_page.pid_input.setText(str(os.getpid()))
        monitor_page.selected_metrics = [MetricType.MEMORY_RSS, MetricType.CPU_PERCENT]
        monitor_page._update_metric_summary()
        monitor_page.interval_spinbox.setValue(1)
        monitor_page.start_button.click()

        assert len(monitor_page.task_cards) == 1, "启动后应有且仅有一张任务卡片"
        task_id = next(iter(monitor_page.task_cards))
        card = monitor_page.task_cards[task_id]

        _pause()

        # ========== 2. 轮询等待至少 2 次采集 ==========
        got_two_samples = _poll_until(lambda: card.count >= 2)
        assert got_two_samples, f"15s 内未采集到 2 次数据，实际 count={card.count}"
        assert card.count_label.text() == f"已记录: {card.count} 次采集"

        _pause()

        # ========== 3. 停止任务（内含 wait 到落库完成） ==========
        window.monitor_manager.stop_task(task_id)
        # 让跨线程排队的 task_stopped 信号有机会被主线程事件循环投递处理
        QTest.qWait(200)

        assert e2e_db.get_sample_count(task_id) >= 2, "停止后落库的采集次数应 >= 2"

        _pause()

        # ========== 4. 历史页：查看图表与表格 ==========
        window.switchTo(window.history_page)
        QTest.qWait(100)

        history_page = window.history_page
        found = False
        for i in range(history_page.task_combo.count()):
            if history_page.task_combo.itemData(i) == task_id:
                history_page.task_combo.setCurrentIndex(i)
                found = True
                break
        assert found, "历史页任务下拉框中应能找到刚创建的任务"
        QTest.qWait(100)

        assert history_page.data_table.rowCount() >= 2, "历史页表格行数应 >= 2"
        assert history_page.chart_widget.listDataItems(), "历史页图表应有绘制数据"

        # 表格首行为最新时间戳（既有"倒序显示，最新的在前"契约）
        points = e2e_db.get_task_data_points(task_id, metric_type=history_page.current_metric)
        assert points, "查询到的数据点不应为空"
        latest_time_str = points[-1].timestamp.strftime('%H:%M:%S')
        first_row_time = history_page.data_table.item(0, 0).text()
        assert first_row_time == latest_time_str, (
            f"表格首行时间 {first_row_time} 应等于最新采集时间戳 {latest_time_str}")

        _pause()

        # ========== 5. 导出页：导出 CSV（绕过文件保存对话框） ==========
        window.switchTo(window.export_page)
        QTest.qWait(100)

        export_page = window.export_page
        found = False
        for i in range(export_page.task_combo.count()):
            if export_page.task_combo.itemData(i) == task_id:
                export_page.task_combo.setCurrentIndex(i)
                found = True
                break
        assert found, "导出页任务下拉框中应能找到刚创建的任务"
        QTest.qWait(100)

        save_path = str(tmp_path / "export_smoke.csv")
        export_page.path_edit.setText(save_path)
        export_page.export_button.click()

        def _export_done():
            worker = export_page._export_worker
            still_running = worker is not None and worker.isRunning()
            return os.path.exists(save_path) and not still_running

        exported = _poll_until(_export_done)
        assert exported, "15s 内导出未完成"

        with open(save_path, 'r', newline='', encoding='utf-8-sig') as f:
            rows = list(csv.reader(f))
        assert len(rows) >= 3, f"导出 CSV 应至少含表头+2 行数据，实际 {len(rows)} 行"

        assert startfile_calls, "导出完成后应触发（被拦截的）打开文件夹调用"

        _pause()
    finally:
        # ========== 6. 关闭窗口：走完整 closeEvent 线程收尾，不挂不崩 ==========
        window.close()
        QTest.qWait(200)
