"""
结论条（卡片）：分析页最上方一行，先回答"有没有问题、是哪几项"。

左：判定胶囊（持续上涨 / 波动 / 平稳 / 数据不足，配色与摘要表胶囊同源）+ 主句，如
"2 / 5 项持续上涨：私有字节 +35 MB/小时、句柄数 +120 个/小时"；
右：统计依据（"基线 16:37:33 起 · 2小时15分 · 8,100 点"）与运行中任务的自动刷新状态
（"自动刷新 · 15 秒前"）。文案由 ui.formatting.verdict_summary / basis_text /
refresh_status_text 生成，控件只负责摆放与配色。
"""
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QSizePolicy
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, qconfig

from ui.formatting import VerdictSummary
from ui.widgets.analysis.summary_table import pill_style
from ui.widgets.history.common import CARD_PADDING_H

BANNER_PADDING_V = 10
BASIS_PREFIX = "依据："
SEPARATOR = " · "


class VerdictBanner(CardWidget):
    """结论条。set_* 不发信号。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._summary: Optional[VerdictSummary] = None
        self.pill = QLabel("", self)
        self.pill.setAlignment(Qt.AlignCenter)
        self.pill.setVisible(False)
        self.sentence_label = BodyLabel("", self)
        self.sentence_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.sentence_label.setMinimumWidth(120)
        self.basis_label = CaptionLabel("", self)
        self.refresh_label = CaptionLabel("", self)
        self.refresh_label.setVisible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(CARD_PADDING_H, BANNER_PADDING_V,
                                  CARD_PADDING_H, BANNER_PADDING_V)
        layout.setSpacing(12)
        layout.addWidget(self.pill)
        layout.addWidget(self.sentence_label, 1)
        layout.addWidget(self.basis_label)
        layout.addWidget(self.refresh_label)
        qconfig.themeChanged.connect(self._on_theme_changed)

    # ---------- 数据 ----------

    def set_summary(self, summary: Optional[VerdictSummary]) -> None:
        """主句与胶囊；None 清空。"""
        self._summary = summary
        verdict = summary.verdict if summary is not None else ""
        self.pill.setText(verdict)
        self.pill.setVisible(bool(verdict))
        sentence = summary.sentence if summary is not None else ""
        self.sentence_label.setText(sentence)
        # 主句可能被截断：完整内容与各判定下的全部指标放 tooltip
        tooltip = "\n".join(t for t in (sentence, summary.tooltip if summary else "") if t)
        self.sentence_label.setToolTip(tooltip)
        self.pill.setToolTip(tooltip)
        self._apply_theme()

    def set_basis(self, basis: str) -> None:
        """统计依据（basis_text 的结果）；空串隐藏。"""
        self.basis_label.setText(f"{BASIS_PREFIX}{basis}" if basis else "")
        self.basis_label.setVisible(bool(basis))

    def set_refresh_status(self, text: str) -> None:
        """自动刷新状态（运行中任务才有）；空串隐藏。"""
        self.refresh_label.setText(f"{SEPARATOR}{text}" if text else "")
        self.refresh_label.setVisible(bool(text))

    def clear(self) -> None:
        self.set_summary(None)
        self.set_basis("")
        self.set_refresh_status("")

    # ---------- 查询（测试用） ----------

    def verdict(self) -> str:
        return self.pill.text()

    def sentence(self) -> str:
        return self.sentence_label.text()

    def basis(self) -> str:
        return self.basis_label.text()

    def refresh_status(self) -> str:
        return self.refresh_label.text().removeprefix(SEPARATOR)

    # ---------- 主题 ----------

    def _apply_theme(self) -> None:
        verdict = self.pill.text()
        self.pill.setStyleSheet(pill_style(verdict) if verdict else "")

    def _on_theme_changed(self, _theme) -> None:
        # 无父对象的控件可能 C++ 端已删除而 Python 对象还在：遇到就退订
        try:
            self._apply_theme()
        except RuntimeError:
            try:
                qconfig.themeChanged.disconnect(self._on_theme_changed)
            except TypeError:
                pass
