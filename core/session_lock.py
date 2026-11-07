"""
会话锁：判断上一次运行是不是异常结束

数据目录下的 session.lock 记录当前实例的 PID、进程创建时间、启动时间与版本。程序启动时写入，
所有正常退出路径（关窗、托盘退出、注销 / 关机、无窗口模式退出）收尾完成后删除。因此启动时
发现残留的锁，且记录的进程已经不在（或 PID 已被别的程序占用），就说明上一次运行被强杀、崩溃
或断电——库里仍是 running 的任务才有"续采"的可能。

四种启动前状态（PreviousSession）：
- CLEAN：没有锁文件（上次正常退出，或第一次运行）；
- ABNORMAL：有锁，但记录的进程已不在、已被别的程序占用，或锁文件损坏读不出来；
- OTHER_INSTANCE：有锁且记录的进程仍是本程序在运行（另一个实例）——不能判为异常退出，也不
  覆盖它的锁、退出时也不删它的锁（正常情况下单实例守卫已经拦下了第二个实例，这是兜底）；
  启动校正也要跳过，否则会把它正在跑的任务收尾成"已停止"；
- UNKNOWN：有锁，但探测记录的进程时权限不足，无法确认它在不在（如上一个实例以管理员身份
  运行）。既不能说它异常退出（会误续采、误收尾），也不能说它在运行：不覆盖它的锁、不续采，
  只记日志。

锁的写入与进程探测都可注入，测试用假进程表驱动，不碰真实进程。只依赖标准库与 psutil，不碰 Qt。
"""
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Optional, Tuple

import psutil

import config

logger = logging.getLogger(__name__)

LOCK_FILE_NAME = 'session.lock'
# 创建时间比对容差（秒）：与 MonitorWorker 的 PID 复用比对同一量级
CREATE_TIME_TOLERANCE_S = 1.0


class PreviousSession(Enum):
    """启动时对上一次运行的判断。"""
    CLEAN = 'clean'
    ABNORMAL = 'abnormal'
    OTHER_INSTANCE = 'other_instance'
    UNKNOWN = 'unknown'


class ProbeIndeterminate(Exception):
    """进程探测无法确认进程在不在（权限不足等）。与"确定不在"（返回 None）区分开。"""


@dataclass(frozen=True)
class LockInfo:
    """锁文件内容。create_time 为记录进程的创建时间（epoch 秒，读不到为 None）。"""
    pid: int
    started_at: str
    version: str
    create_time: Optional[float] = None
    exe_name: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({'pid': self.pid, 'started_at': self.started_at,
                           'version': self.version, 'create_time': self.create_time,
                           'exe_name': self.exe_name}, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> 'LockInfo':
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get('pid'), int):
            raise ValueError("会话锁内容不合法")
        create_time = data.get('create_time')
        return cls(pid=data['pid'], started_at=str(data.get('started_at') or ''),
                   version=str(data.get('version') or ''),
                   create_time=float(create_time) if isinstance(create_time, (int, float))
                   else None,
                   exe_name=data.get('exe_name') if isinstance(data.get('exe_name'), str)
                   else None)


# 进程探测：pid -> (创建时间, 可执行文件名)；进程不存在返回 None；无法确认抛 ProbeIndeterminate
ProcessProbe = Callable[[int], Optional[Tuple[Optional[float], Optional[str]]]]


def probe_process(pid: int) -> Optional[Tuple[Optional[float], Optional[str]]]:
    """生产实现：用 psutil 读进程的创建时间与名称；不存在或已结束返回 None；权限不足等
    无法确认时抛 ProbeIndeterminate（调用方按"无法确认"处理，不能当成"不在"）。"""
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        try:
            create_time: Optional[float] = proc.create_time()
        except (psutil.Error, OSError):
            create_time = None
        try:
            name: Optional[str] = proc.name()
        except (psutil.Error, OSError):
            name = None
        return create_time, name
    except psutil.NoSuchProcess:
        return None
    except (ValueError, OverflowError):
        return None    # 不是合法的 PID
    except (psutil.Error, OSError) as exc:
        # 权限不足等：按"在"（当成另一个实例）或"不在"（当成异常退出）处理都会出错——
        # 前者放过真正的崩溃，后者会覆盖别人的锁、把别人正在跑的任务收尾——交给调用方
        logger.warning("无法确认进程 %s 是否在运行（%s）", pid, exc)
        raise ProbeIndeterminate(str(exc)) from exc


class SessionLock:
    """某个数据目录的会话锁。"""

    def __init__(self, directory: str, *, pid: Optional[int] = None,
                 probe: ProcessProbe = probe_process,
                 now: Callable[[], datetime] = datetime.now,
                 version: Optional[str] = None):
        """
        Args:
            directory: 数据目录（锁文件放在里面）
            pid: 本进程 PID，默认 os.getpid()
            probe: 进程探测函数（测试注入假进程表）
            now: 取当前时间
            version: 写进锁的版本号，默认 config.APP_VERSION
        """
        self.path = os.path.join(directory, LOCK_FILE_NAME)
        self._pid = pid if pid is not None else os.getpid()
        self._probe = probe
        self._now = now
        self._version = version if version is not None else config.APP_VERSION
        self._owned = False

    # ---------- 启动前判断 ----------

    def read(self) -> Optional[LockInfo]:
        """读锁文件；不存在返回 None，损坏抛 ValueError。"""
        try:
            with open(self.path, encoding='utf-8') as f:
                return LockInfo.from_json(f.read())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"无法读取会话锁: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"会话锁不是合法 JSON: {exc}") from exc

    def check_previous(self) -> PreviousSession:
        """判断上一次运行的结局（只读，不改锁文件）。"""
        try:
            info = self.read()
        except ValueError:
            logger.warning("会话锁文件损坏，按上次异常退出处理: %s", self.path)
            return PreviousSession.ABNORMAL
        if info is None:
            return PreviousSession.CLEAN
        if info.pid == self._pid:
            # 本进程自己写的（同一进程重复初始化，如测试）：不是上一次运行遗留的
            return PreviousSession.CLEAN
        try:
            running = self._is_this_program_running(info)
        except ProbeIndeterminate:
            logger.warning("无法确认会话锁记录的进程 (PID %d) 是否仍在运行（权限不足？），"
                           "不覆盖它的锁、不续采", info.pid)
            return PreviousSession.UNKNOWN
        if running:
            logger.info("会话锁属于仍在运行的另一个实例 (PID %d)，不判为异常退出", info.pid)
            return PreviousSession.OTHER_INSTANCE
        logger.warning("发现上次运行遗留的会话锁（PID %d，启动于 %s，版本 %s，进程已不在）："
                       "上次异常退出", info.pid, info.started_at, info.version)
        return PreviousSession.ABNORMAL

    def _is_this_program_running(self, info: LockInfo) -> bool:
        found = self._probe(info.pid)
        if found is None:
            return False
        create_time, name = found
        if info.create_time is not None and create_time is not None:
            # 创建时间一致才是同一个进程；不一致说明 PID 已被复用
            return abs(create_time - info.create_time) <= CREATE_TIME_TOLERANCE_S
        # 没有创建时间可比：退回比较可执行文件名
        if info.exe_name and name:
            return info.exe_name.lower() == name.lower()
        return False

    # ---------- 写入与删除 ----------

    def acquire(self) -> bool:
        """写入本实例的锁。已有属于另一个仍在运行的实例的锁时不覆盖，返回 False。
        写失败（目录只读等）只记日志返回 False：没有锁只会让下次启动少一次续采机会。"""
        if self.check_previous() in (PreviousSession.OTHER_INSTANCE, PreviousSession.UNKNOWN):
            return False
        try:
            found = self._probe(self._pid)
        except ProbeIndeterminate:
            found = None
        create_time, name = found if found is not None else (None, None)
        info = LockInfo(pid=self._pid, started_at=self._now().strftime('%Y-%m-%d %H:%M:%S'),
                        version=self._version, create_time=create_time, exe_name=name)
        tmp = self.path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(info.to_json())
            os.replace(tmp, self.path)
        except OSError:
            logger.warning("写入会话锁失败: %s", self.path, exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        self._owned = True
        return True

    def release(self) -> bool:
        """正常退出时删除自己写的锁（可重复调用）；锁已属于别的实例或不存在不动它。

        Returns:
            bool: 本次调用删掉了锁文件
        """
        try:
            info = self.read()
        except ValueError:
            # 自己写的锁被损坏（磁盘问题等）：本实例持有过就照删，免得下次误判
            info = LockInfo(self._pid, '', '') if self._owned else None
        if info is None or info.pid != self._pid:
            self._owned = False
            return False
        try:
            os.remove(self.path)
        except FileNotFoundError:
            return False
        except OSError:
            logger.warning("删除会话锁失败: %s", self.path, exc_info=True)
            return False
        self._owned = False
        return True

    @property
    def owned(self) -> bool:
        """本实例是否持有锁（acquire 成功且尚未 release）。"""
        return self._owned
