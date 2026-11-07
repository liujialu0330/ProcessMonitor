"""
离屏双主题截图用例的开关与夹具

默认跳过：截图要建主窗口、跑后台查询，比普通用例慢得多，且产物需要人看。
显式加 --visual 才运行，--visual-out 指定输出目录（默认 tests/visual/.out/）。
用 `python -m pytest tests/visual --visual` 运行：本 conftest 只有在命令行直接给出
tests/visual 时才会在解析参数前加载，选项才被识别。

数据目录：根 conftest 的自动夹具已把 PROCESS_MONITOR_DATA_DIR 指向本用例的临时目录，
这里的夹具再核对一次，截图用例绝不碰开发机的 data\\。
"""
import os
from pathlib import Path

import pytest

import config
from tests.visual.snapshot import DEFAULT_OUT_DIR, capture_dual_theme

VISUAL_OPTION = "--visual"
VISUAL_OUT_OPTION = "--visual-out"
SKIP_REASON = "截图用例默认跳过，加 --visual 运行"


def pytest_addoption(parser):
    group = parser.getgroup("visual", "离屏双主题截图")
    group.addoption(VISUAL_OPTION, action="store_true", default=False,
                    help="运行 tests/visual 下的离屏双主题截图用例")
    group.addoption(VISUAL_OUT_OPTION, action="store", default=None,
                    help="截图输出目录（默认 tests/visual/.out/）")


def _option(config_, name, default):
    # 本 conftest 不是"初始 conftest"时（如全量运行 tests/），选项未注册，按默认值处理
    return config_.getoption(name, default=default)


@pytest.fixture(autouse=True)
def _require_visual_option(request):
    if not _option(request.config, VISUAL_OPTION, False):
        pytest.skip(SKIP_REASON)


@pytest.fixture
def visual_out_dir(request) -> Path:
    """截图输出目录"""
    out = _option(request.config, VISUAL_OUT_OPTION, None)
    return Path(out).resolve() if out else DEFAULT_OUT_DIR


@pytest.fixture
def visual_data_dir(app_paths):
    """本用例的应用路径：确认数据目录已被根 conftest 指向临时目录（不是项目 data\\）"""
    data_dir = os.environ.get(config.DATA_DIR_ENV)
    assert data_dir, "根 conftest 应已设置数据目录环境变量"
    from app.paths import AppPaths
    real_env = {k: v for k, v in os.environ.items() if k != config.DATA_DIR_ENV}
    real_data = Path(AppPaths.resolve(env=real_env).data_dir).resolve()
    project_data = Path(config.__file__).resolve().parent / "data"
    assert Path(app_paths.data_dir).resolve() == Path(data_dir).resolve()
    assert Path(data_dir).resolve() not in (project_data, real_data)
    app_paths.ensure_dirs()
    return app_paths


@pytest.fixture
def visual_snapshot(qapp, visual_out_dir, visual_data_dir):
    """截图函数：visual_snapshot(build_widget, name, size=..., settle=None) -> (浅色图, 深色图)。"""
    def snap(build_widget, name, size=None, *, settle=None):
        kwargs = {"out_dir": visual_out_dir, "settle": settle}
        if size is not None:
            kwargs["size"] = size
        return capture_dual_theme(build_widget, name, **kwargs)
    return snap
