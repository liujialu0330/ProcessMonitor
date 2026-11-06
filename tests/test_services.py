"""
界面层服务的生产实现：InfoBar 提示时长、真实对话框的按钮文案与返回约定、
系统交互的委托方式、psutil 进程查询、配置偏好读写、任务状态委托与服务组装。

真实对话框不进 exec()：给 QtDialogs 注入离屏 show() 后直接点按钮的运行器，
不挂定时器、不阻塞，用例之间不留残余事件。
"""
import dataclasses
import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QEvent, QSize, Qt
from PyQt5.QtGui import QColor, QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QWidget
from qfluentwidgets import InfoBar, InfoBarPosition

import ui.services.dialogs as dialogs_module
from app_config import cfg
from core.contracts import TaskInfo, TaskStatus
from ui.services import (
    Clock, ConfigPreferences, Dialogs, InfoBarNotifier, ManagerTaskStatus,
    MonitorPreferences, Notifier, ProcessQuery, PsutilProcessQuery, QtDialogs,
    SystemClock, TaskStatusProvider, UiServices, production_services, resolve_duration,
)
from utils.metrics import LEAK_CHECK_PRESET, MetricType

# 一个几乎不可能存在的 PID（Windows 的 PID 是 4 的倍数，奇数 PID 不会出现）
MISSING_PID = 999_999_999


@pytest.fixture
def host(qapp):
    # 遮罩式对话框与 InfoBar 都挂在宿主窗口内，宿主不可见时它们也不会显示
    widget = QWidget()
    widget.resize(900, 700)
    widget.show()
    QApplication.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def restore_last_metrics():
    # conftest 只复位 cfg 的另外三项，last_metrics 由本文件自行复位
    cfg.last_metrics.value = list(LEAK_CHECK_PRESET)
    yield
    cfg.last_metrics.value = list(LEAK_CHECK_PRESET)


class _ClickRunner:
    """测试用的对话框运行器（注入 QtDialogs 替代 exec()）：离屏 show()，等淡入结束后
    交给 action 检查/操作（通常是点某个按钮），再等淡出完成，按对话框的 result()
    返回是否被确认。全程不进模态循环、不挂任何定时器。"""

    def __init__(self, action):
        self.action = action
        self.seen = {}
        self.ran = 0

    def __call__(self, dialog) -> bool:
        self.ran += 1
        dialog.show()
        QTest.qWait(300)  # 淡入动画 200ms
        self.action(dialog, self.seen)
        for _ in range(50):  # 淡出动画 100ms 后才真正 done()
            if not dialog.isVisible():
                break
            QTest.qWait(20)
        assert not dialog.isVisible(), "对话框没有关闭"
        return dialog.result() == QDialog.Accepted


def _click(button_name):
    return lambda dialog, seen: getattr(dialog, button_name).click()


# ---------- 协议一致性与组装 ----------

def test_production_implementations_satisfy_protocols():
    assert isinstance(InfoBarNotifier(), Notifier)
    assert isinstance(QtDialogs(), Dialogs)
    assert isinstance(SystemClock(), Clock)
    assert isinstance(PsutilProcessQuery(), ProcessQuery)
    assert isinstance(ManagerTaskStatus(object()), TaskStatusProvider)
    assert isinstance(ConfigPreferences(), MonitorPreferences)


def test_production_services_binds_task_status_to_manager():
    fake_manager = SimpleNamespace(get_task_info=lambda task_id: (
        _info(task_id, TaskStatus.RUNNING) if task_id == "t1" else None))
    from core.shutdown import ThreadRegistry
    registry = ThreadRegistry()
    services = production_services(fake_manager, registry)

    assert isinstance(services, UiServices)
    assert services.threads is registry
    assert isinstance(services.notifier, InfoBarNotifier)
    assert isinstance(services.dialogs, QtDialogs)
    assert isinstance(services.clock, SystemClock)
    assert isinstance(services.process_query, PsutilProcessQuery)
    assert isinstance(services.preferences, ConfigPreferences)
    assert services.task_status.is_running("t1") is True
    assert services.task_status.is_running("t2") is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        services.clock = SystemClock()


# ---------- Notifier ----------

def test_resolve_duration_defaults_and_explicit_values():
    assert resolve_duration("info", None) == 2000
    assert resolve_duration("success", None) == 2000
    assert resolve_duration("warning", None) == 3000
    assert resolve_duration("error", None) == -1
    assert resolve_duration("warning", 10000) == 10000
    with pytest.raises(ValueError):
        resolve_duration("fatal", None)


def test_infobar_notifier_shows_bars_with_expected_duration(host):
    notifier = InfoBarNotifier()
    bars = {
        "info": notifier.info(host, "提示", "一般信息"),
        "success": notifier.success(host, "成功", "已保存"),
        "warning": notifier.warning(host, "注意", "请先选择指标"),
        "error": notifier.error(host, "错误", "采集失败"),
    }
    QApplication.processEvents()

    assert {level: bar.duration for level, bar in bars.items()} == {
        "info": 2000, "success": 2000, "warning": 3000, "error": -1}
    for level, bar in bars.items():
        assert isinstance(bar, InfoBar)
        assert bar.position == InfoBarPosition.TOP
        assert bar.isClosable
        assert bar.parent() is host
    assert bars["warning"].title == "注意"
    assert bars["warning"].content == "请先选择指标"

    for bar in bars.values():
        bar.close()


def test_infobar_notifier_honors_explicit_duration_and_emits_closed(host):
    notifier = InfoBarNotifier()
    long_warning = notifier.warning(host, "异常结束", "进程已退出", duration=15000)
    timed_error = notifier.error(host, "错误", "可自定义时长", duration=5000)
    assert long_warning.duration == 15000
    assert timed_error.duration == 5000

    closed = []
    long_warning.closedSignal.connect(lambda: closed.append(True))
    long_warning.close()
    timed_error.close()
    assert closed == [True]


# ---------- Dialogs：真实对话框 ----------

def test_default_runner_is_exec(monkeypatch):
    calls = []

    class _Dialog:
        def exec(self):
            calls.append("exec")
            return 1

    assert dialogs_module.exec_dialog(_Dialog()) is True
    assert calls == ["exec"]


def test_confirm_uses_given_texts_focuses_cancel_and_returns_true(host):
    def _accept(box, seen):
        seen.update(yes=box.yesButton.text(), cancel=box.cancelButton.text(),
                    focus_on_cancel=box.focusWidget() is box.cancelButton)
        box.yesButton.click()

    runner = _ClickRunner(_accept)
    result = QtDialogs(run_dialog=runner).confirm(host, "删除任务", "此操作不可撤销。", "删除")

    assert result is True
    assert runner.ran == 1
    assert runner.seen == {"yes": "删除", "cancel": "取消", "focus_on_cancel": True}


def test_confirm_returns_false_on_cancel_with_custom_cancel_text(host):
    def _cancel(box, seen):
        seen['cancel'] = box.cancelButton.text()
        box.cancelButton.click()

    runner = _ClickRunner(_cancel)
    result = QtDialogs(run_dialog=runner).confirm(
        host, "停止监控", "确定停止吗？", "停止", cancel_text="保留")

    assert result is False
    assert runner.seen['cancel'] == "保留"


def test_ask_marker_label_returns_stripped_label_and_shows_click_time(host):
    clicked_at = datetime(2026, 3, 4, 9, 8, 7)

    def _fill(dialog, seen):
        seen['time'] = dialog.time_label.text()
        seen['target'] = dialog.target_label.text()
        seen['default'] = dialog.label_edit.text()
        dialog.label_edit.setText("  第 3 轮建模  ")
        dialog.yesButton.click()

    runner = _ClickRunner(_fill)
    label = QtDialogs(run_dialog=runner).ask_marker_label(
        host, "第 2 轮", clicked_at, process_name="demo.exe", pid=1234)

    assert label == "第 3 轮建模"
    assert runner.seen['default'] == "第 2 轮"
    assert runner.seen['time'] == "已在 09:08:07 记录，可修改名称"
    assert runner.seen['target'] == "demo.exe · PID 1234"


def test_ask_marker_label_keeps_old_positional_signature(host):
    """不传 process_name / pid 的旧调用方式仍可用"""
    def _fill(dialog, seen):
        seen['target_hidden'] = dialog.target_label.isHidden()
        dialog.yesButton.click()

    runner = _ClickRunner(_fill)
    label = QtDialogs(run_dialog=runner).ask_marker_label(
        host, "基线", datetime(2026, 3, 4, 9, 8, 7))
    assert label == "基线" and runner.seen['target_hidden'] is True


def test_ask_marker_label_returns_none_on_cancel(host):
    dialogs = QtDialogs(run_dialog=_ClickRunner(_click("cancelButton")))
    assert dialogs.ask_marker_label(host, "基线", datetime.now()) is None


def test_choose_metrics_returns_selection_or_none(host):
    selected = [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]

    result = QtDialogs(run_dialog=_ClickRunner(_click("yesButton"))).choose_metrics(
        host, selected)
    assert isinstance(result, list)
    assert set(result) == set(selected)

    cancelled = QtDialogs(run_dialog=_ClickRunner(_click("cancelButton"))).choose_metrics(
        host, selected)
    assert cancelled is None


@pytest.mark.parametrize("can_tray, button, expected", [
    (False, "quit_button", "quit"),
    (False, "cancelButton", "cancel"),
    (True, "yesButton", "tray"),
    (True, "cancelButton", "cancel"),
])
def test_ask_close_action_maps_buttons_to_choices(host, can_tray, button, expected):
    dialogs = QtDialogs(run_dialog=_ClickRunner(_click(button)))
    assert dialogs.ask_close_action(host, 2, can_tray) == expected


# ---------- Dialogs：系统交互 ----------

def test_ask_save_path_normalizes_and_maps_cancel_to_none(monkeypatch, host):
    calls = []
    answers = iter([("C:/tmp/out/chart.png", "PNG 图片 (*.png)"), ("", "")])

    def _fake_get_save(*args):
        calls.append(args)
        return next(answers)

    monkeypatch.setattr(dialogs_module, "QFileDialog",
                        SimpleNamespace(getSaveFileName=_fake_get_save))
    dialogs = QtDialogs()

    path = dialogs.ask_save_path(host, "保存图表为图片", "a.png", "PNG 图片 (*.png)")
    assert path == os.path.normpath("C:/tmp/out/chart.png")
    assert calls[0] == (host, "保存图表为图片", "a.png", "PNG 图片 (*.png)")

    assert dialogs.ask_save_path(host, "选择保存位置", "b.csv", "CSV 文件 (*.csv)") is None


def test_open_folder_and_file_delegate_to_startfile_and_propagate_errors(monkeypatch):
    opened = []
    monkeypatch.setattr(dialogs_module.os, "startfile", opened.append, raising=False)
    dialogs = QtDialogs()
    dialogs.open_folder(r"C:\data")
    dialogs.open_file(r"C:\setup.exe")
    assert opened == [r"C:\data", r"C:\setup.exe"]

    def _fail(path):
        raise OSError("找不到路径")

    monkeypatch.setattr(dialogs_module.os, "startfile", _fail, raising=False)
    with pytest.raises(OSError):
        dialogs.open_folder(r"C:\missing")


def test_open_url_delegates_to_desktop_services(monkeypatch):
    opened = []

    def _open(url):
        opened.append(url.toString())
        return True

    monkeypatch.setattr(dialogs_module, "QDesktopServices", SimpleNamespace(openUrl=_open))
    assert QtDialogs().open_url("https://example.com/releases") is True
    assert opened == ["https://example.com/releases"]


def test_copy_pixmap_puts_image_on_clipboard(qapp):
    pixmap = QPixmap(12, 8)
    pixmap.fill(QColor(Qt.red))
    QtDialogs().copy_pixmap(pixmap)
    assert QApplication.clipboard().pixmap().size() == QSize(12, 8)


# ---------- Clock / ProcessQuery ----------

def test_system_clock_returns_current_local_time():
    before = datetime.now()
    now = SystemClock().now()
    after = datetime.now()
    assert before <= now <= after
    assert now.tzinfo is None


def test_psutil_process_query_sees_own_process():
    query = PsutilProcessQuery()
    own = os.getpid()

    assert query.self_pid() == own
    assert query.name_of(own)
    assert query.is_alive(own) is True

    processes = query.list_processes()
    assert processes
    assert all(len(entry) == 3 for entry in processes)
    own_entries = [entry for entry in processes if entry[0] == own]
    assert own_entries
    pid, name, create_time = own_entries[0]
    assert name == query.name_of(own)
    assert create_time is None or isinstance(create_time, float)


def test_psutil_process_query_missing_pid():
    query = PsutilProcessQuery()
    assert query.name_of(MISSING_PID) is None
    assert query.is_alive(MISSING_PID) is False


# ---------- Preferences ----------

def test_config_preferences_reads_cfg_values():
    prefs = ConfigPreferences()
    cfg.default_interval.value = 7
    cfg.retention_days.value = 30
    cfg.close_to_tray.value = True

    assert prefs.default_interval() == 7
    assert prefs.retention_days() == 30
    assert prefs.close_to_tray() is True


def test_config_preferences_ranges_come_from_app_config():
    """取值范围只在 app_config 定义一份，偏好服务原样转交"""
    import app_config
    prefs = ConfigPreferences()
    assert prefs.interval_range() == app_config.interval_range() == (1, 3600)
    assert prefs.retention_options() == app_config.retention_options()


def test_config_preferences_remember_last_metrics_roundtrip(restore_last_metrics):
    prefs = ConfigPreferences()
    assert prefs.last_metrics() == list(LEAK_CHECK_PRESET)

    prefs.remember_last_metrics([MetricType.CPU_PERCENT, "no_such_metric", MetricType.MEMORY_RSS])
    # 委托 app_config.remember_last_metrics：未知指标被归一化丢弃
    assert prefs.last_metrics() == [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]
    assert cfg.get(cfg.last_metrics) == [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]

    # 返回副本：调用方修改不影响存储
    prefs.last_metrics().append(MetricType.NUM_THREADS)
    assert prefs.last_metrics() == [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]


# ---------- TaskStatusProvider ----------

def _info(task_id, status, is_stopping=False, is_paused=False):
    return TaskInfo(
        task_id=task_id, pid=1234, process_name="demo.exe", metric_types=("cpu_percent",),
        interval=1.0, note="", status=status, start_time=datetime(2026, 1, 1, 8, 0, 0),
        end_time=None, create_time=None, is_paused=is_paused, is_stopping=is_stopping)


class _SnapshotManager:
    """只提供 get_task_info 的假 manager"""

    def __init__(self, infos):
        self.infos = {info.task_id: info for info in infos}
        self.asked = []

    def get_task_info(self, task_id):
        self.asked.append(task_id)
        return self.infos.get(task_id)


def test_manager_task_status_uses_snapshot_status():
    manager = _SnapshotManager([
        _info("running", TaskStatus.RUNNING),
        _info("paused", TaskStatus.RUNNING, is_paused=True),
        _info("pending", TaskStatus.PENDING),
        _info("stopped", TaskStatus.STOPPED),
    ])
    status = ManagerTaskStatus(manager)

    assert status.is_running("running") is True
    assert status.is_running("paused") is True
    assert status.is_running("pending") is False
    assert status.is_running("stopped") is False
    assert status.is_running("removed") is False
    assert manager.asked == ["running", "paused", "pending", "stopped", "removed"]


def test_manager_task_status_counts_stopping_as_running():
    # 停止收尾中：线程仍在运行、库里 end_time 可能尚未写入，仍算运行中
    manager = _SnapshotManager([_info("stopping", TaskStatus.RUNNING, is_stopping=True)])
    assert ManagerTaskStatus(manager).is_running("stopping") is True


def test_manager_task_status_with_real_manager(db):
    from core.monitor_manager import MonitorManager

    manager = MonitorManager(db=db)
    task_id = manager.create_task(os.getpid(), "python.exe", [MetricType.CPU_PERCENT], 1.0)
    assert task_id
    status = ManagerTaskStatus(manager)
    # 已创建未启动：PENDING 不算运行；不存在的任务也不算
    assert status.is_running(task_id) is False
    assert status.is_running("no-such-task") is False
