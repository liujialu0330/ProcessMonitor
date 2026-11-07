"""
数据层错误类型

仓储层遇到 sqlite3 异常时统一抛 StorageError（原始异常挂在 __cause__ 上），
是否降级（显示"读取数据失败"、重试、忽略）由调用方决定，仓储本身不吞异常。
"""


class StorageError(Exception):
    """数据库读写失败（连接、锁、磁盘、损坏等）。"""


class StorageFullError(StorageError):
    """磁盘已满：SQLite 报 SQLITE_FULL（"database or disk is full"），或 SQLITE_IOERR 族里
    有明确证据表明是磁盘写满。仍是 StorageError 的子类，现有的 except StorageError 照常捕获；
    需要"保留缓冲并暂停、等空间回升"的调用方先捕获它。"""
