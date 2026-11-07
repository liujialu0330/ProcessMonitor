"""
三个页面（监控 / 分析 / 设置）在浅色、深色主题下的离屏截图（1280×820）。

与组合根相同的装配：build_context(paths) 迁移并组装 Database / MonitorManager / 服务，
再建 MainWindow(db, manager, services, hotkey=...)；只把进程查询换成固定进程表、对话框
换成脚本替身、快捷键换成假后端（测试绝不注册真实热键）。数据目录是根 conftest 指向的
临时目录，里面造一条已结束的任务：基线 + 2 轮打点，3 个指标共 300 个点。

断言只管"截出来了、尺寸对、浅深确实不同、深色确实是深底"，图里画得好不好由人看
（输出目录见 README.md）。
"""
import dataclasses
import uuid
from datetime import datetime, timedelta

import pytest

from tests.doubles import FakeProcessQuery, ScriptedDialogs
from tests.helpers import process_events_until
from tests.visual.snapshot import DEFAULT_SIZE, load_image, mean_luminance, pixel_difference_ratio

SIZE = DEFAULT_SIZE   # (1280, 820)
# 浅深两张图至少要有这么多像素不同
MIN_DIFF_RATIO = 0.01
# 整图平均亮度：深色主题应明显偏暗、浅色明显偏亮（0–255）
DARK_MAX_LUMINANCE = 100
LIGHT_MIN_LUMINANCE = 170

PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "CADModeler.exe", 1002.5),
             (1003, "notepad.exe", 1003.5)]
SAMPLES = 100           # 每个指标的点数
INTERVAL = 10           # 秒；跨度 16.5 分钟，够判定门槛（≥10 分钟且 ≥30 点）


class _FakeHotkeyBackend:
    """注册总是成功、不碰系统热键的后端（设置页显示"已启用"）。"""

    def register(self, hotkey_id, modifiers, vk):
        return True

    def unregister(self, hotkey_id):
        pass


def _seed_finished_task(db, manager) -> str:
    """一条已结束的任务：私有字节持续上涨、句柄平稳、GDI 缓慢爬升；基线 + 2 轮打点。"""
    from data.models import DataPoint, MonitorTask
    from utils.metrics import MetricType

    metrics = [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES, MetricType.NUM_GDI_OBJECTS]
    start = (datetime.now() - timedelta(hours=2)).replace(microsecond=0)
    duration = (SAMPLES - 1) * INTERVAL
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=1002, process_name="CADModeler.exe",
                       metric_types=list(metrics), interval=float(INTERVAL),
                       start_time=start, end_time=start + timedelta(seconds=duration),
                       status="stopped", note="截图夹具 · 建模 200 次")
    assert db.save_task(task)
    points = []
    for i in range(SAMPLES):
        ts = start + timedelta(seconds=i * INTERVAL)
        values = {
            MetricType.MEMORY_PRIVATE: 512000.0 + i * 410 + (i % 7) * 35,
            MetricType.NUM_HANDLES: 812.0 + (3 if i % 23 in (11, 12) else 0),
            MetricType.NUM_GDI_OBJECTS: 1450.0 + i // 4,
        }
        points += [DataPoint(task.task_id, ts, value, metric) for metric, value in values.items()]
    assert len(points) == 300
    db.save_data_points(points)
    assert manager.add_marker(task.task_id, "基线", start + timedelta(minutes=1))
    assert manager.add_marker(task.task_id, "第 1 轮", start + timedelta(minutes=6))
    assert manager.add_marker(task.task_id, "第 2 轮", start + timedelta(minutes=11))
    return task.task_id


@pytest.fixture
def context(qapp, visual_data_dir):
    from app.bootstrap import build_context
    from ui.hotkey import MarkerHotkey

    ctx = build_context(visual_data_dir, retention_days=0)
    services = dataclasses.replace(
        ctx.services, process_query=FakeProcessQuery(PROCESSES),
        dialogs=ScriptedDialogs(ask_close_action='quit'))
    hotkey = MarkerHotkey(_FakeHotkeyBackend())
    hotkey.bind("Ctrl+Alt+M")
    task_id = _seed_finished_task(ctx.db, ctx.manager)
    yield ctx, services, hotkey, task_id
    process_events_until(lambda: not services.threads.entries(), timeout=10)
    ctx.manager.stop_all_tasks()
    ctx.manager.join_all(3000)
    ctx.db.close()


def _window_builder(ctx, services, hotkey, page_attr):
    def build():
        from ui.main_window import MainWindow
        window = MainWindow(ctx.db, ctx.manager, services, hotkey=hotkey)
        # 不发起真实联网的检查更新（主窗口 3 秒后会静默检查一次）
        window.about_group.check_update = lambda *args, **kwargs: None
        window.switchTo(getattr(window, page_attr))
        return window
    return build


def _settle_monitor(window):
    assert process_events_until(lambda: not window.monitor_page.form.picker.is_loading())


def _settle_analysis(window):
    vm = window.analysis_page.vm
    assert process_events_until(lambda: vm.last_analysis is not None, timeout=15), \
        "分析页应在期限内加载完任务分析"
    assert process_events_until(lambda: not window.services.threads.entries(), timeout=10)


def _settle_setting(window):
    process_events_until(lambda: False, timeout=0.3)


def _assert_pair(light, dark):
    for path in (light, dark):
        assert path.is_file(), path
        image = load_image(path)
        assert (image.width(), image.height()) == SIZE, path
    ratio = pixel_difference_ratio(light, dark)
    assert ratio > MIN_DIFF_RATIO, f"浅深两张图几乎相同（差异 {ratio:.2%}）"
    assert mean_luminance(dark) < DARK_MAX_LUMINANCE, "深色主题截图应是深底"
    assert mean_luminance(light) > LIGHT_MIN_LUMINANCE, "浅色主题截图应是浅底"


@pytest.mark.parametrize("page_attr, name, settle", [
    ("monitor_page", "monitor", _settle_monitor),
    ("analysis_page", "analysis", _settle_analysis),
    ("setting_page", "setting", _settle_setting),
])
def test_page_dual_theme(visual_snapshot, context, page_attr, name, settle):
    ctx, services, hotkey, _task_id = context
    light, dark = visual_snapshot(_window_builder(ctx, services, hotkey, page_attr), name,
                                  SIZE, settle=settle)
    _assert_pair(light, dark)
