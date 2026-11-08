"""测试辅助：轮询等待，替代固定 time.sleep 的时序断言"""
import time


def poll_until(condition, timeout: float = 10.0, interval: float = 0.002) -> bool:
    """轮询等待 condition() 为真，最长 timeout 秒；超时返回最后一次判定结果"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


def wait_stable(getter, quiet: float = 0.4, timeout: float = 10.0) -> bool:
    """等待 getter() 的值连续 quiet 秒不变（用于确认"已不再产生新数据"）"""
    deadline = time.monotonic() + timeout
    last = getter()
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.02)
        current = getter()
        if current != last:
            last = current
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= quiet:
            return True
    return False


def stop_and_join(manager, timeout_ms: int = 5000) -> bool:
    """停止 manager 的全部任务并带超时等待线程结束（stop_all_tasks 本身不再等待）。
    用例收尾与需要"任务已全部收尾落库"的断言前调用。"""
    manager.stop_all_tasks()
    return manager.join_all(timeout_ms=timeout_ms)


def process_events_until(condition, timeout: float = 5.0) -> bool:
    """一边处理 Qt 事件一边等 condition() 为真（队列信号与 deleteLater 需要事件循环）"""
    from PyQt5.QtCore import QCoreApplication, QEvent

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if condition():
            return True
        time.sleep(0.002)
    QCoreApplication.processEvents()
    return bool(condition())


def dispose_widget(widget) -> None:
    """隐藏并立即释放顶层控件（用例收尾用）。

    无父对象的顶层窗口不会随 Python 引用消失而析构；qfluentwidgets 的全局
    styleSheetManager 又登记着其中每个控件，残留窗口会让之后每次主题切换都给
    它们重刷样式表（残留十几个主窗口时，冒烟用例的两次切主题要几十秒）。
    """
    from PyQt5.QtCore import QCoreApplication, QEvent

    widget.hide()
    release_plot_menus(widget)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def release_plot_menus(widget) -> None:
    """释放控件内全部 pyqtgraph 图表的 PlotItem.ctrlMenu（约 30 个无父 QMenu）。

    pyqtgraph 的 PlotItem 构造时无条件建这个菜单，只在 PlotItem.close() 里释放；控件被删后
    它们仍作为无父顶层控件留在应用里，全量里积到上千个，拖慢每个用例的 topLevelWidgets()。
    只做 close() 里释放菜单的那两行（不动 ViewBox 与坐标轴），只在控件即将被删除时调用。
    """
    try:
        import pyqtgraph as pg
        for plot in widget.findChildren(pg.PlotWidget):
            item = plot.getPlotItem()
            menu = getattr(item, 'ctrlMenu', None)
            if menu is not None:
                menu.setParent(None)
                item.ctrlMenu = None
    except Exception:   # 释放失败只是多留一些菜单，不应让用例收尾失败
        pass


def record_stop_reasons(task) -> list:
    """直连记录 task_stopped 的停止原因（StopReason），在发射线程内同步追加，
    适用于 MonitorTask 与 MonitorManager"""
    from PyQt5.QtCore import Qt

    reasons = []
    task.task_stopped.connect(
        lambda _tid, reason: reasons.append(reason), Qt.DirectConnection)
    return reasons


class ConnectionFactoryStub:
    """
    与 data.db.connection.ConnectionFactory 同接口的测试桩（db_path / connect() / transaction()），
    两个上下文管理器照搬 Database._get_connection / Database._transaction，
    供仓储单元测试在连接工厂就绪前独立驱动仓储。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _open(self, **kwargs):
        import sqlite3
        conn = sqlite3.connect(self.db_path, **kwargs)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _pragmas(conn):
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=NORMAL')

    def connect(self):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            conn = self._open()
            try:
                self._pragmas(conn)
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return _cm()

    def transaction(self):
        import sqlite3
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            conn = self._open(isolation_level=None)
            try:
                self._pragmas(conn)
                conn.execute('BEGIN IMMEDIATE')
                try:
                    yield conn.cursor()
                    conn.execute('COMMIT')
                except BaseException:
                    try:
                        conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
            finally:
                conn.close()
        return _cm()


def make_current_db(db_path: str) -> str:
    """建一个当前结构（data.db.schema.SCHEMA_VERSION）的临时库并返回路径。"""
    from data.database import Database

    Database(db_path).close()
    return db_path


# 过时：旧名，库结构已不止 v4；新用例请用 make_current_db
make_v4_db = make_current_db


def make_broken_factories(tmp_dir: str) -> list:
    """
    两个必然触发 sqlite3.Error 的连接工厂桩：库所在目录不存在（打不开）、库文件已损坏
    （不是 SQLite 文件），用于验证仓储把 sqlite3.Error 包成 StorageError。
    """
    import os

    corrupt = os.path.join(tmp_dir, 'corrupt.db')
    with open(corrupt, 'wb') as f:
        f.write(b'this is not a sqlite database file' * 64)
    return [
        ConnectionFactoryStub(os.path.join(tmp_dir, 'no_such_dir', 'x.db')),
        ConnectionFactoryStub(corrupt),
    ]


def fetch_history_render_inputs(db, task_id: str, metric_type: str, since, table_limit: int,
                                max_buckets: int):
    """
    按历史页的四类查询取数（明细最近 table_limit 条、统计、分桶、打点），返回
    (table_points, stats, chart_points, markers)，供历史页显示控件的渲染测试直接喂数据。
    口径与历史页一致；只用数据层仓储，不依赖页面或查询编排模块。
    """
    from data.repos.point_repo import PointQuery

    table_points = db.points.fetch(
        PointQuery(task_id, metric_type, since=since, limit=table_limit))
    stats = db.analytics.legacy_stats(task_id, metric_type, since=since)
    chart_points = db.analytics.legacy_buckets(task_id, metric_type, max_buckets, since=since)
    markers = db.markers.list(task_id, since=since)
    return table_points, stats, chart_points, markers


class SyncHistoryRunner:
    """历史查询执行器替身：在当前线程同步执行 run_history_query 并立即回调。
    requests 记下每次收到的请求（按顺序），threads 记下执行线程。"""

    def __init__(self, db):
        self.db = db
        self.requests = []
        self.threads = []

    def __call__(self, request, on_done):
        import threading
        from core.history_query import run_history_query

        self.requests.append(request)
        self.threads.append(threading.current_thread())
        on_done(run_history_query(self.db, request))


class ManualHistoryRunner:
    """历史查询执行器替身：只记下请求，测试调用 complete()/fail() 时才回调，
    用于模拟"查询在途"、结果乱序到达与过期丢弃。"""

    def __init__(self, db):
        self.db = db
        self.pending = []   # [(request, on_done)]
        self.requests = []

    def __call__(self, request, on_done):
        self.requests.append(request)
        self.pending.append((request, on_done))

    def complete(self, index: int = 0):
        """执行第 index 个在途请求的真实查询并回调，返回快照"""
        from core.history_query import run_history_query

        request, on_done = self.pending.pop(index)
        snapshot = run_history_query(self.db, request)
        on_done(snapshot)
        return snapshot

    def fail(self, message: str, index: int = 0, storage_error: bool = True):
        """让第 index 个在途请求以失败快照返回"""
        from core.history_query import HistorySnapshot

        request, on_done = self.pending.pop(index)
        snapshot = HistorySnapshot(request=request, error=message,
                                   storage_error=storage_error)
        on_done(snapshot)
        return snapshot

    def complete_all(self):
        while self.pending:
            self.complete(0)


class ManualDefer:
    """HistoryViewModel 的 defer 替身：把推迟的回调排队，run() 时才执行（模拟下一轮
    事件循环），用于验证同一轮内的多次加载请求只发一次查询。"""

    def __init__(self):
        self.queue = []

    def __call__(self, fn):
        self.queue.append(fn)

    def run(self):
        while self.queue:
            self.queue.pop(0)()


def qt_quit_pending() -> bool:
    """本线程是否带着"已请求退出"的残留状态。

    QCoreApplication.quit()/exit() 在没有主事件循环（QCoreApplication.exec）运行时调用，
    Qt 5 会把线程数据里的 quitNow 置位，且只有下一次 QCoreApplication.exec 才清除；
    置位期间任何新建 QEventLoop 的 exec_() 都立即返回 -1。pytest-qt 的 waitSignal /
    waitUntil 都靠这种局部循环等待，于是后台线程发来的信号还没送达就"超时"了。
    探测用一个 0 毫秒定时器退出的局部循环：正常时返回 0，残留时返回 -1。
    """
    from PyQt5.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(0)
    rc = loop.exec_()
    timer.stop()
    return rc == -1


def reset_qt_quit_state() -> bool:
    """清除 qt_quit_pending 描述的残留退出状态；返回清除前是否有残留。

    跑一轮立即退出的 QCoreApplication.exec_()：Qt 在它开头和结尾都会清掉 quitNow。
    """
    from PyQt5.QtCore import QCoreApplication, QTimer

    if not qt_quit_pending():
        return False
    app = QCoreApplication.instance()
    QTimer.singleShot(0, app.quit)
    app.exec_()
    return True
