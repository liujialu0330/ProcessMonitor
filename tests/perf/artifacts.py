"""性能证据的原子写入与跨进程锁，不依赖第三方锁库。"""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import tempfile
import time


def write_json(path: Path, data: dict, *, lock_dir: Path = None) -> None:
    """每次写入使用独立临时文件；失败不会覆盖现有完整结果。"""
    path = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        # Windows上同时替换同一目标也可能返回共享冲突；唯一临时名并不能替代提交锁。
        lock_root = path.parent / '.locks' if lock_dir is None else Path(lock_dir)
        with file_lock(lock_root / (path.name + '.write.lock')):
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if os.name != 'nt' or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def file_lock(path: Path, timeout_s: float = 300.0):
    """锁由操作系统持有，持有者异常退出也会释放；锁文件本身不要删除。

    删除锁文件会让不同进程锁住不同的文件对象，失去互斥，所以保留空文件。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        # Windows允许锁定EOF之后的字节。不要在加锁前初始化写入：另一持有者
        # 已锁定字节0时，延迟flush会因区域锁失败，连等待锁的循环都进不去。
        if os.name == 'nt':
            import msvcrt

            def acquire():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'等待性能作业锁超时：{path}') from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            release()
