"""管理员重启的一次性交接记录，候选实例等待旧进程退出后才允许启动。"""
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from utils.elevation import WindowsProcess

RESTART_ARG = '--restart-token'
RESTART_FILE = 'restart-handoff.json'
READY_TIMEOUT_S = 30.0
COMMIT_TIMEOUT_S = 90.0
MAX_STATE_BYTES = 16 * 1024 * 1024


def normalized_directory(directory: str) -> str:
    return os.path.normcase(os.path.realpath(directory))


def read_json(path: Path) -> dict:
    with path.open('rb') as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError('重启交接记录过大')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('重启交接记录格式错误')
    return result


def write_json(path: Path, state: dict) -> None:
    temporary = path.with_suffix('.tmp')
    raw = json.dumps(state, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError('重启交接记录过大')
    try:
        with temporary.open('wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def restart_token(argv: Sequence[str]) -> Optional[str]:
    if RESTART_ARG not in argv[1:]:
        return None
    if list(argv).count(RESTART_ARG) != 1:
        raise ValueError('管理员重启参数重复')
    index = list(argv).index(RESTART_ARG)
    if index + 1 >= len(argv) or not re.fullmatch('[0-9a-f]{64}', argv[index + 1]):
        raise ValueError('管理员重启凭据无效')
    return argv[index + 1]


def strip_restart_args(argv: Sequence[str]) -> list:
    result = list(argv)
    if RESTART_ARG in result:
        index = result.index(RESTART_ARG)
        del result[index:index + 2]
    return result


class RestartTicket:
    def __init__(self, directory: str, token: str):
        if not re.fullmatch('[0-9a-f]{64}', token):
            raise ValueError('管理员重启凭据无效')
        self.directory = normalized_directory(directory)
        self.token = token
        self.path = Path(directory) / RESTART_FILE
        self.ready_path = Path(directory) / f'restart-ready-{token}.json'

    @classmethod
    def create(cls, directory: str, identity: dict) -> 'RestartTicket':
        ticket = cls(directory, secrets.token_hex(32))
        write_json(ticket.path, {
            'version': 1, 'phase': 'request', 'token': ticket.token,
            'directory': ticket.directory, 'parent': identity})
        return ticket

    def read(self) -> dict:
        state = read_json(self.path)
        if (state.get('version') != 1 or state.get('token') != self.token
                or state.get('directory') != self.directory):
            raise ValueError('管理员重启凭据与数据目录不匹配')
        return state

    def ready(self, child_identity: dict) -> bool:
        if not self.ready_path.exists():
            return False
        ready = read_json(self.ready_path)
        return (ready.get('token') == self.token
                and ready.get('child') == child_identity)

    def commit(self, child_identity: dict, payload: dict) -> None:
        state = self.read()
        state.update(phase='committed', child=child_identity, payload=payload)
        write_json(self.path, state)

    def cancel(self) -> None:
        try:
            state = self.read()
        except FileNotFoundError:
            return
        if state.get('phase') != 'request':
            return
        state['phase'] = 'cancelled'
        write_json(self.path, state)

    def cleanup(self) -> None:
        self.ready_path.unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)


def await_parent_exit(directory: str, token: str, *,
                      process_factory=WindowsProcess.open,
                      clock: Callable[[], float] = time.monotonic,
                      sleep: Callable[[float], None] = time.sleep) -> Optional[dict]:
    """此函数在建 QApplication、拿单实例锁和打开库之前调用。"""
    ticket = RestartTicket(directory, token)
    state = ticket.read()
    with process_factory(os.getpid()) as child, process_factory(state['parent']['pid']) as parent:
        child_identity = child.identity().to_dict()
        if parent.identity().to_dict() != state['parent'] or not parent.alive():
            raise ValueError('原实例身份已变化')
        if child_identity['user_sid'] != state['parent']['user_sid']:
            raise ValueError('请使用当前 Windows 用户的管理员授权')
        if not child.is_elevated():
            raise ValueError('新实例未取得管理员权限')
        write_json(ticket.ready_path, {'token': token, 'child': child_identity})
        deadline = clock() + COMMIT_TIMEOUT_S
        while clock() < deadline:
            try:
                state = ticket.read()
            except (FileNotFoundError, ValueError):
                ticket.ready_path.unlink(missing_ok=True)
                return None
            if state.get('phase') == 'cancelled':
                ticket.ready_path.unlink(missing_ok=True)
                return None
            if not parent.alive():
                ticket.ready_path.unlink(missing_ok=True)
                if (state.get('phase') != 'committed'
                        or state.get('child') != child_identity):
                    return None
                return state
            sleep(0.1)
    ticket.ready_path.unlink(missing_ok=True)
    raise TimeoutError('等待原实例保存并退出超时')


def pending_restart(directory: str, *, process_factory=WindowsProcess.open) -> Optional[dict]:
    """交接提交后候选意外退出时，下次正常启动也能接回已保存的任务。"""
    path = Path(directory) / RESTART_FILE
    if not path.exists():
        return None
    state = read_json(path)
    ticket = RestartTicket(directory, state.get('token', ''))
    state = ticket.read()
    if state.get('phase') != 'committed':
        return None
    with process_factory(os.getpid()) as current:
        if current.identity().user_sid != state['parent']['user_sid']:
            raise ValueError('重启交接记录属于其他 Windows 用户')
    try:
        with process_factory(state['parent']['pid']) as parent:
            if parent.alive() and parent.identity().to_dict() == state['parent']:
                return None
    except OSError:
        # 没有进程句柄时还需区分 PID 已退出与访问被拒绝。
        import psutil
        if psutil.pid_exists(state['parent']['pid']):
            return None
    return state
