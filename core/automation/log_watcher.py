"""
日志文件增量读尾（LogWatcher）

每次 poll() 只读上次读到的位置之后新写入的完整行，逐行匹配正则，返回匹配结果。由自动
打点服务在界面线程每个 tick 调用一次：不开线程、不长期持有文件句柄（Windows 上持有句柄
会让被测程序的日志轮转——改名或删除——失败）。

- 开始监听时文件已存在：从文件末尾开始，只看之后新写的行（旧内容是上一次运行留下的）；
  文件尚不存在：静默等待，出现后从头读。
- 轮转判定：文件变小（被截断），或文件身份变了（Windows 文件索引号 / 其他平台
  inode 变化；拿不到索引号时退回创建时间）——视为新文件，从头读。
- 按字节找换行切行、逐行解码（errors='replace'），因此编码需兼容 ASCII 换行
  （UTF-8、GBK 等）；还没写完换行的半行留到下次，同一行只会被处理一次。
- 单次最多读 MAX_READ_BYTES，余下的下次再读，避免一次追读巨大增量卡住界面。
"""
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional, Tuple, Union

from core.automation.plan import DEFAULT_LOG_ENCODING, LABEL_GROUP
from core.monitor_task import SYSTEM_CLOCK

logger = logging.getLogger(__name__)

# 单次 poll 最多读取的字节数
MAX_READ_BYTES = 1024 * 1024
# 没有换行的超长"行"上限：超过即按一行处理，防止缓冲无限增长
MAX_LINE_BYTES = 64 * 1024

# 文件身份：(索引号或创建时间的种类, 值)
_Identity = Tuple[str, Any]


@dataclass(frozen=True)
class LogMatch:
    """日志里匹配到的一行。

    Attributes:
        line: 解码后的整行（不含换行符）
        label: 命名分组 label 匹配到的非空文本（去首尾空白），没有为 None
        timestamp: 读到这一行的时刻（注入时钟）
    """
    line: str
    label: Optional[str]
    timestamp: datetime


def _identity(stat: os.stat_result) -> _Identity:
    """文件身份：优先索引号（Windows 文件索引 / inode），取不到时用创建时间。"""
    if getattr(stat, 'st_ino', 0):
        return ('ino', (stat.st_dev, stat.st_ino))
    birth = getattr(stat, 'st_birthtime', None)
    if birth is None and os.name == 'nt':
        birth = stat.st_ctime     # Windows 上 st_ctime 是创建时间
    return ('birth', birth)


class LogWatcher:
    """单个日志文件的增量读尾与匹配。

    Args:
        path: 日志文件绝对路径
        pattern: 正则表达式（文本或已编译）；命名分组 label 可作打点名称
        encoding: 文件编码（需兼容 ASCII 换行）
        clock: 提供 now() 的时钟（ui.services.clock 或 core.monitor_task.SystemClock）；
            缺省为 core.monitor_task.SYSTEM_CLOCK
        start_at_end: 开始监听时文件已存在则从末尾开始（默认）；False 从头读
    """

    def __init__(self, path: str, pattern: Union[str, "re.Pattern[str]"],
                 encoding: str = DEFAULT_LOG_ENCODING, clock=None, *,
                 start_at_end: bool = True):
        self.path = path
        self.pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
        self.encoding = encoding or DEFAULT_LOG_ENCODING
        self._clock = clock if clock is not None else SYSTEM_CLOCK
        self._offset = 0
        self._pending = b''
        self._identity: Optional[_Identity] = None
        # 上次 poll 时文件是否存在：消失后再出现即视为新文件
        self._present = False
        # 读文件出错（如被独占锁定）只记一次日志，恢复后清除
        self._error_logged = False
        stat = self._stat()
        if stat is not None:
            self._present = True
            self._identity = _identity(stat)
            self._offset = stat.st_size if start_at_end else 0

    # ---------- 查询（测试与诊断） ----------

    @property
    def offset(self) -> int:
        """已消费到的字节位置（不含尚未读到换行的半行）。"""
        return self._offset - len(self._pending)

    # ---------- 读尾 ----------

    def _stat(self) -> Optional[os.stat_result]:
        try:
            return os.stat(self.path)
        except OSError:
            return None

    def _now(self) -> datetime:
        return self._clock.now()

    def _restart(self, reason: str) -> None:
        logger.info("日志文件%s，从头读取: %s", reason, self.path)
        self._offset = 0
        self._pending = b''

    def poll(self) -> List[LogMatch]:
        """读新增的完整行并返回其中匹配的行（按出现顺序）。文件不存在、读失败都返回空列表。"""
        stat = self._stat()
        if stat is None:
            self._present = False
            return []
        identity = _identity(stat)
        if not self._present:
            if self._identity is not None:
                self._restart("重新出现")
            self._present = True
        elif self._identity is not None and identity != self._identity:
            self._restart("已被替换（轮转）")
        elif stat.st_size < self._offset:
            self._restart("变小（被截断或轮转）")
        self._identity = identity

        if stat.st_size <= self._offset:
            return []
        try:
            with open(self.path, 'rb') as f:
                f.seek(self._offset)
                chunk = f.read(min(MAX_READ_BYTES, stat.st_size - self._offset))
        except OSError:
            if not self._error_logged:
                logger.warning("读取日志文件失败，稍后重试: %s", self.path, exc_info=True)
                self._error_logged = True
            return []
        self._error_logged = False
        self._offset += len(chunk)
        return self._match_lines(chunk)

    def _match_lines(self, chunk: bytes) -> List[LogMatch]:
        data = self._pending + chunk
        lines = data.split(b'\n')
        self._pending = lines.pop()
        if len(self._pending) > MAX_LINE_BYTES:
            lines.append(self._pending)
            self._pending = b''
        matches: List[LogMatch] = []
        now = None
        for raw in lines:
            text = raw.rstrip(b'\r').decode(self.encoding, errors='replace')
            match = self.pattern.search(text)
            if match is None:
                continue
            if now is None:
                now = self._now()
            matches.append(LogMatch(line=text, label=_label_of(match), timestamp=now))
        return matches


def _label_of(match: "re.Match[str]") -> Optional[str]:
    """命名分组 label 的非空文本；没有该分组或为空时 None。"""
    if LABEL_GROUP not in match.re.groupindex:
        return None
    value = match.group(LABEL_GROUP)
    value = value.strip() if value else ''
    return value or None
