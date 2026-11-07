"""
无窗口模式（--background）的运行时

pmctl 在 GUI 不在时以 --background 拉起本程序：不建主窗口与托盘、不注册全局快捷键，只跑
监控管理器、自动打点服务、阻止睡眠守卫与 IPC 命令处理器。组合根（app.bootstrap）负责
启动顺序，这里只管运行期：

- 自动打点服务（AutoMarkerService）由这里持有，每秒 tick 一次（有窗口时这件事由监控页
  Presenter 的每秒定时器做）；
- 阶段快照：方案到期时由这里持有的 StageSnapshotWriter 在后台写报告图片与摘要 CSV（图片的
  渲染函数由组合根注入；没有时只写摘要 CSV），失败只记日志（没有界面可提示）；
- 空闲退出：idle_exit_s > 0 时，没有任何任务、等待与进行中的命令行请求持续满 idle_exit_s
  秒即退出；0 表示不因空闲退出；
- 退出：quit 命令、Ctrl+C / Ctrl+Break（SIGINT / SIGBREAK）、空闲到时或会话结束（注销 /
  关机），都经 ShutdownCoordinator 收尾（停任务 → 带期限 join → 登记表线程 → 关库），再结束
  事件循环，退出码 0。Python 的信号处理函数只有解释器拿到控制权时才会执行，所以另开一个
  短周期的空定时器让事件循环定期回到 Python。
- 会话结束：没有主窗口替我们处理 QApplication.commitDataRequest，这里直接连接它
  （DirectConnection：必须在系统结束进程之前同步收尾，任务的结束时间与数据质量才能落库）。

只在界面线程使用；不依赖 ui 包（报告图片的渲染函数由组合根注入给命令处理器与阶段快照）。
"""
import logging
import signal
import time
from typing import Callable, Optional, Sequence

from PyQt5.QtCore import QCoreApplication, QObject, Qt, QTimer

from core.automation.auto_marker import AutoMarkerService
from core.automation.stage_snapshot import StageSnapshotWriter
from core.monitor_task import SYSTEM_CLOCK
from core.shutdown import ShutdownCoordinator

logger = logging.getLogger(__name__)

# 自动打点与空闲检查的间隔、让 Python 处理信号的间隔（毫秒）
TICK_INTERVAL_MS = 1000
SIGNAL_POLL_MS = 200
# 退出码
EXIT_OK = 0


class HeadlessRuntime(QObject):
    """无窗口模式的运行期对象（组合根创建并持有）。"""

    def __init__(self, manager, registry, db, parent: Optional[QObject] = None, *,
                 clock=SYSTEM_CLOCK, idle_exit_s: float = 0.0,
                 monotonic: Callable[[], float] = time.monotonic,
                 busy: Optional[Callable[[], bool]] = None,
                 quit_app: Optional[Callable[[int], None]] = None,
                 coordinator_factory=ShutdownCoordinator,
                 stage_renderer: Optional[Callable] = None,
                 snapshot_writer: Optional[StageSnapshotWriter] = None,
                 quit_hooks: Sequence[Callable[[], None]] = ()):
        """
        Args:
            manager: MonitorManager
            registry: 线程登记表（退出时收尾）
            db: 数据库（退出时关闭）
            clock: 提供 now() 的时钟（自动打点服务用）
            idle_exit_s: 空闲多少秒后退出；0 不退出
            monotonic: 单调时钟（秒），空闲计时用，测试可注入
            busy: 额外的"仍有工作"判断（如命令处理器有进行中的请求）
            quit_app: 结束事件循环的函数，默认 QCoreApplication.exit
            stage_renderer: 阶段快照的报告图片渲染函数（见 StageSnapshotWriter 的 renderer）
            snapshot_writer: 阶段快照写入器（测试替换）；None 时按 db / registry / clock 自建
            quit_hooks: 退出收尾之前依次调用的函数（如停掉存储守卫的定时器与在途作业）；
                出错只记日志
        """
        super().__init__(parent)
        self.manager = manager
        self.registry = registry
        self.db = db
        self.idle_exit_s = max(0.0, float(idle_exit_s or 0.0))
        self._monotonic = monotonic
        self._busy = busy
        self._quit_app = quit_app or QCoreApplication.exit
        self._coordinator_factory = coordinator_factory
        self._idle_since: Optional[float] = None
        self._quit_hooks = tuple(quit_hooks)
        self.quitting = False
        self.exit_code: Optional[int] = None
        self.auto_marker = AutoMarkerService(manager, clock, self)
        self.snapshot_writer = (snapshot_writer if snapshot_writer is not None
                                else StageSnapshotWriter(db, manager, registry, clock, self,
                                                         renderer=stage_renderer))
        self.auto_marker.stage_snapshot_due.connect(self.snapshot_writer.write)
        # 写入器已带原因记过日志；这里补一条说明为什么没有提示
        self.snapshot_writer.failed.connect(lambda task_id, message: logger.warning(
            "阶段快照未写出（无窗口模式只记日志）: task_id=%s %s", task_id, message))

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(TICK_INTERVAL_MS)
        self._tick_timer.timeout.connect(self.tick)
        self._signal_timer = QTimer(self)
        self._signal_timer.setInterval(SIGNAL_POLL_MS)
        self._signal_timer.timeout.connect(lambda: None)

    def start(self) -> None:
        """开始每秒驱动，并接管 Ctrl+C / Ctrl+Break 与会话结束。"""
        self._idle_since = self._monotonic()
        self._tick_timer.start()
        self._signal_timer.start()
        self.install_signal_handlers()
        self.install_session_end_handler()

    def install_session_end_handler(self, app=None) -> bool:
        """把 commitDataRequest（注销 / 关机）连到收尾；没有 QApplication 时跳过。"""
        app = app if app is not None else QCoreApplication.instance()
        signal_ = getattr(app, 'commitDataRequest', None)
        if signal_ is None:
            return False
        try:
            signal_.connect(self._on_session_end, Qt.DirectConnection)
            return True
        except Exception:
            logger.warning("无法接管会话结束信号", exc_info=True)
            return False

    def _on_session_end(self, _manager=None) -> None:
        try:
            logger.info("会话结束（注销或关机），无窗口模式开始退出收尾")
            self.quit()
        except Exception:
            logger.error("会话结束时退出收尾失败", exc_info=True)

    def install_signal_handlers(self) -> None:
        for name in ('SIGINT', 'SIGBREAK'):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError):
                # 不在主线程（测试）或平台不支持：不影响运行
                logger.debug("无法接管信号 %s", name, exc_info=True)

    def _on_signal(self, signum, _frame) -> None:
        logger.info("收到中断信号 %s，开始退出收尾", signum)
        # 不在信号处理函数里直接收尾：交给事件循环，保证在界面线程的正常调用栈里执行
        QTimer.singleShot(0, self.quit)

    def is_idle(self) -> bool:
        if self.manager.get_task_count() > 0:
            return False
        try:
            return not (self._busy is not None and self._busy())
        except Exception:
            logger.error("判断是否空闲失败", exc_info=True)
            return False

    def tick(self) -> None:
        """每秒：驱动自动打点（长周期与暂停时也准时），并检查空闲退出。"""
        try:
            self.auto_marker.tick()
        except Exception:
            logger.error("自动打点驱动失败", exc_info=True)
        if self.idle_exit_s <= 0 or self.quitting:
            return
        now = self._monotonic()
        if not self.is_idle():
            self._idle_since = now
            return
        if self._idle_since is None:
            self._idle_since = now
        if now - self._idle_since >= self.idle_exit_s:
            logger.info("无窗口模式空闲 %.0f 秒，自动退出", now - self._idle_since)
            self.quit()

    def quit(self) -> None:
        """真退出：经 ShutdownCoordinator 收尾后结束事件循环（只执行一次）。"""
        if self.quitting:
            return
        self.quitting = True
        self._tick_timer.stop()
        for hook in self._quit_hooks:
            try:
                hook()
            except Exception:
                logger.error("退出前的收尾钩子失败", exc_info=True)
        try:
            coordinator = self._coordinator_factory(self.manager, self.registry, db=self.db)
            if not coordinator.run(on_progress=lambda text: logger.info("退出收尾: %s", text)):
                logger.error("退出收尾未在期限内全部完成，详见上方日志")
        except Exception:
            logger.error("无窗口模式退出收尾失败", exc_info=True)
        finally:
            self.exit_code = EXIT_OK
            self._quit_app(EXIT_OK)
