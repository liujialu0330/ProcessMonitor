"""测试方案的界面流程：模板只保存参数，启动仍走监控页的统一校验与授权。"""
import os
import uuid

from PyQt5.QtCore import QObject
from qfluentwidgets import Action, ComboBox, FluentIcon, LineEdit, PushButton, RoundMenu
from core.automation.plan import AutomationPlan
from core.plans import PlanStore, PlanTarget, TestPlan, execute_plan, read_plan
from data.models import TARGET_NAME
from ui.widgets.monitor.new_task_form import TaskRequest, WaitRequest
from ui.widgets.monitor.process_picker import ResolveError


class PlanFlow(QObject):
    def __init__(self, db, presenter, form, services, parent=None, *, store=None):
        super().__init__(parent)
        self.presenter, self.form, self.services = presenter, form, services
        self.store = store or PlanStore(os.path.dirname(os.path.abspath(db.db_path)))
        self.plans = {}
        self._pending_groups = {}
        self.combo = ComboBox(form)
        self.combo.setMinimumWidth(140)
        self.combo.setMaximumWidth(200)
        self.combo.setPlaceholderText('选择测试方案')
        self.combo.currentTextChanged.connect(self.select)
        self.name = LineEdit(form)
        self.name.setPlaceholderText('方案名称')
        self.name.setMaxLength(120)
        self.name.setMaximumWidth(150)
        self.button = PushButton('方案', form, FluentIcon.DOCUMENT)
        self.button.clicked.connect(self.show_menu)
        for widget in (self.combo, self.name, self.button):
            form.title_row.addWidget(widget)
        presenter.wait_service.started.connect(self._wait_started)
        presenter.wait_service.failed.connect(lambda wid, _reason: self._pending_groups.pop(wid, None))
        self.refresh()

    def refresh(self, selected=''):
        self.plans = {plan.name: plan for plan in self.store.list()}
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(list(self.plans))
        self.combo.setCurrentIndex(-1)
        if selected in self.plans:
            self.combo.setCurrentText(selected)
        self.combo.blockSignals(False)

    def show_menu(self):
        menu = RoundMenu(parent=self.form)
        for label, callback in (
            ('运行选中方案', self.run_selected), ('另存当前配置', self.save_current),
            ('把当前目标追加到选中方案', lambda: self.save_current(append=True)),
            ('导入方案…', self.import_plan), ('导出选中方案…', self.export_plan),
        ):
            action = Action(label, menu)
            action.triggered.connect(lambda _checked=False, cb=callback: self._guard(cb))
            menu.addAction(action)
        menu.exec(self.button.mapToGlobal(self.button.rect().bottomLeft()))
        menu.deleteLater()

    def _guard(self, callback):
        try:
            return callback()
        except (ValueError, OSError) as exc:
            self.services.notifier.error(self.form, '方案操作失败', str(exc))

    def current_target(self):
        form = self.form
        plan = form.automation_plan() or AutomationPlan()
        data = dict(name='', metrics=list(form.metrics()), interval=form.interval(), note=form.note(),
                    scenario=form.scenario_edit.text().strip(), build=form.build_edit.text().strip(),
                    target_kind=form.target_kind(), automation=plan.to_dict(),
                    wait_timeout_s=form.wait_timeout_s())
        if form.launch_enabled():
            spec, error = form.launch_options.spec()
            if spec is None:
                raise ValueError(error)
            data.update(launch=spec.to_dict(), close_on_stop=form.launch_options.close_on_stop())
        elif form.wait_enabled() or form.target_kind() == TARGET_NAME:
            name = form.picker.resolve_name()
            if isinstance(name, ResolveError):
                raise ValueError('请填写被测进程名称')
            data.update(name=name, wait=form.wait_enabled())
        else:
            target = form.picker.resolve()
            if isinstance(target, ResolveError):
                raise ValueError('请先选择被测进程，或设置等待启动/启动程序')
            if form.target_kind() == 'tree':
                data.update(pid=target.pid)
            else:
                data.update(name=target.name)
        return PlanTarget.from_dict(data)

    def save_current(self, append=False):
        selected = self.plans.get(self.combo.currentText())
        name = selected.name if append and selected else self.name.text().strip()
        if not name:
            raise ValueError('请在方案名称框中填写名称')
        target = self.current_target()
        targets = (*selected.targets, target) if append and selected else (target,)
        plan = TestPlan.from_dict(TestPlan(name, targets,
                                         selected.shared_markers if append and selected else True).to_dict())
        exists = name in self.plans
        if exists and not append and not self.services.dialogs.confirm(
                self.form, '覆盖测试方案', f'同名方案“{name}”已存在，是否替换其配置？', '覆盖'):
            return None
        self.store.save(plan, overwrite=exists)
        self.refresh(name)
        self.services.notifier.success(self.form, '方案已保存', f'{name} · {len(plan.targets)} 个目标')
        return plan

    def select(self, name):
        plan = self.plans.get(name)
        if plan is None:
            return
        self.name.setText(name)
        target = plan.targets[0]
        form = self.form
        form.set_collapsed(False)
        form.set_target_kind(target.target_kind)
        form.set_metrics_summary(target.metrics or self.services.preferences.last_metrics())
        form.interval_spinbox.setValue(int(target.interval))
        form.note_input.setText(target.note)
        form.scenario_edit.setText(target.scenario)
        form.build_edit.setText(target.build)
        form.set_launch_spec(target.launch, target.close_on_stop)
        form.set_automation_plan(target.automation)
        form.set_wait_enabled(target.wait)
        form.set_wait_timeout_s(target.wait_timeout_s)
        form.fill_search(str(target.pid) if target.pid else target.name)
        self.button.setToolTip(f'{len(plan.targets)} 个目标；“运行选中方案”将逐个开始，失败项单独提示')

    def run_selected(self):
        plan = self.plans.get(self.combo.currentText())
        if plan is None:
            raise ValueError('请先选择测试方案')
        group_id = str(uuid.uuid4()) if plan.shared_markers else None
        results = execute_plan(plan, lambda target, index: self._start(target, index, group_id))
        errors = [f'{r.index}. {r.target.name or r.target.pid}：{r.error}' for r in results if r.error]
        if errors:
            self.services.notifier.warning(self.form, '方案部分目标未开始', '\n'.join(errors))
        else:
            self.services.notifier.success(self.form, '方案已执行', f'{len(results)} 个目标已开始或进入等待')
        return results

    def _start(self, target, index, group_id):
        args = target.start_args(index=index, now=self.services.clock.now())
        metrics = target.metrics or tuple(self.services.preferences.last_metrics())
        if target.wait and target.target_kind != TARGET_NAME:
            started = []
            record = lambda wid, tid: started.append((wid, tid))
            self.presenter.wait_service.started.connect(record)
            try:
                wid = self.presenter.on_wait_requested(WaitRequest(
                    target.name, metrics, target.interval, args['note'], target.automation,
                    target.target_kind, target.scenario, target.build, target.wait_timeout_s))
            finally:
                self.presenter.wait_service.started.disconnect(record)
            immediate = next((tid for wait_id, tid in started if wait_id == wid), None)
            pending = wid is not None and wid in self.presenter.wait_ids()
            if not immediate and not pending:
                return None
            if group_id:
                if immediate:
                    self.presenter.manager.set_task_group([immediate], group_id)
                else:
                    self._pending_groups[wid] = group_id
            return immediate or wid
        pid, name, created = target.pid or 0, target.name, None
        if target.pid is not None and target.launch is None:
            name = self.services.process_query.name_of(target.pid)
            if not name:
                raise ValueError(f'PID {target.pid} 已不存在或不可访问')
            created = next((ct for p, ct in self.services.process_query.find_pids_by_name(name)
                            if p == target.pid), None)
        if target.launch is None and target.target_kind != TARGET_NAME and target.pid is None:
            found = self.presenter.wait_service.find(target.name)
            if found is None:
                raise ValueError('进程尚未运行，请配置等待启动')
            pid, name, created = found
        tid = self.presenter.start_monitoring(TaskRequest(
            pid, name, created, metrics, target.interval, args['note'], target.automation,
            target.launch, target.close_on_stop, target.target_kind, target.scenario, target.build))
        if tid and group_id:
            self.presenter.manager.set_task_group([tid], group_id)
        return tid

    def _wait_started(self, wait_id, task_id):
        group_id = self._pending_groups.pop(wait_id, None)
        if group_id:
            self.presenter.manager.set_task_group([task_id], group_id)

    def import_plan(self):
        path = self.services.dialogs.ask_open_file(self.form, '导入测试方案', '', '测试方案 (*.json)')
        if not path:
            return
        plan = read_plan(path)
        if plan.name in self.plans and not self.services.dialogs.confirm(
                self.form, '覆盖测试方案', f'已有“{plan.name}”，是否替换？', '覆盖'):
            return
        self.store.save(plan, overwrite=plan.name in self.plans)
        self.refresh(plan.name)
        self.select(plan.name)

    def export_plan(self):
        plan = self.plans.get(self.combo.currentText())
        if plan is None:
            raise ValueError('请先选择测试方案')
        path = self.services.dialogs.ask_save_path(
            self.form, '导出测试方案', str(self.store.path_for(plan.name)), '测试方案 (*.json)')
        if path:
            self.store.export_file(plan, path)
