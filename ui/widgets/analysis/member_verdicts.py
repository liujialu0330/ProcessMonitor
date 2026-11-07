"""
成员结论（卡片，进程组任务的合计视图才显示，放在判定摘要表下方）

标题行"成员结论 · 5 个成员，2 个持续上涨"，右侧"收起 / 展开"；展开时每个成员一行：
成员名 · 出现过的实例数 · 判定胶囊 · 持续上涨的指标（"私有字节 +35 MB/小时、句柄数 …"，
没有时写"—"）。点击一行发 member_selected(成员键)，整页随之切到该成员。
成员很多时（最多 20 种）行区只显示 VISIBLE_ROWS 行，其余在行区内滚动，不把下面的内容挤出首屏。

文案与判定来自 ui.formatting.build_member_rows / member_rows_header（core.verdict 的成员判定），
控件只负责摆放与配色。set_* 不发信号。
"""
from typing import Dict, List, Optional, Sequence

from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, StrongBodyLabel, TransparentPushButton, qconfig)

from ui import theme_tokens as tokens
from ui.formatting import MEMBER_ROWS_HINT, MemberVerdictRow, member_rows_header
from ui.widgets.analysis.summary_table import pill_style
from ui.widgets.history.common import CARD_PADDING_H

ROW_HEIGHT = 36
VISIBLE_ROWS = 5
NAME_WIDTH = 190
INSTANCES_WIDTH = 84
PILL_WIDTH = 84
ROW_TIP = "点击后整页切到该成员"
INSTANCES_TIP = "出现过 {count} 个实例（含已退出的）"
COLLAPSE_TEXT = "收起"
EXPAND_TEXT = "展开"


class MemberRow(QWidget):
    """成员一行：点击发 clicked(成员键)。"""

    clicked = pyqtSignal(str)

    def __init__(self, row: MemberVerdictRow, parent=None):
        super().__init__(parent)
        self.member_key = row.member_key
        self._hovered = False
        self._verdict_text = ""
        self.setFixedHeight(ROW_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.name_label = BodyLabel("", self)
        self.name_label.setFixedWidth(NAME_WIDTH)
        self.instances_label = CaptionLabel("", self)
        self.instances_label.setFixedWidth(INSTANCES_WIDTH)
        self.pill = QLabel("", self)
        self.pill.setAlignment(Qt.AlignCenter)
        self.pill.setFixedWidth(PILL_WIDTH)
        self.rising_label = CaptionLabel("", self)
        self.rising_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.rising_label.setMinimumWidth(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(12)
        layout.addWidget(self.name_label)
        layout.addWidget(self.instances_label)
        layout.addWidget(self.pill)
        layout.addWidget(self.rising_label, 1)
        qconfig.themeChanged.connect(self._on_theme_changed)
        self.set_row(row)

    def set_row(self, row: MemberVerdictRow) -> None:
        self.name_label.setText(row.name)
        self.name_label.setToolTip(row.name)
        instances = f"{row.instances} 个实例" if row.instances is not None else ""
        self.instances_label.setText(instances)
        self.instances_label.setToolTip(
            INSTANCES_TIP.format(count=row.instances) if row.instances is not None else "")
        self._verdict_text = row.verdict_text
        self.pill.setText(row.verdict_text)
        self.rising_label.setText(row.rising_text)
        self.rising_label.setToolTip(
            "\n".join(row.rising_metrics) if row.rising_metrics else "")
        self.setToolTip(ROW_TIP)
        self._apply_theme()

    def texts(self) -> tuple:
        """(成员名, 实例数文字, 判定, 持续上涨的指标)。"""
        return (self.name_label.text(), self.instances_label.text(), self.pill.text(),
                self.rising_label.text())

    def click(self) -> None:
        """等同用户点击这一行。"""
        self.clicked.emit(self.member_key)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.click()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if self._hovered:
            painter = QPainter(self)
            try:
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                painter.setBrush(tokens.qcolor(tokens.CELL_HIGHLIGHT))
                painter.drawRoundedRect(QRectF(self.rect()), 6, 6)
            finally:
                painter.end()
        super().paintEvent(event)

    def _apply_theme(self) -> None:
        self.pill.setStyleSheet(pill_style(self._verdict_text))

    def _on_theme_changed(self, _theme) -> None:
        try:
            self._apply_theme()
        except RuntimeError:
            try:
                qconfig.themeChanged.disconnect(self._on_theme_changed)
            except TypeError:
                pass


class MemberVerdictPanel(CardWidget):
    """成员结论折叠区。"""

    member_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: Dict[str, MemberRow] = {}
        self._expanded = True
        self.title_label = StrongBodyLabel("", self)
        self.hint_label = CaptionLabel(MEMBER_ROWS_HINT, self)
        self.toggle_button = TransparentPushButton(COLLAPSE_TEXT, self)
        self.toggle_button.clicked.connect(lambda: self.set_expanded(not self._expanded))
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(self.title_label)
        head.addWidget(self.hint_label)
        head.addStretch()
        head.addWidget(self.toggle_button)

        self.rows_host = QWidget()
        self.rows_host.setStyleSheet("background: transparent;")
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea{background: transparent; border: none;}")
        self.scroll.viewport().setStyleSheet("background: transparent;")
        self.scroll.setWidget(self.rows_host)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_PADDING_H, 10, CARD_PADDING_H, 10)
        layout.setSpacing(4)
        layout.addLayout(head)
        layout.addWidget(self.scroll)
        self.setVisible(False)

    # ---------- 数据 ----------

    def set_rows(self, rows: Sequence[MemberVerdictRow]) -> None:
        """整体替换各行（行按成员键复用，自动刷新不闪烁）；没有成员时整块隐藏。"""
        rows = list(rows)
        wanted = [r.member_key for r in rows]
        for key in [k for k in self._rows if k not in wanted]:
            widget = self._rows.pop(key)
            self.rows_layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        for index, row in enumerate(rows):
            widget = self._rows.get(row.member_key)
            if widget is None:
                widget = MemberRow(row, self.rows_host)
                widget.clicked.connect(self.member_selected)
                self._rows[row.member_key] = widget
            else:
                widget.set_row(row)
                self.rows_layout.removeWidget(widget)
            self.rows_layout.insertWidget(index, widget)
        self._rows = {key: self._rows[key] for key in wanted}
        self.title_label.setText(member_rows_header(rows) if rows else "")
        self.scroll.setFixedHeight(min(len(rows), VISIBLE_ROWS) * ROW_HEIGHT)
        self._apply_expanded()
        self.setVisible(bool(rows))

    def clear(self) -> None:
        self.set_rows([])

    def set_expanded(self, expanded: bool) -> None:
        if expanded != self._expanded:
            self._expanded = expanded
            self._apply_expanded()

    def _apply_expanded(self) -> None:
        self.scroll.setVisible(self._expanded and bool(self._rows))
        self.hint_label.setVisible(self._expanded)
        self.toggle_button.setText(COLLAPSE_TEXT if self._expanded else EXPAND_TEXT)

    # ---------- 查询（测试用） ----------

    def is_expanded(self) -> bool:
        return self._expanded

    def title(self) -> str:
        return self.title_label.text()

    def member_keys(self) -> List[str]:
        """各行的成员键（显示顺序）。"""
        return list(self._rows)

    def row_texts(self) -> List[tuple]:
        return [row.texts() for row in self._rows.values()]

    def row(self, member_key: str) -> Optional[MemberRow]:
        return self._rows.get(member_key)

    def click_member(self, member_key: str) -> None:
        """等同用户点击某成员的行。"""
        self._rows[member_key].click()
