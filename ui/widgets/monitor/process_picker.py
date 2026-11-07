"""
进程选择器

一个搜索框兼管"按名搜索"和"直接输入 PID"：输入纯数字即视为 PID，不再另设 PID
快捷框（两个入口要双向同步，容易互相覆盖）。下方是字段级的校验错误文字，以及搜索
无结果时可点击的"未找到，点击刷新"。

进程列表自动刷新：展开下拉、输入框获得焦点（页面另在窗口激活/显示时调用
refresh_if_stale）时重新读取，两秒内不重复读取——被测程序往往是刚启动的，
不该让用户先想起去点刷新。

读取方式：给了线程登记表（构造参数 thread_registry 或 set_thread_registry）时在
后台线程读取（ProcessListWorker），psutil 冷启动遍历要 2 秒上下，不能卡住界面与首屏；
同一时刻只跑一个读取线程，进行中再请求刷新只记一笔"结束后再读一次"；结果按
LatestRequestGate 判过期。读取期间搜索框占位文字改为"正在读取进程列表…"，并经
loading_changed 通知页面（刷新按钮置灰）。没给登记表时同步读取（控件单元测试用）。
列表到达时只替换补全项：用户已输入的文本、已选中的进程都保留；直接输入 PID 不依赖
列表，列表到达前即可解析。

resolve() 只给出"要监控谁"或"为什么不行"（错误类型），中文文案由页面/Presenter 映射。
"""
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Set, Union

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel

from core.workers.base import LatestRequestGate
from core.workers.process_list_worker import (
    SHUTDOWN_TIMEOUT_MS, THREAD_NAME, ProcessListWorker,
)
from ui.services.process_query import ProcessEntry, ProcessQuery
from ui.widgets.searchable_combo import SearchableCombo

if TYPE_CHECKING:
    from core.shutdown import ThreadRegistry

logger = logging.getLogger(__name__)

# 搜索框占位文字：平时 / 后台读取进程列表期间
SEARCH_PLACEHOLDER = "搜索进程名或输入 PID（Ctrl+F）"
LOADING_PLACEHOLDER = "正在读取进程列表…"

# 运行中的读取线程：登记表在退出前一直持有它们，这里再兜一层强引用，保证控件或
# 登记表先于线程被回收时（如测试结束、页面被销毁）线程对象不会在运行中被析构
_RUNNING_WORKERS: Set[ProcessListWorker] = set()

# 校验错误文字颜色（浅色 / 深色主题，取 Fluent 的错误色）
ERROR_LIGHT = QColor(196, 43, 28)
ERROR_DARK = QColor(255, 153, 164)

# 看起来是在输 PID、但不是合法正整数的输入（负数、小数、带正负号）
_NUMBER_LIKE = re.compile(r"[+-]?\d+(\.\d*)?|[+-]?\.\d+")

# resolve() 的错误类型
ERROR_EMPTY = 'empty'                  # 什么都没输入
ERROR_NO_MATCH = 'no_match'            # 文本与进程列表任何一项都不一致
ERROR_NOT_A_NUMBER = 'not_a_number'    # 形似数字但不是合法 PID
ERROR_NOT_FOUND = 'not_found'          # 输入的 PID 对应的进程不存在或无权访问
ERROR_WAIT_NEEDS_NAME = 'wait_needs_name'   # 等待进程启动只能按进程名（空或纯数字）
ERROR_GROUP_NEEDS_NAME = 'group_needs_name'  # 同名进程任务只能按进程名（空或纯数字）

# 自动刷新的节流间隔（秒）：展开下拉、获得焦点、窗口激活可能接连发生，只读一次
REFRESH_THROTTLE_SECONDS = 2.0


def no_match_text(text: str) -> str:
    """搜索无结果时的可点击提示。"""
    return f"未找到“{text}”，点击刷新"


class LinkLabel(CaptionLabel):
    """可点击的说明文字（链接色、手形光标），点击发出 clicked。"""

    clicked = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.PointingHandCursor)
        self.setTextColor(QColor(0, 95, 184), QColor(153, 235, 255))

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()


@dataclass(frozen=True)
class Target:
    """要监控的进程。create_time 来自进程列表，用于任务启动时确认 PID 没有被别的
    进程复用；手动输入的 PID 没有可比对的列表信息，为 None。"""
    pid: int
    name: str
    create_time: Optional[float] = None


@dataclass(frozen=True)
class ResolveError:
    """无法确定目标进程的原因。text 为当时的输入文本，pid 只在 not_found 时有值。"""
    kind: str
    text: str = ""
    pid: Optional[int] = None


def item_text(target: Target) -> str:
    """下拉选项的显示文本：同名进程靠 PID 区分，文本互不相同。"""
    return f"{target.name} · PID {target.pid}"


class ProcessPicker(QWidget):
    """进程选择器（搜索框 + 字段错误 + 无结果提示）。

    信号：
        selected(object): 用户选中了列表中的某个进程（Target）
        edited(): 输入内容发生变化（页面据此清掉表单错误等）
        loading_changed(bool): 后台读取进程列表开始 / 结束（含合并的补读）
    """

    selected = pyqtSignal(object)
    edited = pyqtSignal()
    loading_changed = pyqtSignal(bool)

    def __init__(self, process_query: ProcessQuery, parent=None, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 thread_registry: Optional["ThreadRegistry"] = None):
        """
        Args:
            process_query: 进程查询服务
            parent: 父控件
            monotonic: 单调时钟（秒），用于自动刷新节流；测试可注入
            thread_registry: 线程登记表；给出时在后台线程读取进程列表，None 时同步读取
        """
        super().__init__(parent)
        self._query = process_query
        self._monotonic = monotonic
        self._last_refresh: Optional[float] = None
        self._no_match_enabled = True
        self._registry = thread_registry
        self._gate = LatestRequestGate()
        self._worker: Optional[ProcessListWorker] = None
        self._rerun = False          # 读取进行中又被请求刷新：结束后再读一次
        self._loading = False
        self._applying = False       # set_entries 改写输入框期间，文本变化不算用户编辑
        self._placeholder = SEARCH_PLACEHOLDER

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.combo = SearchableCombo(
            self, placeholder=SEARCH_PLACEHOLDER,
            number_matcher=lambda target, pid: target.pid == pid)
        layout.addWidget(self.combo)
        # 展开下拉之前先刷新（pressed 早于库连接的 clicked → 弹出菜单）；获得焦点时也刷新
        self.combo.line_edit.dropButton.pressed.connect(self._on_drop_pressed)
        self.combo.line_edit.installEventFilter(self)

        # 校验错误就近显示在搜索框下方，内容一改即清除；不用页面顶部提示，
        # 避免一闪即逝或与运行期提示混在一起
        self.error_label = CaptionLabel(self)
        self.error_label.setTextColor(ERROR_LIGHT, ERROR_DARK)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        # 搜索无结果："未找到"xxx"，点击刷新"——被测程序多半是刚启动、列表还没读到
        self.no_match_label = LinkLabel(self)
        self.no_match_label.setToolTip("重新读取进程列表")
        self.no_match_label.hide()
        self.no_match_label.clicked.connect(self._on_no_match_clicked)
        layout.addWidget(self.no_match_label)

        self.combo.selected.connect(self.selected)
        self.combo.text_changed.connect(self._on_text_changed)

    # ========== 列表 ==========

    def set_thread_registry(self, registry: Optional["ThreadRegistry"]) -> None:
        """之后的刷新改在后台线程读取（登记到 registry）；None 恢复同步读取。"""
        self._registry = registry

    def refresh(self) -> None:
        """重新读取进程列表：仍在的已选进程保持选中；没有选中项时保留手输文本。
        刷新后不会自动选中任何进程（避免误监控列表第一个进程）。

        有线程登记表时只发起后台读取、立即返回，列表到达后经 set_entries 填充；
        读取进行中再调用只合并为"结束后再读一次"。"""
        self._last_refresh = self._monotonic()
        if self._registry is None:
            self.set_entries(self._query.list_processes())
            return
        if self._worker is not None:
            self._rerun = True
            return
        self._start_worker()

    def set_entries(self, entries: Iterable[ProcessEntry]) -> None:
        """用新的进程列表替换补全项（界面线程）：仍在的已选进程保持选中，没有选中项时
        保留手输文本与光标位置；不自动选中任何进程。"""
        before = self.combo.text()
        cursor = self.combo.line_edit.cursorPosition()
        keep = self.combo.current_data()
        typed = before if keep is None else ""
        targets = [Target(pid, name, create_time) for pid, name, create_time in entries]
        self._applying = True
        try:
            self.combo.set_items([(item_text(t), t) for t in targets], keep=keep)
            if typed and self.combo.current_data() is None:
                self.combo.set_text(typed)
            if self.combo.text() == before:
                self.combo.line_edit.setCursorPosition(cursor)
        finally:
            self._applying = False
        if self.combo.text() != before:
            # 已选进程不在了，输入框随之清空：此前的校验错误不再对应当前内容
            self.clear_error()
            self.edited.emit()
        self._update_no_match()

    def is_loading(self) -> bool:
        """是否正在后台读取进程列表。"""
        return self._loading

    def _start_worker(self) -> None:
        worker = ProcessListWorker(self._query, request_seq=self._gate.next_seq())
        worker.entries_ready.connect(self._on_entries_ready)
        worker.failed.connect(self._on_list_failed)
        worker.finished.connect(self._on_worker_finished)
        # 控件先于线程销毁（页面关闭）时让结果作废；线程本身由登记表收尾
        self.destroyed.connect(worker.cancel)
        _RUNNING_WORKERS.add(worker)
        worker.finished.connect(lambda w=worker: _RUNNING_WORKERS.discard(w))
        self._registry.register(worker, name=THREAD_NAME, cancel=worker.cancel,
                                timeout_ms=SHUTDOWN_TIMEOUT_MS)
        self._worker = worker
        self._set_loading(True)
        worker.start()

    def _on_entries_ready(self, seq, entries) -> None:
        if self._gate.is_current(seq):
            self.set_entries(entries)

    def _on_list_failed(self, message: str) -> None:
        # 读取失败保留旧列表；直接输入 PID 不受影响，下次自动刷新再试
        logger.warning("%s", message)

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is None or worker is not self._worker:
            return
        self._worker = None
        try:
            self.destroyed.disconnect(worker.cancel)
        except TypeError:
            pass
        if self._rerun:
            self._rerun = False
            self._start_worker()
        else:
            self._set_loading(False)

    def _set_loading(self, loading: bool) -> None:
        if loading == self._loading:
            return
        self._loading = loading
        # 占位文案只在列表还是空的（首屏）时切换：已有列表时的自动补读（窗口激活、
        # F5）通常不到 2 秒，来回切文案只会让搜索框闪动
        if loading and self.combo.count() == 0:
            self.combo.line_edit.setPlaceholderText(LOADING_PLACEHOLDER)
        elif not loading:
            self.combo.line_edit.setPlaceholderText(self._placeholder)
        self.loading_changed.emit(loading)

    def refresh_if_stale(self) -> bool:
        """距上次读取已超过节流间隔时刷新；返回是否真的刷新了。"""
        if (self._last_refresh is not None
                and self._monotonic() - self._last_refresh < REFRESH_THROTTLE_SECONDS):
            return False
        self.refresh()
        return True

    def _on_drop_pressed(self) -> None:
        # 下拉已展开时这次点击是收起，不刷新
        if not self.combo.line_edit.dropMenu:
            self.refresh_if_stale()

    def eventFilter(self, obj, event):
        # 弹出补全菜单抢走又还回焦点（PopupFocusReason）不算用户回到输入框
        if (obj is self.combo.line_edit and event.type() == QEvent.FocusIn
                and event.reason() != Qt.PopupFocusReason):
            self.refresh_if_stale()
        return super().eventFilter(obj, event)

    def targets(self):
        """当前进程列表（Target 列表，按显示顺序）。"""
        return [data for _text, data in self.combo.items()]

    # ========== 解析 ==========

    def resolve(self) -> Union[Target, ResolveError]:
        """确定要监控的进程：列表中精确选中的项优先；否则输入为纯数字时按 PID 查询；
        其余情况给出错误类型。"""
        target = self.combo.current_data()
        if target is not None:
            return target
        text = self.combo.text().strip()
        if not text:
            return ResolveError(ERROR_EMPTY)
        if text.isdigit():
            pid = int(text)
            name = self._query.name_of(pid)
            if not name:
                return ResolveError(ERROR_NOT_FOUND, text, pid)
            return Target(pid, name, None)
        if _NUMBER_LIKE.fullmatch(text):
            return ResolveError(ERROR_NOT_A_NUMBER, text)
        return ResolveError(ERROR_NO_MATCH, text)

    def resolve_name(self, error_kind: str = ERROR_WAIT_NEEDS_NAME) -> Union[str, ResolveError]:
        """按进程名监控（等待进程启动、同名进程任务）用的进程名：选中项取其进程名，否则取
        输入文本；空或纯数字（PID 没有"名字"）给出 error_kind 类型的错误。

        输入的名字没写扩展名、而进程列表里恰有 "名字.exe" 时补全为它（如输入 chrome 取
        chrome.exe）：按名监控是精确匹配，少了 .exe 会一个进程也匹配不到。"""
        target = self.combo.current_data()
        if target is not None:
            return target.name
        text = self.combo.text().strip()
        if not text or text.isdigit():
            return ResolveError(error_kind, text)
        if '.' not in text:
            wanted = f"{text}.exe".casefold()
            for listed in self.targets():
                if listed.name.casefold() == wanted:
                    return listed.name
        return text

    def current_pid(self) -> Optional[int]:
        """输入当前指向的 PID（选中项或纯数字输入），都不是时为 None；不查询进程。"""
        target = self.combo.current_data()
        if target is not None:
            return target.pid
        text = self.combo.text().strip()
        return int(text) if text.isdigit() else None

    def forget_if(self, pid: int) -> bool:
        """被测进程已退出时，若输入仍指向它，清掉并刷新进程列表：避免已不存在的
        PID 一直留在表单里，用户再点"开始监控"才报找不到。返回是否清掉了。"""
        if self.current_pid() != pid or self._query.is_alive(pid):
            return False
        self.combo.set_text("")
        self.refresh()
        return True

    # ========== 输入 ==========

    def set_text(self, text: str) -> None:
        self.combo.set_text(text)

    def set_placeholder(self, text: Optional[str]) -> None:
        """搜索框的占位文字（目标类型不同提示不同）；None 恢复默认。读取进程列表期间
        仍显示"正在读取进程列表…"，结束后恢复到这里设的文字。"""
        self._placeholder = text or SEARCH_PLACEHOLDER
        if not (self._loading and self.combo.count() == 0):
            self.combo.line_edit.setPlaceholderText(self._placeholder)

    def text(self) -> str:
        return self.combo.text()

    def focus_search(self) -> None:
        self.combo.focus_input()

    # ========== 字段错误 ==========

    def set_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.show()

    def clear_error(self) -> None:
        if self.error_label.isHidden() and not self.error_label.text():
            return
        self.error_label.clear()
        self.error_label.hide()

    def error_text(self) -> str:
        """当前显示的错误文字（未显示时为空）。"""
        return "" if self.error_label.isHidden() else self.error_label.text()

    # ========== 搜索无结果 ==========

    def set_no_match_hint_enabled(self, enabled: bool) -> None:
        """是否在搜索无结果时提示"未找到，点击刷新"（等待进程启动时关闭：
        此时进程本来就不在列表里）。"""
        self._no_match_enabled = enabled
        self._update_no_match()

    def no_match_visible(self) -> bool:
        return not self.no_match_label.isHidden()

    def _has_match(self, text: str) -> bool:
        needle = text.casefold()
        return any(needle in item.casefold() for item, _data in self.combo.items())

    def _update_no_match(self) -> None:
        text = self.combo.text().strip()
        show = (self._no_match_enabled and bool(text) and not text.isdigit()
                and self.combo.current_data() is None and not self._has_match(text))
        if show:
            self.no_match_label.setText(no_match_text(text))
        self.no_match_label.setVisible(show)

    def _on_no_match_clicked(self) -> None:
        self.refresh()

    def _on_text_changed(self, _text: str) -> None:
        if self._applying:
            return   # 替换列表时的中间态（清空再写回），结束后由 set_entries 统一处理
        # 输入一改，之前的校验错误就不再对应当前内容
        self.clear_error()
        self._update_no_match()
        self.edited.emit()
