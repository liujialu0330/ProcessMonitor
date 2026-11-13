"""
报告图片 2.0 版式截图（--visual 才运行，产物要人看）

一条 5 指标任务（私有字节、句柄数持续上涨，其余平稳）、基线 + 3 轮打点、带数据质量与被测
目标元数据、120 字备注，在深色界面下导出：

- report_v2.png：默认"固定浅色"——标题块换行、元信息网格、结论表、区间增量表、两张小倍数图；
- report_v2_follow_dark.png："跟随主题"时的深色报告，对照看深色下的对比度与网格缝。
"""
from qfluentwidgets import Theme

from app_config import REPORT_SCHEME_FOLLOW, cfg
from core.target_info import TargetInfo
from data.models import QualityStats
from tests.test_report_sheet import GENERATED, LONG_NOTE, analyse, seed_task
from tests.visual.snapshot import mean_luminance, theme_applied


def _task(db):
    task = seed_task(db, note=LONG_NOTE)
    task.quality = QualityStats(240, 239, 1, 14, 3)
    task.meta_json = TargetInfo(
        exe_path=r"C:\Program Files\Demo\demo.exe", file_version="3.2.1.0",
        cmdline=("demo.exe", "--auto"), os_version="Windows 11 10.0.26200",
        logical_cores=16, total_memory_bytes=32 * 1024 ** 3, tool_version="2.0.0",
        tool_is_admin=False).to_json()
    return task


def test_report_image_v2(qapp, db, visual_out_dir, visual_data_dir):
    from ui.widgets.analysis.report_sheet import render_report_for
    visual_out_dir.mkdir(parents=True, exist_ok=True)
    task = _task(db)
    analysis = analyse(db, task)

    with theme_applied(Theme.DARK):
        light = render_report_for(task, analysis, GENERATED, "全部")
        item = cfg.report_color_scheme
        item.value = REPORT_SCHEME_FOLLOW
        try:
            dark = render_report_for(task, analysis, GENERATED, "全部")
        finally:
            item.value = item.defaultValue

    light_path = visual_out_dir / "report_v2.png"
    dark_path = visual_out_dir / "report_v2_follow_dark.png"
    assert light.save(str(light_path)) and dark.save(str(dark_path))
    assert light.width() == dark.width() == 1600
    assert light.height() > 1200
    assert mean_luminance(light_path) > 200
    assert mean_luminance(dark_path) < 100
