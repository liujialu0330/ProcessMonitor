"""
core/workers/base.py：取消令牌、BackgroundTask 的结果/失败/取消语义、LatestRequestGate。
"""
import logging
import threading

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from core.workers.base import BackgroundTask, CancelToken, LatestRequestGate, TaskError


# ---------- CancelToken ----------

def test_cancel_token_starts_uncancelled_and_is_idempotent():
    token = CancelToken()
    assert token.is_cancelled is False
    token.cancel()
    token.cancel()
    assert token.is_cancelled is True


def test_cancel_token_visible_across_threads():
    token = CancelToken()
    seen = []

    def worker():
        while not token.is_cancelled:
            pass
        seen.append(True)

    t = threading.Thread(target=worker)
    t.start()
    token.cancel()
    t.join(5)
    assert seen == [True]


# ---------- BackgroundTask ----------

class _Task(BackgroundTask):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def execute(self, token):
        return self.fn(token)


def _collect(task):
    got = {'result': [], 'failed': []}
    task.result.connect(got['result'].append, Qt.DirectConnection)
    task.failed.connect(got['failed'].append, Qt.DirectConnection)
    return got


def test_result_emitted_with_return_value(qapp):
    task = _Task(lambda token: {"n": 1})
    got = _collect(task)
    task.run()
    assert got == {'result': [{"n": 1}], 'failed': []}


def test_exception_logged_and_reported_via_failed(qapp, caplog):
    def boom(token):
        raise RuntimeError("磁盘已满")

    task = _Task(boom)
    got = _collect(task)
    with caplog.at_level(logging.ERROR, logger="core.workers.base"):
        task.run()
    assert got == {'result': [], 'failed': ["磁盘已满"]}
    assert any(r.exc_info for r in caplog.records)


def test_exception_without_message_uses_type_name(qapp):
    def boom(token):
        raise KeyError()

    task = _Task(boom)
    got = _collect(task)
    task.run()
    assert got['failed'] == ["KeyError"]


def test_task_error_message_passed_through_without_traceback_log(qapp, caplog):
    def expected(token):
        raise TaskError("仓库暂无发布版本")

    task = _Task(expected)
    got = _collect(task)
    with caplog.at_level(logging.ERROR, logger="core.workers.base"):
        task.run()
    assert got['failed'] == ["仓库暂无发布版本"]
    assert not caplog.records


def test_cancelled_task_emits_no_result(qapp):
    def cancel_midway(token):
        task.cancel()
        return "done"

    task = _Task(cancel_midway)
    got = _collect(task)
    task.run()
    assert task.is_cancelled
    assert got == {'result': [], 'failed': []}


def test_cancelled_task_exception_emits_no_failed(qapp):
    def cancel_then_raise(token):
        task.cancel()
        raise InterruptedError("用户取消")

    task = _Task(cancel_then_raise)
    got = _collect(task)
    task.run()
    assert got == {'result': [], 'failed': []}


def test_execute_receives_own_token(qapp):
    seen = []
    task = _Task(lambda token: seen.append(token))
    task.run()
    assert seen == [task.token]


def test_handler_error_does_not_escape_run(qapp, caplog):
    """收尾钩子出错只记日志，不会从 run() 抛出（工作线程里抛出会直接终止进程）"""
    class _BadHandler(_Task):
        def handle_result(self, value):
            raise ValueError("收尾出错")

    task = _BadHandler(lambda token: 1)
    with caplog.at_level(logging.ERROR, logger="core.workers.base"):
        task.run()
    assert any("结果收尾出错" in r.getMessage() for r in caplog.records)


def test_thread_run_delivers_result_and_finishes(qapp):
    task = _Task(lambda token: threading.current_thread() is threading.main_thread())
    results = []
    task.result.connect(results.append)
    task.start()
    assert task.wait(5000)
    QApplication.processEvents()
    assert results == [False]


def test_request_seq_defaults_to_none_and_is_stored(qapp):
    assert _Task(lambda t: None).request_seq is None
    task = BackgroundTask(request_seq=5)
    assert task.request_seq == 5


def test_base_execute_not_implemented(qapp):
    task = BackgroundTask()
    got = _collect(task)
    task.run()
    assert got['result'] == [] and len(got['failed']) == 1


# ---------- LatestRequestGate ----------

def test_gate_latest_request_wins():
    gate = LatestRequestGate()
    assert gate.current == 0
    first = gate.next_seq()
    second = gate.next_seq()
    assert second > first
    assert gate.is_current(second) is True
    assert gate.is_current(first) is False


def test_gate_invalidate_expires_in_flight_requests():
    gate = LatestRequestGate()
    seq = gate.next_seq()
    gate.invalidate()
    assert gate.is_current(seq) is False
    assert gate.is_current(gate.next_seq()) is True


def test_gate_none_is_never_current():
    gate = LatestRequestGate()
    assert gate.is_current(None) is False
    assert gate.is_current(0) is True    # 尚未发起过请求时 0 即当前（与旧序号初值一致）
