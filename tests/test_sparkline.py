"""
SparklineWidget 单元测试（v1.3.0 批3 E）

覆盖 maxlen 截断、clear 语义，以及 paintEvent 四态（0点/1点/平线/常规）不崩溃、
不留下异常痕迹。PyQt5 虚方法（paintEvent/eventFilter）里的异常在 pytest 表面
不一定会红——本项目批2 曾实测踩坑：eventFilter 内异常若无防护，既不保证抛出
AssertionError 也不保证进程干净退出，"测试没红"不能作为验证依据，必须额外
捕获 stderr 断言不含 Traceback 文本。
"""
import contextlib
import io

from ui.components.sparkline import SparklineWidget


def test_append_bounded_maxlen(qapp):
    """追加点数超过 maxlen 后，缓冲区维持在 maxlen 且保留最新的值（旧值被丢弃）"""
    widget = SparklineWidget(maxlen=5)
    for i in range(10):
        widget.append(float(i))

    assert len(widget._values) == 5
    assert list(widget._values) == [5.0, 6.0, 7.0, 8.0, 9.0]


def test_clear_empties(qapp):
    """clear 后缓冲区为空"""
    widget = SparklineWidget(maxlen=5)
    widget.append(1.0)
    widget.append(2.0)
    assert len(widget._values) == 2

    widget.clear()
    assert len(widget._values) == 0


def test_paint_no_crash(qapp):
    """
    paintEvent 覆盖四态（0点/1点/平线/常规）均不抛异常、不留 Traceback。

    widget.grab() 会同步触发一次 paintEvent。用 contextlib.redirect_stderr
    捕获期间的 stderr 输出，断言其中不含 "Traceback" 文本，防止"pytest 没红"
    掩盖真实的绘制异常（该验证手法已在独立脚本中实测确认对 paintEvent 内的
    未捕获异常有效）。
    """
    widget = SparklineWidget(maxlen=60)
    widget.resize(120, 36)

    stderr_capture = io.StringIO()
    with contextlib.redirect_stderr(stderr_capture):
        # 0 个点：不画任何内容
        widget.grab()

        # 1 个点：画一个圆点
        widget.append(10.0)
        widget.grab()

        # 平线：min == max，画一条水平中线
        widget.append(10.0)
        widget.append(10.0)
        widget.grab()

        # 常规：数值有起伏
        widget.clear()
        for value in (1.0, 5.0, 3.0, 8.0, 2.0):
            widget.append(value)
        widget.grab()

    stderr_output = stderr_capture.getvalue()
    assert "Traceback" not in stderr_output
