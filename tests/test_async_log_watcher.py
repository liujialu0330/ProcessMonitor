"""慢网络日志不能阻塞界面，关闭或暂停后的在途结果不得触发打点。"""
import threading

from core.automation.log_watcher import AsyncLogWatcher
from tests.helpers import poll_until


def test_blocked_initial_stat_does_not_block_owner_and_close_discards_result():
    entered, release = threading.Event(), threading.Event()

    def factory(*args):
        entered.set()
        release.wait(5)
        return type('Reader', (), {'poll': lambda self: ['late']})()

    watcher = AsyncLogWatcher('network', factory=factory)
    try:
        assert entered.wait(2)
        assert watcher.poll() == []
        watcher.close()
    finally:
        release.set()
    assert poll_until(lambda: not watcher._busy, timeout=2)
    assert watcher.poll() == []


def test_pause_and_resume_invalidates_inflight_batch():
    entered, release = threading.Event(), threading.Event()

    class Reader:
        def poll(self):
            entered.set()
            release.wait(5)
            return ['stale']

    watcher = AsyncLogWatcher(factory=lambda: Reader())
    try:
        assert poll_until(lambda: not watcher._busy, timeout=2)
        watcher.poll()
        assert entered.wait(2)
        watcher.set_paused(True)
        watcher.set_paused(False)
        release.set()
        assert poll_until(lambda: not watcher._busy, timeout=2)
        assert watcher.poll() == []
    finally:
        watcher.close()
        release.set()
