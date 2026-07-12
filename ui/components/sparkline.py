"""
迷你趋势图组件（Sparkline）
无坐标轴、无文字的极简折线图，用于任务卡片内联展示单个指标最近若干次采集的
趋势走向（v1.3.0 批3 E）。
"""
from collections import deque

from PyQt5.QtCore import Qt, QPointF
from PyQt5.QtGui import QPainter, QPen, QColor, QPolygonF
from PyQt5.QtWidgets import QWidget, QSizePolicy

from qfluentwidgets import isDarkTheme

# 固定高度（像素），横向随父布局拉伸
FIXED_HEIGHT = 36

# 折线取色（评审修订 N5）：在 paintEvent 内动态调用 isDarkTheme()，不连接
# qconfig.themeChanged——任务卡片随监控任务动态创建/销毁，这样实现不必在卡片
# 销毁时手动断开主题信号连接，零信号残留。颜色自含常量，不依赖批2的
# ui/chart_theme.py（该模块服务于历史页图表，两者取色场景独立）。
_LIGHT_COLOR = QColor("#0078D4")
_DARK_COLOR = QColor("#4cc2ff")


class SparklineWidget(QWidget):
    """迷你趋势折线图：固定高度 36px，横向拉伸；纵向按当前缓冲区 min-max 自适应"""

    def __init__(self, maxlen: int = 60, parent=None):
        """
        Args:
            maxlen: 缓冲区最多保留的采集点数，超出后自动丢弃最旧的点
            parent: 父窗口
        """
        super().__init__(parent)
        self._maxlen = maxlen
        self._values = deque(maxlen=maxlen)

        self.setFixedHeight(FIXED_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def append(self, value: float) -> None:
        """追加一个采集点并触发重绘"""
        self._values.append(value)
        self.update()

    def clear(self) -> None:
        """清空缓冲区并触发重绘"""
        self._values.clear()
        self.update()

    def paintEvent(self, event):
        """
        QPainter 抗锯齿折线：纵向按当前缓冲区 min-max 自适应（min==max 画一条
        水平中线）；0 个点不画任何内容，1 个点画一个圆点；无坐标轴、无文字。

        全部分支均已避开除零（step 的分母固定在 count>=2 分支内，恒 >=1；
        span==0 时单独走中线分支不做除法），widget 尚未布局（width/height 为0）
        时也只会画出退化的点/线，不会抛异常——paintEvent 里的异常在 PyQt5 下
        无法被安全捕获后优雅降级，必须在源头保证不发生。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        count = len(self._values)
        width = self.width()
        height = self.height()
        color = _DARK_COLOR if isDarkTheme() else _LIGHT_COLOR

        if count == 1:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(width / 2, height / 2), 2.0, 2.0)
        elif count >= 2:
            pen = QPen(color)
            pen.setWidthF(1.5)
            painter.setPen(pen)

            min_value = min(self._values)
            max_value = max(self._values)
            span = max_value - min_value
            step = width / (count - 1)

            points = []
            for i, value in enumerate(self._values):
                x = i * step
                if span == 0:
                    y = height / 2
                else:
                    ratio = (value - min_value) / span
                    y = height - ratio * height
                points.append(QPointF(x, y))

            painter.drawPolyline(QPolygonF(points))
        # count == 0：不画任何内容

        painter.end()
