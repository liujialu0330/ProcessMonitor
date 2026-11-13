"""
性能基准用的合成库生成器。

当前版本库一律经仓储接口写入（db.tasks.save / db.points.append / db.markers.add），
不手写 SQL：存储结构升级（如 v5）后生成器无需改动，生成的仍是"生产代码写出来的库"。
PointRepo.append 本身就是批量事务（executemany + 同事务维护 sample_count），
按 BATCH_CYCLES 个周期一批调用即可。

旧版（v1）库只能手工构造：表结构照 1.4.1 实际建库语句（与 tests/test_migration_v2.py
的构造方式一致，另补上 1.4.1 存在、v3 迁移会删除的两个旧索引），用于测迁移吞吐。

场景与参数见 TaskSpec；生成结果确定（固定随机种子），缓存键由参数、生成器版本与
SCHEMA_VERSION 共同决定（见 tests/perf/conftest.py）。
"""
import hashlib
import json
import random
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import List

from data.database import Database
from data.mappers import to_ms
from data.models import DataPoint, TaskRecord

# 生成逻辑（数值模型、时间戳规则、打点位置）变化时加一，使旧缓存失效
SYNTH_VERSION = 3   # 本地日期转 UTC 的时区语义纳入缓存身份

# 泄漏排查预设的 5 项指标
LEAK_METRICS = ('memory_private', 'num_handles', 'num_gdi_objects',
                'num_user_objects', 'num_threads')
# 合成任务的起点（生产时间戳带微秒，这里也带，字符串长度与真实库一致）
T0 = datetime(2026, 11, 1, 9, 0, 0, 123456)
# 每批写入的周期数（× 指标数 = 每次 append 的行数）
BATCH_CYCLES = 10_000


def time_identity(duration_s: float) -> dict:
    """缓存中的整数时间戳由本地日期转换而来，换时区后不能沿用旧 UTC 样本。

    同时记录起止时刻和每个覆盖日期的 UTC 映射，覆盖跨夏令时的场景；不修改系统时区。
    """
    days = max(0, int(duration_s // 86400))
    dates = [T0 + timedelta(days=day) for day in range(days + 1)]
    return {
        'local_start': T0.isoformat(),
        'utc_start_ms': to_ms(T0),
        'utc_end_ms': to_ms(T0 + timedelta(seconds=duration_s)),
        'daily_utc_ms': [to_ms(day) for day in dates],
        'timezone_names': list(time.tzname),
    }


@dataclass(frozen=True)
class TaskSpec:
    """
    单任务合成场景。

    - duration_s：任务时长（秒），周期数 = duration_s / interval；
    - hole_every_s / hole_len_s：每隔 hole_every_s 秒出现一个 hole_len_s 秒的空洞
      （模拟暂停或休眠）；hole_every_s 为 0 表示没有空洞；
    - rounds：基线之后的用户打点轮数（0 表示不打点，也不打基线）。
    """
    name: str
    duration_s: int
    interval: float = 1.0
    hole_every_s: int = 3 * 3600
    hole_len_s: int = 120
    rounds: int = 6
    seed: int = 20261106
    metrics: tuple = LEAK_METRICS

    @property
    def task_id(self) -> str:
        return f"perf-{self.name}"

    def params(self) -> dict:
        return {**asdict(self), 'synth_version': SYNTH_VERSION,
                'time_identity': time_identity(self.duration_s)}

    def digest(self) -> str:
        raw = json.dumps(self.params(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]


def days(n: float, name: str, **kwargs) -> TaskSpec:
    return TaskSpec(name=name, duration_s=int(n * 86400), **kwargs)


def _in_hole(second: int, spec: TaskSpec) -> bool:
    return (spec.hole_every_s > 0 and second >= spec.hole_every_s
            and second % spec.hole_every_s < spec.hole_len_s)


def expected_holes(spec: TaskSpec) -> int:
    """场景中的空洞个数（第一个空洞从第 hole_every_s 秒开始）"""
    if spec.hole_every_s <= 0:
        return 0
    return (spec.duration_s - 1) // spec.hole_every_s


def _values(rng: random.Random, k: int, metrics) -> List[float]:
    """第 k 个周期的各指标值：缓慢上涨 + 噪声（量级接近真实泄漏进程）"""
    hours = k / 3600.0
    model = {
        'memory_private': lambda: 120_000 + 35.0 * hours + rng.uniform(-200, 200),
        'num_handles': lambda: float(int(800 + 1.2 * hours + rng.randint(-4, 4))),
        'num_gdi_objects': lambda: float(int(150 + 0.4 * hours + rng.randint(-2, 2))),
        'num_user_objects': lambda: float(int(70 + 0.2 * hours + rng.randint(-1, 1))),
        'num_threads': lambda: float(32 + rng.randint(-3, 3)),
    }
    return [model[m]() if m in model else rng.uniform(0, 100) for m in metrics]


def build_task_db(path: str, spec: TaskSpec) -> dict:
    """
    在 path 新建当前版本库并写入一个合成任务（含基线与 spec.rounds 轮打点）。

    Returns:
        dict: 生成摘要（任务 ID、周期数、行数、空洞数、耗时秒数）
    """
    started = time.perf_counter()
    rng = random.Random(spec.seed)
    db = Database(path)
    try:
        cycles = int(spec.duration_s / spec.interval)
        end = T0 + timedelta(seconds=cycles * spec.interval)
        db.tasks.save(TaskRecord(
            task_id=spec.task_id, pid=4242, process_name='leaky.exe',
            metric_types=list(spec.metrics), interval=spec.interval,
            start_time=T0, end_time=end, status='stopped',
            note=f'性能基准合成任务 {spec.name}'))

        written = 0
        batch: List[DataPoint] = []
        for k in range(cycles):
            second = int(k * spec.interval)
            if _in_hole(second, spec):
                continue
            # 微秒抖动模拟真实采集时刻，保持严格递增
            ts = T0 + timedelta(seconds=k * spec.interval,
                                microseconds=(k * 7919) % 3000)
            for metric, value in zip(spec.metrics, _values(rng, k, spec.metrics)):
                batch.append(DataPoint(spec.task_id, ts, value, metric))
            if len(batch) >= BATCH_CYCLES * len(spec.metrics):
                db.points.append(batch)
                written += len(batch)
                batch = []
        if batch:
            db.points.append(batch)
            written += len(batch)

        if spec.rounds > 0:
            # 基线在开始后 30 分钟（预热之后），各轮均匀分布在其后的时段
            baseline_at = T0 + timedelta(minutes=30)
            db.markers.add(spec.task_id, baseline_at, '基线', 'baseline')
            step = (end - baseline_at) / (spec.rounds + 1)
            for i in range(1, spec.rounds + 1):
                db.markers.add(spec.task_id, baseline_at + step * i, f'第 {i} 轮', 'user')
    finally:
        # 关闭常驻连接：最后一个连接关闭时 SQLite 合并并删除 -wal，缓存只剩一个文件
        db.close()
    return {
        'task_id': spec.task_id,
        'cycles': cycles,
        'rows': written,
        'holes': expected_holes(spec),
        'seconds': round(time.perf_counter() - started, 2),
        'time_identity': time_identity(spec.duration_s),
    }


# ---------------------------------------------------------------- v1 旧库


@dataclass(frozen=True)
class LegacySpec:
    """
    v1（1.4.1）旧库场景：tasks 个任务平分 rows 行，每任务 5 个指标、1 秒周期。

    指标含 memory_vms，使 v1→v2 的"合并重复内存指标"走实际搬迁与去重路径。
    """
    name: str = 'v1'
    rows: int = 1_000_000
    tasks: int = 4
    seed: int = 20261106
    metrics: tuple = ('memory_rss', 'memory_vms', 'memory_private', 'num_handles',
                      'num_threads')

    def params(self) -> dict:
        per_task = self.rows // self.tasks // len(self.metrics)
        span = max(0, self.tasks - 1) * 86400 + per_task
        return {**asdict(self), 'synth_version': SYNTH_VERSION, 'schema_version': 1,
                'time_identity': time_identity(span)}

    def digest(self) -> str:
        raw = json.dumps(self.params(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]


def build_v1_db(path: str, spec: LegacySpec) -> dict:
    """手工构造 v1 库（user_version=1）。返回生成摘要。"""
    started = time.perf_counter()
    rng = random.Random(spec.seed)
    conn = sqlite3.connect(path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, pid INTEGER NOT NULL,
                process_name TEXT NOT NULL, metric_type TEXT NOT NULL,
                interval REAL NOT NULL, start_time TEXT NOT NULL,
                end_time TEXT, status TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE data_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL, value REAL NOT NULL, metric_type TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            )
        ''')
        cursor.execute('CREATE INDEX idx_data_points_task_id ON data_points(task_id)')
        cursor.execute('CREATE INDEX idx_data_points_timestamp ON data_points(timestamp)')
        cursor.execute(
            'CREATE INDEX idx_data_points_task_metric ON data_points(task_id, metric_type)')

        per_task = spec.rows // spec.tasks // len(spec.metrics)
        written = 0
        for t in range(spec.tasks):
            task_id = f"legacy-{t}"
            start = T0 + timedelta(days=t)
            cursor.execute(
                "INSERT INTO tasks VALUES (?, ?, 'old.exe', ?, 1.0, ?, ?, 'stopped')",
                (task_id, 1000 + t, json.dumps(list(spec.metrics)), start.isoformat(),
                 (start + timedelta(seconds=per_task)).isoformat()))
            rows = []
            for k in range(per_task):
                ts = (start + timedelta(seconds=k, microseconds=(k * 7919) % 3000)).isoformat()
                base = 50_000 + k * 0.02
                for metric in spec.metrics:
                    rows.append((task_id, ts, base + rng.uniform(-100, 100), metric))
                if len(rows) >= 100_000:
                    cursor.executemany(
                        'INSERT INTO data_points (task_id, timestamp, value, metric_type) '
                        'VALUES (?, ?, ?, ?)', rows)
                    written += len(rows)
                    rows = []
            if rows:
                cursor.executemany(
                    'INSERT INTO data_points (task_id, timestamp, value, metric_type) '
                    'VALUES (?, ?, ?, ?)', rows)
                written += len(rows)
        cursor.execute('PRAGMA user_version = 1')
        conn.commit()
    finally:
        conn.close()
    return {'rows': written, 'tasks': spec.tasks,
            'seconds': round(time.perf_counter() - started, 2),
            'time_identity': spec.params()['time_identity']}
