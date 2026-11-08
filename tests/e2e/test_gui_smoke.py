"""
GUI 端到端冒烟测试（tests/e2e/，零生产代码改动）

主线链路：选进程开始监控 -> 采集数据 -> 托盘菜单打"基线"（不弹窗）-> 全局快捷键入口
再打两轮（不弹窗）-> 停止任务（落库）-> 卡片"查看分析"跳到分析页（判定摘要表、打点
区间表、采集明细）-> 导出报告三件套（报告图片 + 明细 CSV + 摘要 CSV）-> 设置页（三个
分组、卡片存在 + 主题切换实际生效）-> 再开一个任务后关闭窗口（退出协调器停掉运行中
任务并全部收尾）。侧栏只有 监控 / 分析 / 设置。

双跑法：
    - 默认 offscreen 断言态（CI 可跑）：`pytest tests/e2e/test_gui_smoke.py -q`
    - 真窗口观察态（发布前人工冒烟）：
      `$env:QT_QPA_PLATFORM='windows'; $env:GUI_SMOKE_VISIBLE='1'; \
       python -m pytest tests/e2e/test_gui_smoke.py -s`
      断言与 offscreen 模式完全一致，仅在关键步骤间追加停顿方便人工观察。

隔离核实：db 注入 tmp 库（不碰项目真实 data\\monitor.db）、不写 logs（MainWindow
不调用 setup_logging）。服务用生产实现（真实进程查询、时钟、提示条），只把
对话框换成 ScriptedDialogs（不真弹资源管理器、关窗确认直接给出"退出"），并把
about_group.check_update 覆写为 no-op 避免真联网。不注册真实全局快捷键（不注入
MarkerHotkey），快捷键入口直接调用主窗口的 quick_marker（与快捷键 triggered 同一槽）。
"""
import csv
import dataclasses
import os
import time

import pytest
from PyQt5.QtTest import QTest
from qfluentwidgets import Theme, qconfig, isDarkTheme

from app_config import cfg
from core.monitor_manager import MonitorManager
from tests.doubles import ScriptedDialogs
from ui.main_window import MainWindow
from core.shutdown import ThreadRegistry
from ui.services import production_services
from ui.tray import ALL_TASKS
from ui.widgets.analysis.toolbar import EXPORT_REPORT
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


def _start_monitoring(monitor_page, metrics):
    """监控页：搜索框直接输入 PID（绕过指标对话框）后点"开始监控"，返回新卡片。"""
    before = set(monitor_page.board.cards())
    form = monitor_page.form
    form.picker.set_text(str(os.getpid()))
    form.set_metrics_summary(metrics)
    form.interval_spinbox.setValue(1)
    form.start_button.click()
    added = set(monitor_page.board.cards()) - before
    assert len(added) == 1, "点开始后应新增且仅新增一张任务卡片"
    task_id = added.pop()
    return task_id, monitor_page.board.cards()[task_id]


def _wait_more_samples(card, more=2):
    target = card.count + more
    assert _poll_until(lambda: card.count >= target), \
        f"15s 内采集次数未增加到 {target}，实际 count={card.count}"


@pytest.mark.slow   # 全链路冒烟约 10 秒；各环节的快速覆盖见架构说明 §8 的 slow 清单
def test_gui_smoke_full_lifecycle(qapp, e2e_db, tmp_path):
    """主线冒烟：开始 -> 基线 -> 两轮打点 -> 停止 -> 分析页判定与区间 -> 导出报告 -> 关窗收尾"""
    # 与 main.py 相同的组装方式，只替换对话框：打开文件夹只做记录；关闭确认是模态
    # 对话框，用例中途失败时若仍有任务在跑，收尾的 close() 不能被它阻塞
    manager = MonitorManager(db=e2e_db)
    dialogs = ScriptedDialogs(ask_close_action='quit')
    services = dataclasses.replace(
        production_services(manager, ThreadRegistry()), dialogs=dialogs)

    window = MainWindow(e2e_db, manager, services)
    # 覆写为 no-op：防止 3 秒后 QTimer 触发真实联网检查更新；MainWindow 内部 QTimer
    # lambda 以 silent=True 调用，no-op 必须接受 **kwargs
    window.about_group.check_update = lambda *args, **kwargs: None
    # 记录退出协调器的结果（真退出路径只经 _run_shutdown）
    shutdown_results = []
    real_shutdown = window._run_shutdown
    window._run_shutdown = lambda: shutdown_results.append(real_shutdown()) or shutdown_results[-1]

    window.show()
    _pause()
    metrics = [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES]

    try:
        # ========== 1. 监控页：选进程开始监控 ==========
        monitor_page = window.monitor_page
        task_id, card = _start_monitoring(monitor_page, metrics)
        assert _poll_until(lambda: card.count >= 2), \
            f"15s 内未采集到 2 次数据，实际 count={card.count}"
        assert card.count_label.text().endswith(f"每 1 秒 · {card.count} 次采集")
        _pause()

        # ========== 2. 托盘菜单打"基线"，快捷键入口再打两轮（都不弹窗） ==========
        window.tray.marker_requested.emit(ALL_TASKS)
        _wait_more_samples(card)
        window.quick_marker()
        _wait_more_samples(card)
        window.quick_marker()
        _wait_more_samples(card)
        markers = e2e_db.markers.list(task_id)
        assert [m.kind for m in markers] == ['baseline', 'user', 'user'], \
            f"应依次记下基线与两轮打点，实际 {[(m.label, m.kind) for m in markers]}"
        assert markers[0].label == "基线"
        assert dialogs.calls_of('ask_marker_label') == [], "托盘/快捷键打点不应弹窗"
        _pause()

        # ========== 3. 停止任务（内含 wait 到落库完成） ==========
        window.monitor_manager.stop_task(task_id)
        QTest.qWait(200)
        assert e2e_db.get_sample_count(task_id) >= 8, "停止后落库的采集次数应 >= 8"
        # task_stopped 携带 StopReason 枚举跨线程排队投递：卡片转已结束并显示中文原因
        assert _poll_until(lambda: card.is_finished), "停止后卡片应转为已结束"
        assert "用户停止" in card.finished_detail_label.text()
        _pause()

        # ========== 4. 分析页：已结束卡片"查看分析"跳转并选中该任务 ==========
        card.history_button.click()
        analysis_page = window.analysis_page
        assert window.stackedWidget.currentWidget() is analysis_page
        loaded = _poll_until(lambda: analysis_page.current_task_id == task_id
                             and analysis_page.vm.last_analysis is not None
                             and len(analysis_page.interval_table.row_texts()) >= 2)
        assert loaded, "分析页应选中刚结束的任务并给出判定摘要与打点区间"
        summary = analysis_page.summary_table.row_texts()
        assert [row[0] for row in summary] == ["私有字节", "句柄数"], f"摘要表行：{summary}"
        intervals = [row[0] for row in analysis_page.interval_table.row_texts()]
        assert len(intervals) >= 2 and intervals[1].startswith("基线 →"), f"区间：{intervals}"
        assert analysis_page.summary_table.basis().startswith("依据：")

        # 明细首行为最新时间戳（"倒序显示，最新的在前"契约）
        data_table = analysis_page.sample_table.data_table
        assert data_table.rowCount() >= 2
        points = e2e_db.get_task_data_points(task_id, metric_type=analysis_page.current_metric)
        latest_time_str = points[-1].timestamp.strftime('%m-%d %H:%M:%S')
        assert data_table.item(0, 0).text() == latest_time_str
        _pause()

        # ========== 5. 导出报告三件套（保存对话框由 ScriptedDialogs 给出路径） ==========
        out = tmp_path / "report"
        out.mkdir()
        dialogs.push('ask_save_path', str(out / "smoke_报告.png"))
        assert analysis_page.export_flow.run(EXPORT_REPORT), "导出应已启动"
        assert _poll_until(lambda: analysis_page.vm.export_worker is None
                           and len(os.listdir(out)) == 3), "15s 内导出未完成"
        names = sorted(os.listdir(out))
        assert names == ["smoke.csv", "smoke_报告.png", "smoke_摘要.csv"], names
        assert all(os.path.getsize(out / name) > 0 for name in names)
        with open(out / "smoke.csv", newline='', encoding='utf-8-sig') as f:
            rows = [row for row in csv.reader(f) if row and not row[0].startswith('#')]
        assert len(rows) >= 3, f"明细 CSV 应至少含表头+2 行数据，实际 {len(rows)} 行"

        assert not dialogs.calls_of('open_folder'), "导出完成后不应自动打开文件夹抢占焦点"
        analysis_page.export_flow.open_folder_button.click()
        assert dialogs.calls_of('open_folder') == [{"path": str(out)}], \
            "用户点击后才应打开导出目录"
        _pause()

        # ========== 6. 设置页：分组卡片存在 + 主题切换实际生效 ==========
        window.switchTo(window.setting_page)
        QTest.qWait(100)

        setting_page = window.setting_page
        assert setting_page.general_group is not None, "“常规”分组应存在"
        assert setting_page.data_group is not None, "“数据”分组应存在"
        assert setting_page.about_group is window.about_group, "“关于与更新”分组应存在"
        assert setting_page.cleanup_card is not None, "“压缩数据库”卡片应存在"
        assert setting_page.open_dir_card is not None, "“数据目录”卡片应存在"
        assert setting_page.close_to_tray_card is not None, "托盘开关卡片应存在"
        assert setting_page.hotkey_card is not None, "打点快捷键卡片应存在"

        # 只断言"不抛异常"抓不住 themeMode 状态分裂类缺陷（如 AppConfig 误重定义
        # themeMode 导致 setTheme 与设置页各改一个对象、互不联动）：必须实际断言
        # isDarkTheme()/qconfig.theme 确实变为深色，再切回浅色验证双向均生效。
        # qconfig.set 默认会落盘：先 qconfig.load 到 tmp_path 下的一次性文件，避免在
        # 项目根目录写出游离的 config\ 目录（不影响已连接的信号与已构造的设置页）。
        qconfig.load(str(tmp_path / "e2e_theme_probe_config.json"), cfg)

        qconfig.set(cfg.themeMode, Theme.DARK)
        QTest.qWait(100)
        assert isDarkTheme() is True, "切换到深色主题后 isDarkTheme() 应实际为 True"
        assert qconfig.theme == Theme.DARK

        qconfig.set(cfg.themeMode, Theme.LIGHT)
        QTest.qWait(100)
        assert isDarkTheme() is False, "切回浅色主题后 isDarkTheme() 应实际为 False"
        assert qconfig.theme == Theme.LIGHT
        _pause()

        # ========== 7. 再开一个任务，带着运行中任务关窗：退出协调器停任务并全部收尾 ==========
        window.switchTo(monitor_page)
        second_id, second_card = _start_monitoring(monitor_page, metrics)
        assert _poll_until(lambda: second_card.count >= 1)
        assert manager.has_running_tasks()
    finally:
        window.close()
        QTest.qWait(200)

    assert dialogs.calls_of('ask_close_action'), "仍有任务在监控时关窗应先询问"
    assert shutdown_results == [True], "退出协调器应在期限内完成全部收尾"
    assert not manager.has_running_tasks()
    assert services.threads.entries() == [], "登记的后台线程应全部收尾"
    assert e2e_db.get_task(second_id).status == 'stopped', "运行中任务应在退出时落库为已结束"
