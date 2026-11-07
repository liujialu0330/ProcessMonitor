"""
机器校准微基准：用一段固定的纯 Python + SQLite 工作量估计"这台机器比基线机器慢多少"。

工作量刻意与被测代码同类（解释器字典/列表操作 + sqlite3 批量插入与聚合），而不是
纯 CPU 循环，这样换算出的系数对数据库类用例更贴切。取 5 次的中位数，抗偶发抖动。

运行时系数 = 本机校准值 / budgets.json 里记录的基线机器校准值；预算 × 系数即本机预算。

可单独运行查看本机校准值：python tests/perf/calibrate.py
"""
import sqlite3
import statistics
import time

# 工作量参数是校准口径的一部分：改动后必须用 --perf-update-budgets 重新回填基线
DICT_OPS = 200_000
SQLITE_ROWS = 50_000
ROUNDS = 5


def _python_work() -> int:
    """字典写入/查找 + 列表追加/排序：解释器层面的典型开销"""
    table = {}
    items = []
    for i in range(DICT_OPS):
        key = f"k{i % 5000}"
        table[key] = table.get(key, 0) + i
        items.append(i ^ 0x5A5A)
    items.sort()
    return len(table) + items[len(items) // 2]


def _sqlite_work() -> int:
    """内存库：建表、带索引批量插入、聚合查询"""
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, k TEXT, ts TEXT, v REAL)')
        conn.execute('CREATE INDEX idx_t ON t (k, ts)')
        rows = [(f"m{i % 5}", f"2026-01-01T00:{(i // 60) % 60:02d}:{i % 60:02d}.{i:06d}",
                 i * 0.5) for i in range(SQLITE_ROWS)]
        with conn:
            conn.executemany('INSERT INTO t (k, ts, v) VALUES (?, ?, ?)', rows)
        return conn.execute('SELECT k, COUNT(*), AVG(v) FROM t GROUP BY k').fetchall()[0][1]
    finally:
        conn.close()


def run_once() -> float:
    """执行一轮校准工作量，返回耗时（毫秒）"""
    start = time.perf_counter()
    _python_work()
    _sqlite_work()
    return (time.perf_counter() - start) * 1000


def calibrate_ms(rounds: int = ROUNDS) -> float:
    """校准值：rounds 轮耗时的中位数（毫秒）。先空跑一轮预热（导入、内存分配）"""
    run_once()
    return statistics.median(run_once() for _ in range(rounds))


if __name__ == '__main__':
    print(f"本机校准值: {calibrate_ms():.1f} ms")
