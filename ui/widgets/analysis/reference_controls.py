"""测试场景、版本和参考关系的编辑区；写入全部经管理器。"""
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, LineEdit, PushButton

from data.errors import StorageError


class ReferenceControls(QWidget):
    identity_changed = pyqtSignal(str)
    compare_requested = pyqtSignal(str)

    def __init__(self, manager, services, parent=None):
        super().__init__(parent)
        self._manager, self._services = manager, services
        self._task = None
        self._reference_id = None
        self._dirty = False
        self.scenario_edit = LineEdit(self)
        self.scenario_edit.setPlaceholderText('例如：连续打开关闭窗口')
        self.scenario_edit.setMaxLength(120)
        self.build_edit = LineEdit(self)
        self.build_edit.setPlaceholderText('默认使用被测程序文件版本')
        self.build_edit.setMaxLength(120)
        self.save_button = PushButton('保存场景与版本', self)
        self.reference_button = PushButton('设为场景参考', self)
        self.star_button = PushButton('标星', self)
        self.compare_button = PushButton('与参考对比', self)
        self.result_label = CaptionLabel('填写场景后，可指定一项已结束的测试作为参考', self)
        self.result_label.setWordWrap(True)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(BodyLabel('测试场景', self), 0, 0)
        layout.addWidget(self.scenario_edit, 0, 1)
        layout.addWidget(BodyLabel('版本', self), 0, 2)
        layout.addWidget(self.build_edit, 0, 3)
        buttons = QHBoxLayout()
        for button in (self.save_button, self.reference_button, self.star_button, self.compare_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons, 1, 0, 1, 4)
        layout.addWidget(self.result_label, 2, 0, 1, 4)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        self.scenario_edit.textEdited.connect(self._edited)
        self.build_edit.textEdited.connect(self._edited)
        self.save_button.clicked.connect(self._save)
        self.reference_button.clicked.connect(self._reference)
        self.star_button.clicked.connect(self._star)
        self.compare_button.clicked.connect(self._compare)
        self.set_task(None)

    def _edited(self, _text):
        self._dirty = True

    def set_task(self, task) -> None:
        changed = getattr(task, 'task_id', None) != getattr(self._task, 'task_id', None)
        changed = changed or getattr(task, 'scenario', '') != getattr(self._task, 'scenario', '')
        self._task = task
        if changed or not self._dirty:
            self.scenario_edit.setText(getattr(task, 'scenario', ''))
            self.build_edit.setText(getattr(task, 'build', ''))
            self._dirty = False
        if changed:
            self._reference_id = None
            self.result_label.setText('正在查找该场景的参考任务' if task else '请选择任务')
        editable = task is not None and not getattr(self._manager.db, 'read_only', False)
        for widget in (self.scenario_edit, self.build_edit, self.save_button, self.star_button):
            widget.setEnabled(editable)
        self.reference_button.setEnabled(editable and task.status == 'stopped')
        self.reference_button.setText('取消场景参考' if getattr(task, 'is_reference', False)
                                      else '设为场景参考')
        self.star_button.setText('取消标星' if getattr(task, 'starred', False) else '标星')
        self.compare_button.setEnabled(bool(task and self._reference_id
                                           and task.task_id != self._reference_id))

    def set_result(self, result) -> None:
        if (self._task is None or result.task_id != self._task.task_id
                or result.scenario != self._task.scenario):
            return
        self._reference_id = result.reference_id
        if result.reason:
            text = result.reason
        elif result.status == 'regression':
            count = sum(row.status == 'regression' for row in result.rows)
            text = f'较场景参考：{count} 项每轮增长显著增加；详见摘要表“较参考”列'
        elif result.status == 'comparable':
            text = '较场景参考：未发现每轮增长显著增加（95% 置信区间）'
        else:
            text = '暂不可比：' + (result.rows[0].reason if result.rows else '缺少可比较指标')
        self.result_label.setText(text)
        self.compare_button.setEnabled(bool(result.reference_id
                                           and result.reference_id != self._task.task_id))

    def _apply(self, action) -> bool:
        try:
            if not action():
                raise ValueError('任务已不存在，请刷新后重试')
        except (ValueError, StorageError) as exc:
            self._services.notifier.route('task_identity', 'warning', '未能保存', str(exc), anchor=self)
            return False
        self._dirty = False
        self.identity_changed.emit(self._task.task_id)
        return True

    def _save(self) -> bool:
        if self._task is None:
            return False
        return self._apply(lambda: self._manager.update_task_identity(
            self._task.task_id, self.scenario_edit.text(), self.build_edit.text()))

    def _reference(self) -> None:
        if self._task is None:
            return
        task_id, enabled = self._task.task_id, not self._task.is_reference
        if self._dirty and not self._save():
            return
        self._apply(lambda: self._manager.set_task_reference(task_id, enabled))

    def _star(self) -> None:
        if self._task is not None:
            self._apply(lambda: self._manager.set_task_starred(self._task.task_id,
                                                              not self._task.starred))

    def _compare(self) -> None:
        if self._reference_id:
            self.compare_requested.emit(self._reference_id)
