"""
测试替身自身的行为：记录、脚本队列、未预设即报错、时钟推进、进程表与存活判定、
任务状态、偏好，以及 make_services 的组装与替换。
"""
from datetime import datetime, timedelta

import pytest

from tests.doubles import (
    DEFAULT_FAKE_NOW, FakeClock, FakeInfoBar, FakePreferences, FakeProcessQuery,
    FakeTaskStatus, RecordingNotifier, ScriptedDialogs,
)
from tests.factories import make_services
from ui.services import (
    Clock, Dialogs, MonitorPreferences, Notifier, ProcessQuery, TaskStatusProvider,
    UiServices,
)
from utils.metrics import LEAK_CHECK_PRESET, MetricType


def test_doubles_satisfy_service_protocols():
    assert isinstance(RecordingNotifier(), Notifier)
    assert isinstance(ScriptedDialogs(), Dialogs)
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeProcessQuery(), ProcessQuery)
    assert isinstance(FakeTaskStatus(), TaskStatusProvider)
    assert isinstance(FakePreferences(), MonitorPreferences)


# ---------- RecordingNotifier ----------

def test_recording_notifier_records_calls_in_order():
    notifier = RecordingNotifier()
    parent = object()
    notifier.success(parent, "成功", "已保存")
    notifier.warning(parent, "注意", "进程已退出", duration=15000)
    notifier.error(parent, "错误", "采集失败")

    assert notifier.levels() == ["success", "warning", "error"]
    warning = notifier.messages("warning")[0]
    assert (warning.title, warning.content, warning.duration, warning.parent) == (
        "注意", "进程已退出", 15000, parent)
    assert notifier.last().content == "采集失败"
    assert notifier.last("success").title == "成功"
    assert len(notifier.messages()) == 3


def test_recording_notifier_effective_duration_follows_production_defaults():
    notifier = RecordingNotifier()
    notifier.info(None, "提示", "a")
    notifier.warning(None, "注意", "b")
    notifier.error(None, "错误", "c")
    notifier.warning(None, "注意", "d", duration=10000)
    assert [n.effective_duration for n in notifier.messages()] == [2000, 3000, -1, 10000]


def test_recording_notifier_last_without_records_fails_clearly():
    notifier = RecordingNotifier()
    with pytest.raises(AssertionError, match="尚未弹出"):
        notifier.last()
    notifier.info(None, "提示", "x")
    with pytest.raises(AssertionError, match="error"):
        notifier.last("error")
    with pytest.raises(ValueError):
        notifier.messages("fatal")
    notifier.clear()
    assert notifier.messages() == []


def test_fake_infobar_supports_close_signal_dedup_and_widgets(qapp):
    notifier = RecordingNotifier()
    showing = {}

    def _report(key):
        # 与页面"同一错误仍在显示时不再叠加"的写法一致
        if key in showing:
            return
        bar = notifier.error(None, "错误", key)
        showing[key] = bar
        bar.closedSignal.connect(lambda k=key: showing.pop(k, None))

    _report("采集失败")
    _report("采集失败")
    assert len(notifier.messages("error")) == 1

    bar = notifier.last().bar
    assert isinstance(bar, FakeInfoBar)
    button = object()
    bar.addWidget(button)
    assert bar.widgets == [button]

    bar.close()
    assert bar.closed and "采集失败" not in showing
    bar.close()  # 重复关闭不再发信号
    _report("采集失败")
    assert len(notifier.messages("error")) == 2


# ---------- ScriptedDialogs ----------

def test_scripted_dialogs_queue_then_fixed_value():
    dialogs = ScriptedDialogs(confirm=False)
    dialogs.push("confirm", True, True)

    assert [dialogs.confirm(None, "t", "c", "删除") for _ in range(3)] == [True, True, False]
    assert dialogs.pending("confirm") == 0
    assert dialogs.calls_of("confirm")[0] == {
        "parent": None, "title": "t", "content": "c", "confirm_text": "删除",
        "cancel_text": "取消"}


def test_scripted_dialogs_none_is_a_valid_preset():
    dialogs = ScriptedDialogs(ask_marker_label=None, ask_save_path=None)
    clicked_at = datetime(2026, 1, 1, 8, 0, 0)
    assert dialogs.ask_marker_label(None, "标记 1", clicked_at) is None
    assert dialogs.ask_save_path(None, "保存", "a.csv", "CSV 文件 (*.csv)") is None
    assert dialogs.last_call("ask_marker_label")["timestamp"] == clicked_at


@pytest.mark.parametrize("method, args", [
    ("confirm", (None, "t", "c", "确定")),
    ("ask_marker_label", (None, "标记 1", datetime(2026, 1, 1))),
    ("choose_metrics", (None, [])),
    ("ask_save_path", (None, "保存", "a.png", "PNG 图片 (*.png)")),
    ("ask_close_action", (None, 1, True)),
])
def test_scripted_dialogs_unscripted_call_fails_clearly(method, args):
    dialogs = ScriptedDialogs()
    with pytest.raises(AssertionError, match=f"ScriptedDialogs.{method} 未预设"):
        getattr(dialogs, method)(*args)
    # 失败的调用同样被记录，便于排查
    assert dialogs.methods_called() == [method]


def test_scripted_dialogs_side_effect_methods_need_no_script():
    dialogs = ScriptedDialogs()
    dialogs.open_folder(r"C:\data")
    dialogs.open_file(r"C:\setup.exe")
    assert dialogs.open_url("https://example.com") is True
    dialogs.copy_pixmap("pixmap")

    assert dialogs.methods_called() == ["open_folder", "open_file", "open_url", "copy_pixmap"]
    assert dialogs.last_call("open_folder") == {"path": r"C:\data"}
    assert dialogs.last_call("copy_pixmap") == {"pixmap": "pixmap"}


def test_scripted_dialogs_raises_scripted_exception():
    dialogs = ScriptedDialogs()
    dialogs.push("open_folder", OSError("找不到路径"))
    with pytest.raises(OSError, match="找不到路径"):
        dialogs.open_folder(r"C:\missing")
    dialogs.open_folder(r"C:\ok")  # 队列耗尽后回到默认行为


def test_scripted_dialogs_callable_preset_runs_while_dialog_is_open():
    """可调用预设在"对话框打开期间"执行：此时调用已记录，返回值即对话框结果。"""
    dialogs = ScriptedDialogs()
    happened = []

    def _while_open():
        # 回调执行时本次调用已入账，可取到参数
        happened.append(dialogs.last_call("confirm")["title"])
        return False

    dialogs.push("confirm", _while_open)
    dialogs.always("confirm", lambda: True)

    assert dialogs.confirm(None, "删除任务", "确定？", "删除") is False
    assert happened == ["删除任务"]
    assert dialogs.confirm(None, "再删", "确定？", "删除") is True  # 固定值同样支持回调
    assert dialogs.pending("confirm") == 0


def test_scripted_dialogs_callable_preset_may_raise_or_return_exception():
    dialogs = ScriptedDialogs()

    def _boom():
        raise RuntimeError("回调出错")

    dialogs.push("open_folder", _boom, lambda: OSError("找不到路径"))
    with pytest.raises(RuntimeError, match="回调出错"):
        dialogs.open_folder(r"C:\first")
    with pytest.raises(OSError, match="找不到路径"):
        dialogs.open_folder(r"C:\second")
    assert dialogs.calls_of("open_folder") == [{"path": r"C:\first"}, {"path": r"C:\second"}]


def test_scripted_dialogs_choose_metrics_and_close_action():
    dialogs = ScriptedDialogs(ask_close_action="tray")
    dialogs.push("choose_metrics", [MetricType.CPU_PERCENT], None)
    selected = [MetricType.MEMORY_RSS]

    result = dialogs.choose_metrics(None, selected)
    assert result == [MetricType.CPU_PERCENT]
    assert dialogs.choose_metrics(None, selected) is None
    assert dialogs.last_call("choose_metrics")["selected"] == selected
    assert dialogs.ask_close_action(None, 3, True) == "tray"
    assert dialogs.last_call("ask_close_action") == {
        "parent": None, "running_count": 3, "can_tray": True}


def test_scripted_dialogs_rejects_unknown_method_and_missing_call():
    dialogs = ScriptedDialogs()
    with pytest.raises(ValueError):
        dialogs.push("ask_text", "x")
    with pytest.raises(ValueError):
        ScriptedDialogs(confrim=True)
    with pytest.raises(AssertionError, match="从未被调用"):
        dialogs.last_call("confirm")


# ---------- FakeClock ----------

def test_fake_clock_advance_and_set():
    clock = FakeClock()
    assert clock.now() == DEFAULT_FAKE_NOW

    start = datetime(2026, 5, 1, 10, 0, 0)
    clock = FakeClock(start)
    assert clock.advance(timedelta(minutes=5)) == start + timedelta(minutes=5)
    assert clock.advance(30) == start + timedelta(minutes=5, seconds=30)
    assert clock.now() == start + timedelta(minutes=5, seconds=30)

    later = datetime(2026, 5, 2, 0, 0, 0)
    clock.set(later)
    assert clock.now() == later


# ---------- FakeProcessQuery ----------

PROCESSES = [(1002, "beta.exe", 2.0), (1001, "Alpha.exe", 1.0), (1003, "gamma.exe", None)]


def test_fake_process_query_lists_sorted_and_names():
    query = FakeProcessQuery(PROCESSES, self_pid=4242)
    assert [entry[0] for entry in query.list_processes()] == [1001, 1002, 1003]
    assert query.name_of(1002) == "beta.exe"
    assert query.name_of(5) is None
    assert query.self_pid() == 4242
    assert ("name_of", 1002) in query.calls and ("list_processes", None) in query.calls


def test_fake_process_query_alive_follows_process_list_by_default():
    query = FakeProcessQuery(PROCESSES, self_pid=4242)
    assert query.is_alive(1001) and query.is_alive(4242)
    assert not query.is_alive(5)

    query.kill(1001)
    assert not query.is_alive(1001)
    assert query.name_of(1001) is None

    query.processes.append((7, "late.exe", 7.0))
    assert query.is_alive(7)


def test_fake_process_query_explicit_alive_set():
    query = FakeProcessQuery(PROCESSES, alive={1002})
    # 列表里有、但已退出：以显式 alive 集合为准
    assert query.name_of(1001) == "Alpha.exe"
    assert not query.is_alive(1001)
    assert query.is_alive(1002)
    query.kill(1002)
    assert not query.is_alive(1002)


# ---------- FakeTaskStatus / FakePreferences ----------

def test_fake_task_status():
    status = FakeTaskStatus(["a"])
    assert status.is_running("a") and not status.is_running("b")
    status.set_running("b")
    status.set_running("a", False)
    assert status.running == {"b"}


def test_fake_task_status_all_running_switch():
    status = FakeTaskStatus(all_running=True)
    assert status.is_running("任意任务") and status.is_running("")
    status.all_running = False
    assert not status.is_running("任意任务")
    status.set_running("x")
    assert status.is_running("x")


def test_fake_preferences():
    prefs = FakePreferences()
    assert prefs.last_metrics() == list(LEAK_CHECK_PRESET)
    assert (prefs.default_interval(), prefs.retention_days(), prefs.close_to_tray()) == (
        1, 0, False)

    prefs = FakePreferences(last_metrics=[MetricType.CPU_PERCENT], default_interval=5,
                            retention_days=30, close_to_tray=True)
    assert (prefs.default_interval(), prefs.retention_days(), prefs.close_to_tray()) == (
        5, 30, True)

    prefs.remember_last_metrics([MetricType.MEMORY_RSS])
    assert prefs.last_metrics() == [MetricType.MEMORY_RSS]
    assert prefs.remembered == [[MetricType.MEMORY_RSS]]
    prefs.last_metrics().append("x")  # 返回副本
    assert prefs.last_metrics() == [MetricType.MEMORY_RSS]

    prefs.tray = False
    assert prefs.close_to_tray() is False

    # 取值范围默认与生产配置一致，可改写
    import app_config
    assert prefs.interval_range() == app_config.interval_range()
    assert prefs.retention_options() == app_config.retention_options()
    prefs.retention_options().append(999)   # 返回副本
    assert 999 not in prefs.retention_options()


# ---------- make_services ----------

def test_make_services_defaults_to_doubles():
    services = make_services()
    assert isinstance(services, UiServices)
    assert isinstance(services.notifier, RecordingNotifier)
    assert isinstance(services.dialogs, ScriptedDialogs)
    assert isinstance(services.clock, FakeClock)
    assert isinstance(services.process_query, FakeProcessQuery)
    assert isinstance(services.task_status, FakeTaskStatus)
    assert isinstance(services.preferences, FakePreferences)
    # 线程登记表用真实对象（QObject，离屏可用），初始为空
    from core.shutdown import ThreadRegistry
    assert isinstance(services.threads, ThreadRegistry)
    assert services.threads.entries() == []
    # 每次组装都是新实例，用例之间不共享记录
    assert make_services().notifier is not services.notifier
    assert make_services().threads is not services.threads


def test_make_services_overrides_by_field_name():
    clock = FakeClock(datetime(2026, 2, 3))
    dialogs = ScriptedDialogs(confirm=True)
    services = make_services(clock=clock, dialogs=dialogs)
    assert services.clock is clock and services.dialogs is dialogs
    assert isinstance(services.notifier, RecordingNotifier)
    with pytest.raises(TypeError):
        make_services(clocks=clock)
