"""
数据层错误类型

仓储层遇到 sqlite3 异常时统一抛 StorageError（原始异常挂在 __cause__ 上），
是否降级（显示"读取数据失败"、重试、忽略）由调用方决定，仓储本身不吞异常。
"""


class StorageError(Exception):
    """数据库读写失败（连接、锁、磁盘、损坏等）。"""


class MigrationError(StorageError):
    """数据库迁移失败。"""
