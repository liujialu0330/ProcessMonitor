"""GUI 按需提权的组装与可撤销任务交接。"""
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication

from core.restart_handoff import READY_TIMEOUT_S, RESTART_ARG, RestartTicket
from utils.elevation import (
    ElevationCancelled, WindowsProcess, is_elevated, launch_elevated,
)

logger = logging.getLogger(__name__)
PREPARE_TIMEOUT_S = 30.0


def restart_command(directory: str, token: str) -> list:
    if getattr(sys, 'frozen', False):
        command = [sys.executable]
    else:
        executable = Path(sys.executable)
        windowed = executable.with_name('pythonw.exe')
        command = [str(windowed if windowed.exists() else executable),
                   str(Path(__file__).resolve().parents[1] / 'main.py')]
    return command + ['--data-dir', directory, RESTART_ARG, token]


class _LaunchWorker(QThread):
    launched = pyqtSignal(object)
    failed = pyqtSignal(str, bool)

    def __init__(self, ticket: RestartTicket, owner: int, launcher: Callable):
        super().__init__()
        self.ticket = ticket
        self.owner = owner
        self.launcher = launcher
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        try:
            self.ticket.cancel()
        except (OSError, ValueError):
            logger.warning('取消管理员启动交接失败', exc_info=True)

    def run(self) -> None:
        try:
            process = self.launcher(
                restart_command(self.ticket.directory, self.ticket.token),
                cwd=str(Path(__file__).resolve().parents[1]), owner=self.owner)
            if self._cancelled.is_set():
                process.close()
            else:
                self.launched.emit(process)
        except ElevationCancelled as exc:
            self.failed.emit(str(exc), True)
        except Exception as exc:
            logger.warning('启动管理员实例失败', exc_info=True)
            self.failed.emit(str(exc), False)


class ElevationController(QObject):
    def __init__(self, ctx, window, guard, *, launcher=launch_elevated,
                 process_factory=WindowsProcess.open):
        super().__init__(window)
        self.ctx = ctx
        self.window = window
        self.dispatcher = getattr(guard, 'dispatcher', None)
        self.launcher = launcher
        self.process_factory = process_factory
        self._ticket = None
        self._process = None
        self._child_identity = None
        self._worker = None
        self._workers = []
        self._frozen = False
        self._committed = False
        self._deadline = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        QApplication.instance().aboutToQuit.connect(self._on_quit)

    def _blocked_reason(self) -> str:
        manager = self.ctx.manager
        if (manager.is_maintenance_running() or manager.has_running_exports()
                or manager.has_storage_jobs() or manager.has_task_deletions()):
            return '请等待当前数据维护或导出操作结束，再以管理员身份重启。'
        if self.dispatcher is not None and self.dispatcher.has_pending():
            return '命令行还有进行中的等待或导出请求，请先完成或取消该请求再重启。'
        return ''

    def request(self) -> None:
        if self._ticket is not None or self._worker is not None:
            return
        if is_elevated():
            self.ctx.services.notifier.info(
                self.window, '已以管理员身份运行', '请检查受限指标的具体失败原因。')
            return
        reason = self._blocked_reason()
        if reason:
            self.ctx.services.notifier.warning(self.window, '暂时无法重启', reason)
            return
        try:
            with self.process_factory(os.getpid()) as current:
                self._ticket = RestartTicket.create(
                    self.ctx.paths.data_dir, current.identity().to_dict())
            worker = _LaunchWorker(self._ticket, int(self.window.winId()), self.launcher)
            self._worker = worker
            worker.launched.connect(self._on_launched)
            worker.failed.connect(self._abort)
            worker.finished.connect(lambda: setattr(self, '_worker', None))
            self.ctx.registry.register(worker, name='管理员授权', cancel=worker.cancel)
            worker.start()
        except Exception as exc:
            self._abort(str(exc))

    def _on_launched(self, process) -> None:
        self._process = process
        try:
            parent = self._ticket.read()['parent']
            self._child_identity = process.identity().to_dict()
            if self._child_identity['user_sid'] != parent['user_sid']:
                raise ValueError('请使用当前 Windows 用户的管理员授权')
            if not process.is_elevated():
                raise ValueError('新实例未取得管理员权限')
            self._deadline = time.monotonic() + READY_TIMEOUT_S
            self._timer.start()
        except Exception as exc:
            self._abort(str(exc))

    def _freeze(self) -> None:
        reason = self._blocked_reason()
        if reason:
            raise RuntimeError(reason)
        self._frozen = True
        self.window.setEnabled(False)
        self.window.monitor_page.presenter.suspend_for_restart(True)
        if self.dispatcher is not None:
            self.dispatcher.suspend_for_restart(True)
        if self.ctx.storage_guard is not None:
            self.ctx.storage_guard.stop()
        self.ctx.manager.begin_shutdown()
        workers = self.ctx.manager.get_all_tasks()
        self._workers = [worker.task_id for worker in workers]
        for worker in workers:
            worker.prepare_handoff()
        self._deadline = time.monotonic() + PREPARE_TIMEOUT_S

    def _poll(self) -> None:
        try:
            if not self._process.alive():
                raise RuntimeError('管理员实例在完成交接前退出')
            if time.monotonic() >= self._deadline:
                raise TimeoutError('管理员重启准备超时，当前任务继续监控')
            if not self._frozen:
                if self._ticket.ready(self._child_identity):
                    self._freeze()
                return
            active = []
            for task_id in self._workers:
                worker = self.ctx.manager.get_task(task_id)
                if worker is None or worker.isFinished():
                    continue
                ready, error = worker.handoff_status()
                if error:
                    raise RuntimeError(error)
                if not ready:
                    return
                active.append(worker)
            manager = self.ctx.manager
            if not manager.prepare_restart_writes(deadline=self._deadline):
                return
            payload = {
                'tasks': {worker.task_id: worker.handoff_state() for worker in active},
                'ui': self.window.monitor_page.presenter.restart_ui_state(),
                'ipc_launches': self.dispatcher.restart_launches()
                if self.dispatcher is not None else {}}
            self._ticket.commit(self._child_identity, payload)
            # 提交后即进入接管阶段，后续异常由新实例依据已落库记录恢复。
            self._committed = True
            for worker in active:
                worker.commit_handoff()
            self._timer.stop()
            self._process.close()
            self._process = None
            self.window.finish_elevated_restart()
        except Exception as exc:
            logger.warning('管理员重启交接未完成', exc_info=True)
            if self._committed:
                logger.error('交接已提交，结束旧实例后由新实例恢复', exc_info=True)
                self._timer.stop()
                QApplication.quit()
                return
            self._abort(str(exc))

    def _abort(self, message: str, cancelled: bool = False) -> None:
        self._timer.stop()
        if self._ticket is not None:
            try:
                self._ticket.cancel()
            except (OSError, ValueError):
                logger.warning('写入取消交接状态失败', exc_info=True)
        if self._process is not None:
            self._process.close()
            self._process = None
        if self._frozen:
            self.ctx.manager.cancel_restart_preparation()
            self.window.monitor_page.presenter.suspend_for_restart(False)
            if self.dispatcher is not None:
                self.dispatcher.suspend_for_restart(False)
            if self.ctx.storage_guard is not None:
                self.ctx.storage_guard.start()
            self.window.setEnabled(True)
        self._ticket = None
        self._workers = []
        self._frozen = False
        notify = (self.ctx.services.notifier.info if cancelled
                  else self.ctx.services.notifier.error)
        notify(self.window, '已取消管理员重启' if cancelled else '管理员重启未完成', message)

    def _on_quit(self) -> None:
        if not self._committed and self._ticket is not None:
            if self._worker is not None:
                self._worker.cancel()
            else:
                try:
                    self._ticket.cancel()
                except (OSError, ValueError):
                    logger.warning('退出时取消管理员交接失败', exc_info=True)
        if self._process is not None:
            self._process.close()
            self._process = None


def restore_handoff(ctx, window, guard) -> None:
    from app.bootstrap import run_recovery
    state = ctx.restart_state
    try:
        presenter = window.monitor_page.presenter
        outcome = run_recovery(ctx, auto_marker=presenter.auto_marker, resume_all=True)
        presenter.adopt_recovered_plans()
        presenter.restore_restart_ui(state['payload']['ui'])
        dispatcher = getattr(guard, 'dispatcher', None)
        if dispatcher is not None:
            dispatcher.restore_restart_launches(state['payload'].get('ipc_launches', {}))
        window.show_monitor_page()
        if outcome is not None and outcome.failed:
            ctx.services.notifier.warning(
                window, '部分任务未能续采', '目标进程或任务状态已变化，请在分析页查看已保存数据。')
        RestartTicket(ctx.paths.data_dir, state['token']).cleanup()
        ctx.restart_state = None
        logger.info('管理员重启交接已恢复')
    except Exception:
        logger.error('恢复管理员重启交接失败，保留交接记录供下次启动', exc_info=True)
        ctx.services.notifier.error(
            window, '重启状态恢复未完成', '已保存数据保留，请查看日志中的具体原因。')
