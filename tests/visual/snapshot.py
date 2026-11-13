"""
离屏双主题截图

capture_dual_theme(build_widget, name) 在浅色、深色两种主题下各新建一次控件（同一个
控件切主题会残留旧样式，结果不可信），放进带主题窗口底色的容器，按给定尺寸布局后
截图存成 {name}_light.png / {name}_dark.png。

主题只在内存里切换（qconfig._theme + themeChanged，与现有控件测试相同），不写配置
文件；结束后恢复原主题。容器底色取 Fluent 窗口的浅 / 深底色：主窗口在 Windows 11 上
启用了 Mica（自身背景透明），离屏截图时没有系统合成的底色，不垫一层深色底会截成
"深色文字控件 + 浅色背景"的假象。
"""
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple, Union

from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget
from qfluentwidgets import Theme, qconfig

# 与 qfluentwidgets FluentWindow 的浅 / 深窗口底色一致
LIGHT_BACKGROUND = "#f3f3f3"
DARK_BACKGROUND = "#202020"
DEFAULT_SIZE = (1280, 820)
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / ".out"

# 显示后、截图前处理事件的轮数：布局与样式表各需要一轮
SETTLE_ROUNDS = 2

_HOST_NAME = "visualSnapshotHost"

BuildWidget = Callable[[], QWidget]
# 显示后、截图前的回调（参数为 build_widget 返回的控件），用于等待异步数据到达
Settle = Callable[[QWidget], None]


def process_events(rounds: int = SETTLE_ROUNDS) -> None:
    """处理若干轮事件（含延迟删除）。"""
    for _ in range(rounds):
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _apply_theme(theme: Theme) -> None:
    qconfig._theme = theme
    qconfig.themeChanged.emit(theme)


@contextmanager
def theme_applied(theme: Theme) -> Iterator[None]:
    """在内存中切到给定主题（不写配置文件），退出时恢复原主题。"""
    original = qconfig._theme
    _apply_theme(theme)
    try:
        yield
    finally:
        _apply_theme(original)


def _dispose(widget: QWidget) -> None:
    widget.hide()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def capture(build_widget: BuildWidget, theme: Theme, path: Path,
            size: Tuple[int, int] = DEFAULT_SIZE, *,
            settle: Optional[Settle] = None) -> Path:
    """在给定主题下新建控件、放进主题底色容器、截图存到 path。"""
    background = DARK_BACKGROUND if theme == Theme.DARK else LIGHT_BACKGROUND
    with theme_applied(theme):
        host = QWidget()
        host.setObjectName(_HOST_NAME)
        host.setAttribute(Qt.WA_StyledBackground, True)
        host.setStyleSheet(f"QWidget#{_HOST_NAME} {{ background: {background}; }}")
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        try:
            widget = build_widget()
            layout.addWidget(widget)
            host.resize(*size)
            host.show()
            process_events()
            if settle is not None:
                settle(widget)
                process_events()
            path.parent.mkdir(parents=True, exist_ok=True)
            if not host.grab().save(str(path), "PNG"):
                raise OSError(f"截图保存失败：{path}")
        finally:
            _dispose(host)
            process_events()
    return path


def capture_dual_theme(build_widget: BuildWidget, name: str,
                       size: Tuple[int, int] = DEFAULT_SIZE,
                       out_dir: Union[str, Path, None] = None, *,
                       settle: Optional[Settle] = None) -> Tuple[Path, Path]:
    """浅色、深色各截一张，返回 (浅色图路径, 深色图路径)。

    Args:
        build_widget: 无参工厂，每种主题调用一次、返回一个新控件（可以是主窗口）；
            控件随容器一起释放，调用方不要再持有或释放它
        name: 文件名前缀，输出 {name}_light.png / {name}_dark.png
        size: 截图尺寸（逻辑像素）
        out_dir: 输出目录，默认 tests/visual/.out/
        settle: 显示后、截图前的回调（参数为控件），用于等待异步加载的数据
    """
    directory = Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    light = capture(build_widget, Theme.LIGHT, directory / f"{name}_light.png", size,
                    settle=settle)
    dark = capture(build_widget, Theme.DARK, directory / f"{name}_dark.png", size,
                   settle=settle)
    return light, dark


def load_image(path: Union[str, Path]) -> QImage:
    """读 PNG 并统一成 32 位 RGB，便于逐像素比较。"""
    image = QImage(str(path))
    if image.isNull():
        raise OSError(f"无法读取图片：{path}")
    return image.convertToFormat(QImage.Format_RGB32)


def pixel_difference_ratio(path_a: Union[str, Path], path_b: Union[str, Path]) -> float:
    """两张同尺寸图片中颜色不同的像素占比（0–1）；尺寸不同视为完全不同。"""
    import numpy as np

    a, b = load_image(path_a), load_image(path_b)
    if a.size() != b.size():
        return 1.0

    def pixels(image: QImage):
        ptr = image.constBits()
        ptr.setsize(image.byteCount())
        rows = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine())
        return rows[:, :image.width() * 4].reshape(image.height(), image.width(), 4)[..., :3]

    differs = np.any(pixels(a) != pixels(b), axis=-1)
    return float(differs.mean())


def mean_luminance(path: Union[str, Path]) -> float:
    """整图平均亮度（0–255，Rec. 601 加权），用于粗判深 / 浅底。"""
    import numpy as np

    image = load_image(path)
    ptr = image.constBits()
    ptr.setsize(image.byteCount())
    rows = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine())
    bgr = rows[:, :image.width() * 4].reshape(image.height(), image.width(), 4)[..., :3]
    b, g, r = bgr[..., 0].astype(float), bgr[..., 1].astype(float), bgr[..., 2].astype(float)
    return float((0.299 * r + 0.587 * g + 0.114 * b).mean())
