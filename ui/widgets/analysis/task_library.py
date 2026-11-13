"""可筛选的任务库：选择、标星状态与经过双重确认的后台批量删除。"""
from PyQt5.QtCore import QItemSelectionModel, Qt, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QTableWidgetItem,
                             QVBoxLayout, QWidget)
from qfluentwidgets import CaptionLabel, CheckBox, ComboBox, PushButton, SearchLineEdit, TableWidget

from data.errors import StorageError


class TaskLibraryPanel(QWidget):
    task_selected = pyqtSignal(str)
    changed = pyqtSignal()

    def __init__(self, manager, services, parent=None):
        super().__init__(parent)
        self._manager, self._services = manager, services
        self._tasks, self._visible = [], []
        self._worker = None
        self.toggle_button = PushButton('展开任务库', self)
        self.body = QWidget(self)
        self.body.setVisible(False)
        self.search_edit = SearchLineEdit(self.body)
        self.search_edit.setPlaceholderText('搜索进程、备注、场景、版本或 PID')
        self.scene_combo = ComboBox(self.body)
        self.starred_only = CheckBox('仅标星', self.body)
        self.select_button = PushButton('选择筛选结果', self.body)
        self.delete_button = PushButton('删除所选', self.body)
        self.count_label = CaptionLabel('', self.body)
        self.table = TableWidget(self.body)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(['进程 / 备注', '场景', '版本', '标星', '参考', '状态'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(120)
        self.table.setMaximumHeight(280)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 6):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.toggle_button)
        outer.addWidget(self.body)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        filters = QHBoxLayout()
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.scene_combo)
        filters.addWidget(self.starred_only)
        body.addLayout(filters)
        body.addWidget(self.table)
        actions = QHBoxLayout()
        actions.addWidget(self.count_label)
        actions.addStretch(1)
        actions.addWidget(self.select_button)
        actions.addWidget(self.delete_button)
        body.addLayout(actions)
        self.toggle_button.clicked.connect(self._toggle)
        self.search_edit.textChanged.connect(self._filter)
        self.scene_combo.currentIndexChanged.connect(self._filter)
        self.starred_only.stateChanged.connect(self._filter)
        self.select_button.clicked.connect(self.table.selectAll)
        self.delete_button.clicked.connect(self._delete)
        self.table.cellDoubleClicked.connect(self._open)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.set_tasks([])

    def _toggle(self):
        expanded = not self.body.isHidden()
        self.body.setVisible(not expanded)
        self.toggle_button.setText('展开任务库' if expanded else '收起任务库')

    def set_tasks(self, tasks):
        self._tasks = list(tasks)
        selected = self.scene_combo.currentData()
        self.scene_combo.blockSignals(True)
        try:
            self.scene_combo.clear()
            self.scene_combo.addItem('全部场景', userData=None)
            for scene in sorted({task.scenario for task in self._tasks if task.scenario}):
                self.scene_combo.addItem(scene, userData=scene)
            index = self.scene_combo.findData(selected)
            self.scene_combo.setCurrentIndex(max(0, index))
        finally:
            self.scene_combo.blockSignals(False)
        self._filter()

    def refresh_tasks(self, tasks):
        """增量刷新任务记录，保留筛选和多选；停止状态不能停留在旧缓存里。"""
        selected = {task.task_id for task in self.selected_tasks()}
        previous = [task.task_id for task in self._visible]
        scenes = {task.scenario for task in self._tasks if task.scenario}
        self._tasks = list(tasks)
        visible = self._filtered_tasks()
        if ([task.task_id for task in visible] == previous
                and scenes == {task.scenario for task in self._tasks if task.scenario}):
            self._visible = visible
            for row, task in enumerate(visible):
                for column, text in enumerate(self._row_texts(task)):
                    item = self.table.item(row, column)
                    if item.text() != text:
                        item.setText(text)
                        item.setToolTip(text)
            self.count_label.setText(f'{len(self._visible)} / {len(self._tasks)} 个任务；双击查看分析')
            self._selection_changed()
            return
        self.set_tasks(self._tasks)
        selection = self.table.selectionModel()
        for row, task in enumerate(self._visible):
            if task.task_id in selected:
                selection.select(self.table.model().index(row, 0),
                                 QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self._selection_changed()

    def _filtered_tasks(self):
        query = self.search_edit.text().strip().casefold()
        scene = self.scene_combo.currentData()
        visible = [task for task in self._tasks
                   if (not scene or task.scenario == scene)
                   and (not self.starred_only.isChecked() or task.starred)
                   and (not query or query in ' '.join((task.process_name, task.note,
                        task.scenario, task.build, str(task.pid), task.task_id)).casefold())]
        visible.sort(key=lambda task: (not task.starred, task.process_name.casefold()))
        return visible

    @staticmethod
    def _row_texts(task):
        identity = task.process_name + ((' · ' + task.note) if task.note else '')
        return (identity, task.scenario or '—', task.build or '—',
                '已标星' if task.starred else '—', '参考' if task.is_reference else '—',
                '已结束' if task.status == 'stopped' else '运行中')

    def _filter(self, *_args):
        self.table.clearSelection()
        self._visible = self._filtered_tasks()
        self.table.setRowCount(len(self._visible))
        for row, task in enumerate(self._visible):
            for column, text in enumerate(self._row_texts(task)):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                item.setData(Qt.UserRole, task.task_id)
                self.table.setItem(row, column, item)
        self.count_label.setText(f'{len(self._visible)} / {len(self._tasks)} 个任务；双击查看分析')
        self._selection_changed()

    def selected_tasks(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self._visible[row] for row in rows if row < len(self._visible)]

    def _selection_changed(self):
        self.delete_button.setEnabled(bool(self.selected_tasks()) and self._worker is None
                                      and not getattr(self._manager.db, 'read_only', False))

    def _open(self, row, _column):
        if 0 <= row < len(self._visible):
            self.task_selected.emit(self._visible[row].task_id)

    def _delete(self):
        tasks = self.selected_tasks()
        if not tasks or self._worker is not None:
            return
        if any(task.status != 'stopped' for task in tasks):
            self._notice('warning', '不能删除', '所选任务包含运行中任务，请先停止监控')
            return
        dialogs = self._services.dialogs
        if not dialogs.confirm(self.window(), '删除所选任务',
                               f'将永久删除 {len(tasks)} 个任务的全部采样数据和打点，无法撤销。', '删除'):
            return
        references = [task for task in tasks if task.is_reference]
        if references and not dialogs.confirm(self.window(), '删除场景参考',
                '以下场景将失去参考：' + '、'.join(task.scenario for task in references), '仍然删除参考'):
            return
        try:
            worker = self._manager.start_task_deletion(
                [task.task_id for task in tasks], self._services.threads,
                allow_references=bool(references),
                on_result=lambda count: self._notice('success', '已删除任务', f'{count} 个任务'),
                on_failed=lambda message: self._notice('error', '删除失败', message),
                on_finished=self._finished)
        except (ValueError, StorageError) as exc:
            self._notice('warning', '未能删除', str(exc))
            return
        self._worker = worker
        self._selection_changed()

    def _finished(self):
        self._worker = None
        self.changed.emit()
        self._selection_changed()

    def _notice(self, level, title, content):
        self._services.notifier.route('task_library', level, title, content, anchor=self)
