"""
仓储共用的异常转换：sqlite3.Error → StorageError

只转换 sqlite3.Error；参数类型错误（TypeError/ValueError）属于调用方编程错误，原样抛出，
不能伪装成"存储故障"。
"""
import sqlite3
from types import TracebackType
from typing import Optional, Type

from data.errors import StorageError


class storage_errors:
    """上下文管理器：块内抛出的 sqlite3.Error 改抛为 StorageError（原异常挂在 __cause__）。

    用类而非 @contextmanager 实现，便于包在生成器的 yield 外层（GeneratorExit 不受影响）。
    """

    def __init__(self, action: str):
        self._action = action

    def __enter__(self) -> 'storage_errors':
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]],
                 exc: Optional[BaseException],
                 tb: Optional[TracebackType]) -> bool:
        if exc is not None and isinstance(exc, sqlite3.Error):
            raise StorageError(f"{self._action}失败: {exc}") from exc
        return False
