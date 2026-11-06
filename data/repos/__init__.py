"""
数据层仓储

每个仓储只持有一个连接工厂（提供 connect()/transaction() 两个上下文管理器与 db_path），
按表划分职责；sqlite3 异常统一包成 data.errors.StorageError 向上抛，是否降级由调用方决定。
各仓储请按完整模块路径导入（如 from data.repos.task_repo import TaskRepo）。
"""
