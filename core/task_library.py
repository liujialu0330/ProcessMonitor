"""任务后台删除。目标锁或全局维护锁由管理器占用，线程只执行可取消的数据事务。"""
from core.workers.base import BackgroundTask


class TaskDeletionWorker(BackgroundTask):
    def __init__(self, repo, task_ids, *, allow_references=False, parent=None):
        super().__init__(parent)
        self._repo = repo
        self.task_ids = tuple(task_ids)
        self._allow_references = allow_references

    def execute(self, token):
        return self._repo.delete_many_stopped(
            self.task_ids, allow_references=self._allow_references,
            cancelled=lambda: token.is_cancelled)
