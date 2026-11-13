"""参考列与任务库增量刷新保持用户当前视图状态。"""
from datetime import timedelta

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from core.monitor_manager import MonitorManager
from tests.factories import make_services
from tests.helpers import SyncHistoryRunner, dispose_widget
from tests.test_analysis_vm import BASE, HANDLES, MEM, seed
from ui.pages.analysis_page import AnalysisPage
from ui.widgets.analysis.summary_table import SUMMARY_COLUMNS


def test_reference_column_survives_keyboard_metric_selection(qapp, db):
    manager, services = MonitorManager(db), make_services()
    ref = seed(db, start=BASE - timedelta(days=1), slope=1)
    current = seed(db, slope=10)
    for task in (ref, current):
        db.tasks.update_identity(task.task_id, '窗口操作', '2.0')
        for index, second in enumerate((0, 30, 60, 90)):
            db.markers.add(task.task_id, task.start_time + timedelta(seconds=second),
                           '基线' if index == 0 else f'第{index}轮',
                           kind='baseline' if index == 0 else 'user')
    db.tasks.set_reference(ref.task_id)
    runner = SyncHistoryRunner(db)
    page = AnalysisPage(db, manager, services, worker_runner=runner, defer=lambda fn: fn())
    try:
        page.show()
        page.vm.reload_tasks()
        column = len(SUMMARY_COLUMNS)
        table = page.summary_table.table
        assert not table.isColumnHidden(column)
        assert '回归' in table.item(0, column).text()
        count = len(runner.requests)
        table.setFocus()
        table.setCurrentCell(0, 0)
        QTest.keyClick(table, Qt.Key_Down)
        assert page.current_metric == HANDLES
        assert not table.isColumnHidden(column)
        assert page.summary_table._reference is page.vm.last_reference
        assert len(runner.requests) == count
        # 切换到无参考关系的任务时仍应清理上一个任务的结果。
        other = seed(db, start=BASE + timedelta(days=1), metrics=(MEM,))
        page.vm.reload_tasks()
        page.vm.select_task(other.task_id, True)
        assert table.isColumnHidden(column)
    finally:
        services.threads.cancel_all()
        services.threads.shutdown_all()
        dispose_widget(page)


def test_library_updates_stopped_tasks_without_losing_filter_or_selection(qapp, db):
    manager, services = MonitorManager(db), make_services()
    current = seed(db)
    others = [seed(db, name=f'other-{i}.exe', start=BASE - timedelta(days=i + 1))
              for i in range(2)]
    for task in others:
        db.tasks.update_status(task.task_id, 'running')
        services.task_status.set_running(task.task_id)
    page = AnalysisPage(db, manager, services, worker_runner=SyncHistoryRunner(db),
                        defer=lambda fn: fn())
    try:
        page.vm.reload_tasks()
        page.vm.select_task(current.task_id, True)
        library = page.task_library
        library.search_edit.setText('other-')
        library.select_button.click()
        selected = {task.task_id for task in library.selected_tasks()}
        assert selected == {task.task_id for task in others}
        for task in others:
            db.tasks.update_status(task.task_id, 'stopped')
            services.task_status.set_running(task.task_id, False)
        page.vm.on_tick()
        assert library.search_edit.text() == 'other-'
        assert {task.task_id for task in library.selected_tasks()} == selected
        assert all(task.status == 'stopped' for task in library.selected_tasks())
        assert [library.table.item(row, 5).text() for row in range(2)] == ['已结束', '已结束']
        assert library.delete_button.isEnabled()
    finally:
        services.threads.cancel_all()
        services.threads.shutdown_all()
        dispose_widget(page)
