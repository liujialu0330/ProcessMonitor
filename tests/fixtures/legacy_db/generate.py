"""
真实旧库样本生成器（只在手动运行时执行；测试只读本目录的 *.db.gz 与 manifest.json）

做法：
1. 对每个已发布 tag，用 `git archive <tag>` 只导出写库所需的源码（config.py、data/ 下的
   database.py / models.py / __init__.py）到临时目录；
2. 在子进程里以 `python -I` 运行一段写库脚本：导入该版本自己的 data.database.Database，
   按固定场景（见 build_scenario）用该版本的接口写库，写完 checkpoint + VACUUM；
3. 把库 gzip（mtime=0，便于重复生成时比较）为 `<tag>_v<schema>.db.gz`；
4. v2 / v3 两个从未发布的中间版本，用当前仓库的冻结迁移链（只到 v2 / v3）对 v1.4.1 样本
   截断迁移得到；v3 样本再按 v3 结构补写备注、打点任务（v3 起才有这两种数据）；
5. 期望计数由本脚本按场景独立推算（不运行被测迁移链），写入 manifest.json。

数据目录隔离：旧版 config.py 在非打包环境把数据目录定为"config.py 所在目录/data"，
即临时导出目录下的 data\\；写库脚本在写入前断言：导入的 config / data.database 来自临时
导出目录、config.DATA_DIR 与目标库路径都不在本仓库内，否则立即退出。旧版代码绝不在
仓库目录里运行，也就不会碰到项目根 data\\ 下的用户数据。

用法（在项目根目录）：
    python tests/fixtures/legacy_db/generate.py [--workdir 临时目录] [--keep]

新增 tag：在 TAG_SAMPLES 里加一行（tag、schema 版本、写库接口风格），确认该版本
data/database.py 的写库接口与已有风格一致，重新运行本脚本并提交产物。
"""
import argparse
import gzip
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(FIXTURE_DIR)))
MANIFEST_PATH = os.path.join(FIXTURE_DIR, 'manifest.json')

# 单份压缩样本的大小上限
MAX_GZ_BYTES = 200 * 1024

# 已发布 tag 的样本：(tag, 该版本库的 user_version, 写库接口风格)
# - 'single'：v1.0.x，MonitorTask.metric_type 单值、DataPoint 无 metric_type（一任务一指标）
# - 'multi' ：v1.1.0 起，MonitorTask.metric_types 列表、DataPoint.metric_type
# v1.0.5 的 data/database.py 与 v1.0.6 逐字节相同（只差 config.py 的数据目录位置），
# v1.3.0 / v1.4.0 与 v1.4.1 相同，故不单独出样本。
TAG_SAMPLES: List[Tuple[str, int, str]] = [
    ('v1.0.6', 0, 'single'),
    ('v1.1.0', 1, 'multi'),
    ('v1.2.0', 1, 'multi'),
    ('v1.4.1', 1, 'multi'),
]

# 由截断迁移链派生的中间版本样本：(目标版本, 基础样本 tag)
DERIVED_SAMPLES: List[Tuple[int, str]] = [(2, 'v1.4.1'), (3, 'v1.4.1')]

# 导出的最小源码集合（这些版本的 database.py 只依赖 config 与 data.models）
ARCHIVE_PATHS = ['config.py', 'data/__init__.py', 'data/database.py', 'data/models.py']

# 1.x 时代的重复内存指标：Windows 上三者取值相同，v2 迁移合并为 memory_private
MEMORY_FAMILY = ('memory_private', 'memory_vms', 'memory_pagefile')
LEGACY_ALIAS = {'memory_vms': 'memory_private', 'memory_pagefile': 'memory_private'}

TIME_FMT_NOTE = '时间戳一律带微秒（与旧版 datetime.now().isoformat() 的形态一致）'


# ========== 场景 ==========

def _series(n: int, base: datetime, value_fns: Dict[str, callable]):
    """生成 n 个采样周期的数据点：每周期每个指标一行，同一周期时间戳相同"""
    points = []
    for i in range(n):
        ts = (base + timedelta(seconds=i)).isoformat()
        for metric, fn in value_fns.items():
            points.append((ts, metric, float(fn(i))))
    return points


def _leak(i):
    """私有字节/虚拟内存/提交大小（三者同值）：缓慢上涨，模拟泄漏"""
    return 80000.0 + i * 16.0 + (i % 7) * 3.0


def _rss(i):
    return 60000.0 + i * 4.0 + (i % 5)


def _handles(i):
    return 400 + i // 20


TASK_A_BASE = datetime(2025, 6, 1, 9, 0, 0, 250000)
TASK_B_BASE = datetime(2025, 6, 2, 14, 30, 0, 500000)
TASK_C_START = datetime(2025, 6, 3, 8, 0, 0, 750000)
TASK_D_BASE = datetime(2025, 6, 4, 10, 0, 0, 125000)

A_POINTS = 600
B_POINTS = 200
D_POINTS = 120

PROCESS_A = '泄漏复现_客户端.exe'
PROCESS_B = '崩溃遗留_服务.exe'
PROCESS_C = '空任务_工具.exe'
PROCESS_D = '打点任务_编辑器.exe'
NOTE_A = '第二轮复现：打开 20 个文档后关闭'


def build_scenario(style: str) -> dict:
    """
    固定场景（与版本无关的部分在这里写死，保证每次生成结果一致）

    - 任务 A：已结束，5 个指标（含重复的 memory_vms / memory_private / memory_pagefile），
      600 个采样周期，中文进程名。v1.0.x 一任务只能一个指标，按当年用法拆成 5 个同进程、
      同时间戳的单指标任务（golden-a 是其中的 memory_vms 任务）；
    - 任务 B：status 仍为 running（模拟异常退出未收尾），200 个周期，指标含 memory_pagefile；
    - 任务 C：已结束的空任务，0 个点；
    - 任务 D（带打点、备注）：已发布版本都不支持，只在派生的 v3 样本里补写（见 add_v3_extras）。
    """
    a_metrics = OrderedDict([
        ('memory_rss', _rss), ('memory_vms', _leak), ('memory_private', _leak),
        ('memory_pagefile', _leak), ('num_handles', _handles),
    ])
    a_end = (TASK_A_BASE + timedelta(seconds=A_POINTS)).isoformat()
    tasks: List[dict] = []
    points: Dict[str, list] = {}

    if style == 'multi':
        tasks.append(dict(task_id='golden-a', pid=4242, process_name=PROCESS_A,
                          metrics=list(a_metrics), interval=1.0,
                          start_time=TASK_A_BASE.isoformat(), end_time=a_end,
                          status='stopped'))
        points['golden-a'] = _series(A_POINTS, TASK_A_BASE, a_metrics)
    else:
        suffix = {'memory_rss': '-rss', 'memory_vms': '', 'memory_private': '-private',
                  'memory_pagefile': '-pagefile', 'num_handles': '-handles'}
        for metric, fn in a_metrics.items():
            task_id = 'golden-a' + suffix[metric]
            tasks.append(dict(task_id=task_id, pid=4242, process_name=PROCESS_A,
                              metrics=[metric], interval=1.0,
                              start_time=TASK_A_BASE.isoformat(), end_time=a_end,
                              status='stopped'))
            points[task_id] = _series(A_POINTS, TASK_A_BASE, {metric: fn})

    b_metrics = (OrderedDict([('memory_pagefile', lambda i: 120000.0 + i * 2.0),
                              ('num_handles', lambda i: 900 - (i % 3))])
                 if style == 'multi'
                 else OrderedDict([('memory_pagefile', lambda i: 120000.0 + i * 2.0)]))
    tasks.append(dict(task_id='golden-b', pid=5151, process_name=PROCESS_B,
                      metrics=list(b_metrics), interval=1.0,
                      start_time=TASK_B_BASE.isoformat(), end_time=None, status='running'))
    points['golden-b'] = _series(B_POINTS, TASK_B_BASE, b_metrics)

    c_metrics = ['memory_vms', 'cpu_percent'] if style == 'multi' else ['memory_vms']
    tasks.append(dict(task_id='golden-c', pid=6161, process_name=PROCESS_C,
                      metrics=c_metrics, interval=2.0,
                      start_time=TASK_C_START.isoformat(),
                      end_time=(TASK_C_START + timedelta(seconds=5)).isoformat(),
                      status='stopped'))
    points['golden-c'] = []
    return {'tasks': tasks, 'points': points}


# ========== 期望值（独立推算，不运行被测迁移链） ==========

def expected_after_migration(scenario: dict, notes: Optional[dict] = None,
                             markers: Optional[dict] = None) -> dict:
    """
    按现有迁移链的口径推算迁到当前版本、并做启动校正后的期望值。

    去重口径（v2 迁移）：同一任务、同一时间戳下 memory_private / memory_vms /
    memory_pagefile 三者只保留一行并统一命名为 memory_private（不跨任务合并）；
    任务指标列表做同样改名并保持原顺序去重。sample_count = 不同时间戳数（v3 回填），
    last_sample_at = 最大时间戳。遗留 running 任务经 reconcile 变为 stopped，
    end_time 取 last_sample_at。
    """
    notes = notes or {}
    markers = markers or {}
    result = OrderedDict()
    for task in scenario['tasks']:
        task_id = task['task_id']
        rows = scenario['points'][task_id]
        single = len(task['metrics']) == 1

        written: Dict[str, int] = OrderedDict()
        for _ts, metric, _v in rows:
            key = task['metrics'][0] if single else metric
            written[key] = written.get(key, 0) + 1

        kept: Dict[Tuple[str, str], float] = OrderedDict()
        for ts, metric, value in rows:
            metric = task['metrics'][0] if single else metric
            metric = LEGACY_ALIAS.get(metric, metric)
            kept.setdefault((ts, metric), value)
        after: Dict[str, int] = OrderedDict()
        sums: Dict[str, float] = OrderedDict()
        for (_ts, metric), value in kept.items():
            after[metric] = after.get(metric, 0) + 1
            sums[metric] = sums.get(metric, 0.0) + value

        metric_types_after: List[str] = []
        for metric in task['metrics']:
            metric = LEGACY_ALIAS.get(metric, metric)
            if metric not in metric_types_after:
                metric_types_after.append(metric)

        timestamps = sorted({ts for ts, _m, _v in rows})
        last = timestamps[-1] if timestamps else None
        if task['status'] == 'running':
            end_after = last
        else:
            end_after = task['end_time']
        result[task_id] = OrderedDict([
            ('process_name', task['process_name']),
            ('status_before', task['status']),
            ('status_after_reconcile', 'stopped'),
            ('metric_types_before', task['metrics']),
            ('metric_types_after', metric_types_after),
            ('rows_written_by_metric', written),
            ('rows_after_by_metric', after),
            ('value_sum_after_by_metric', sums),
            ('sample_count_after', len(timestamps)),
            ('last_sample_at', last),
            ('end_time_after_reconcile', end_after),
            ('note_after', notes.get(task_id, '')),
            ('markers_after', markers.get(task_id, 0)),
        ])
    return result


def _totals(tasks: dict) -> dict:
    written = sum(sum(t['rows_written_by_metric'].values()) for t in tasks.values())
    after = sum(sum(t['rows_after_by_metric'].values()) for t in tasks.values())
    return OrderedDict([
        ('tasks', len(tasks)),
        ('running_tasks', sum(1 for t in tasks.values() if t['status_before'] == 'running')),
        ('rows_written', written),
        ('rows_after', after),
        ('duplicate_rows_removed', written - after),
        ('markers_after', sum(t['markers_after'] for t in tasks.values())),
    ])


# ========== 旧版代码子进程 ==========

# 在子进程里运行（python -I，不读用户 site-packages 与环境变量，不把脚本目录放进 sys.path）
CHILD_SCRIPT = r'''
import json, os, sqlite3, sys
from datetime import datetime

src_root, db_path, spec_path, forbidden_root = sys.argv[1:5]
src_root = os.path.realpath(src_root)
forbidden_root = os.path.realpath(forbidden_root)


def inside(path, root):
    path = os.path.realpath(path)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


sys.path.insert(0, src_root)
import config
import data.database as database_module
from data.models import MonitorTask, DataPoint

# 数据目录隔离自检：任一条件不满足立即退出，绝不写库
for module in (config, database_module):
    if not inside(module.__file__, src_root):
        sys.exit('导入了临时导出目录以外的模块: %s' % module.__file__)
for path in (config.DATA_DIR, config.DB_PATH, db_path):
    if inside(path, forbidden_root):
        sys.exit('数据路径落在仓库内，拒绝写库: %s' % path)
if not inside(config.DATA_DIR, src_root):
    sys.exit('旧版数据目录不在临时导出目录内: %s' % config.DATA_DIR)

with open(spec_path, encoding='utf-8') as f:
    spec = json.load(f)
multi = 'metric_types' in MonitorTask.__dataclass_fields__

db = database_module.Database(db_path)
for task in spec['tasks']:
    kwargs = dict(task_id=task['task_id'], pid=task['pid'],
                  process_name=task['process_name'], interval=task['interval'],
                  start_time=datetime.fromisoformat(task['start_time']),
                  end_time=None, status='running')
    if multi:
        kwargs['metric_types'] = task['metrics']
    else:
        kwargs['metric_type'] = task['metrics'][0]
    assert db.save_task(MonitorTask(**kwargs)), task['task_id']

    batch = []
    for ts, metric, value in spec['points'][task['task_id']]:
        if multi:
            batch.append(DataPoint(task_id=task['task_id'],
                                   timestamp=datetime.fromisoformat(ts),
                                   value=value, metric_type=metric))
        else:
            batch.append(DataPoint(task_id=task['task_id'],
                                   timestamp=datetime.fromisoformat(ts), value=value))
        if len(batch) >= 250:
            assert db.save_data_points(batch)
            batch = []
    if batch:
        assert db.save_data_points(batch)

    if task['status'] == 'stopped':
        assert db.update_task_status(task['task_id'], 'stopped',
                                     datetime.fromisoformat(task['end_time']))
del db

conn = sqlite3.connect(db_path)
try:
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    conn.execute('VACUUM')
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    has_metric_col = any(r[1] == 'metric_type'
                         for r in conn.execute('PRAGMA table_info(data_points)'))
    if has_metric_col:
        rows = conn.execute('SELECT task_id, metric_type, COUNT(*) FROM data_points '
                            'GROUP BY task_id, metric_type').fetchall()
    else:
        rows = conn.execute('SELECT task_id, NULL, COUNT(*) FROM data_points '
                            'GROUP BY task_id').fetchall()
    report = {
        'user_version': conn.execute('PRAGMA user_version').fetchone()[0],
        'journal_mode': conn.execute('PRAGMA journal_mode').fetchone()[0],
        'sqlite_version': sqlite3.sqlite_version,
        'objects': [list(r) for r in conn.execute(
            'SELECT type, name FROM sqlite_master ORDER BY type, name')],
        'rows': [list(r) for r in rows],
        'tasks': [list(r) for r in conn.execute(
            'SELECT task_id, metric_type, status, end_time FROM tasks ORDER BY task_id')],
    }
finally:
    conn.close()
print(json.dumps(report, ensure_ascii=True))
'''


def _git(*args: str) -> bytes:
    return subprocess.run(['git', *args], cwd=PROJECT_ROOT, check=True,
                          capture_output=True).stdout


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def export_tag(tag: str, dest: str) -> str:
    """git archive 导出该 tag 的最小源码到 dest，返回来源 commit"""
    commit = _git('rev-parse', f'{tag}^{{commit}}').decode().strip()
    tar_bytes = _git('archive', '--format=tar', tag, '--', *ARCHIVE_PATHS)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        tar.extractall(dest, filter='data')
    return commit


def check_metric_keys(tag: str, scenario: dict) -> None:
    """确认场景用到的指标键名在该版本的 utils/metrics.py 中确实存在"""
    text = _git('show', f'{tag}:utils/metrics.py').decode('utf-8')
    used = {m for t in scenario['tasks'] for m in t['metrics']}
    missing = sorted(m for m in used if f'"{m}"' not in text)
    if missing:
        raise SystemExit(f'{tag} 没有这些指标键: {missing}')


def run_legacy_writer(src_root: str, db_path: str, scenario: dict) -> dict:
    """在子进程里用旧版 Database 写库，返回子进程报告"""
    spec_path = os.path.join(os.path.dirname(db_path), 'spec.json')
    with open(spec_path, 'w', encoding='utf-8') as f:
        json.dump(scenario, f, ensure_ascii=False)
    env = dict(os.environ)
    # 旧版不认这个变量；设成临时目录只是多一道保险
    env['PROCESS_MONITOR_DATA_DIR'] = os.path.dirname(db_path)
    proc = subprocess.run(
        [sys.executable, '-I', '-c', CHILD_SCRIPT, src_root, db_path, spec_path, PROJECT_ROOT],
        cwd=os.path.dirname(db_path), env=env, capture_output=True, text=True,
        encoding='utf-8')
    if proc.returncode != 0:
        raise SystemExit(f'旧版写库失败:\n{proc.stdout}\n{proc.stderr}')
    return json.loads(proc.stdout.strip().splitlines()[-1])


def verify_written(report: dict, scenario: dict, schema: int) -> None:
    """核对子进程实际写入与场景一致"""
    if report['user_version'] != schema:
        raise SystemExit(f'user_version={report["user_version"]}，期望 {schema}')
    want: Dict[Tuple[str, Optional[str]], int] = {}
    for task in scenario['tasks']:
        for _ts, metric, _v in scenario['points'][task['task_id']]:
            key = (task['task_id'], None if schema == 0 else metric)
            want[key] = want.get(key, 0) + 1
    got = {(r[0], r[1]): r[2] for r in report['rows']}
    if got != want:
        raise SystemExit(f'写入行数不符: {got} != {want}')


def gzip_to(src: str, dest: str) -> int:
    """确定性 gzip（mtime=0、不记文件名），返回压缩后字节数"""
    with open(src, 'rb') as f:
        raw = f.read()
    with open(dest, 'wb') as out:
        with gzip.GzipFile(filename='', mode='wb', fileobj=out, mtime=0,
                           compresslevel=9) as gz:
            gz.write(raw)
    size = os.path.getsize(dest)
    if size >= MAX_GZ_BYTES:
        raise SystemExit(f'{os.path.basename(dest)} 压缩后 {size} 字节，超过上限')
    return size


def _assert_no_sidecars(db_path: str) -> None:
    for suffix in ('-wal', '-shm', '-journal'):
        if os.path.exists(db_path + suffix):
            raise SystemExit(f'写库后残留 {db_path + suffix}，库文件可能不完整')


def _raw_counts(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return {
            'user_version': conn.execute('PRAGMA user_version').fetchone()[0],
            'journal_mode': conn.execute('PRAGMA journal_mode').fetchone()[0],
        }
    finally:
        conn.close()


# ========== 派生样本（v2 / v3） ==========

def add_v3_extras(db_path: str) -> Tuple[dict, dict, dict]:
    """
    按 v3 结构补写 v3 时代才有的数据：任务 A 的中文备注，任务 D（带 3 个打点）。
    sample_count / last_sample_at 按 v3 应用的写法同步维护。

    Returns:
        (任务 D 的场景片段, notes, markers 计数)
    """
    d_metrics = OrderedDict([('memory_private', lambda i: 50000.0 + i * 8.0),
                             ('num_handles', lambda i: 300 + i // 10)])
    rows = _series(D_POINTS, TASK_D_BASE, d_metrics)
    end = (TASK_D_BASE + timedelta(seconds=D_POINTS)).isoformat()
    last = rows[-1][0]
    task_d = dict(task_id='golden-d', pid=7171, process_name=PROCESS_D,
                  metrics=list(d_metrics), interval=1.0,
                  start_time=TASK_D_BASE.isoformat(), end_time=end, status='stopped')
    marker_rows = [
        ((TASK_D_BASE + timedelta(seconds=10)).isoformat(), '第1轮开始'),
        ((TASK_D_BASE + timedelta(seconds=60)).isoformat(), '第2轮开始'),
        ((TASK_D_BASE + timedelta(seconds=100)).isoformat(), '操作完成'),
    ]
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute('UPDATE tasks SET note = ? WHERE task_id = ?', (NOTE_A, 'golden-a'))
            conn.execute(
                'INSERT INTO tasks (task_id, pid, process_name, metric_type, interval, '
                'start_time, end_time, status, note, sample_count, last_sample_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('golden-d', 7171, PROCESS_D, json.dumps(list(d_metrics)), 1.0,
                 TASK_D_BASE.isoformat(), end, 'stopped', '', D_POINTS, last))
            conn.executemany(
                'INSERT INTO data_points (task_id, timestamp, value, metric_type) '
                'VALUES (?, ?, ?, ?)', [('golden-d', ts, v, m) for ts, m, v in rows])
            conn.executemany(
                'INSERT INTO task_markers (task_id, timestamp, label, created_at) '
                'VALUES (?, ?, ?, ?)',
                [('golden-d', ts, label, ts) for ts, label in marker_rows])
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('VACUUM')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        conn.close()
    return ({'task': task_d, 'points': rows}, {'golden-a': NOTE_A},
            {'golden-d': len(marker_rows)})


def derive_sample(base_db: str, target: int, work: str) -> str:
    """用当前仓库的冻结迁移链只迁到 target 版本，返回派生库路径"""
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    from data.db.connection import ConnectionFactory
    from data.db.migrations import MIGRATIONS
    from data.db.migrations.runner import MigrationOutcome, MigrationRunner

    os.makedirs(work, exist_ok=True)
    db_path = os.path.join(work, 'monitor.db')
    shutil.copy2(base_db, db_path)
    factory = ConnectionFactory(db_path)
    report = MigrationRunner(factory, migrations=[m for m in MIGRATIONS if m.version <= target],
                             target_version=target).migrate()
    factory.close()
    if report.outcome is not MigrationOutcome.MIGRATED:
        raise SystemExit(f'派生 v{target} 失败: {report}')
    os.remove(report.backup_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('VACUUM')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        conn.close()
    return db_path


# ========== 主流程 ==========

def main() -> None:
    parser = argparse.ArgumentParser(description='生成真实旧库样本（golden 库）')
    parser.add_argument('--workdir', help='临时工作目录（默认新建于系统临时目录）')
    parser.add_argument('--keep', action='store_true', help='保留临时工作目录')
    args = parser.parse_args()

    workdir = os.path.realpath(args.workdir or tempfile.mkdtemp(prefix='legacy_db_gen_'))
    if _inside(workdir, PROJECT_ROOT):
        raise SystemExit(f'工作目录不能在仓库内: {workdir}')
    os.makedirs(workdir, exist_ok=True)

    today = date.today().isoformat()
    samples = []
    base_paths: Dict[str, str] = {}
    base_scenarios: Dict[str, dict] = {}
    try:
        for tag, schema, style in TAG_SAMPLES:
            tag_dir = os.path.join(workdir, tag)
            src_root = os.path.join(tag_dir, 'src')
            out_dir = os.path.join(tag_dir, 'out')
            os.makedirs(src_root)
            os.makedirs(out_dir)
            commit = export_tag(tag, src_root)
            scenario = build_scenario(style)
            check_metric_keys(tag, scenario)
            db_path = os.path.join(out_dir, 'monitor.db')
            report = run_legacy_writer(src_root, db_path, scenario)
            verify_written(report, scenario, schema)
            _assert_no_sidecars(db_path)

            file_name = f'{tag}_v{schema}.db.gz'
            gz_size = gzip_to(db_path, os.path.join(FIXTURE_DIR, file_name))
            tasks = expected_after_migration(scenario)
            samples.append(OrderedDict([
                ('file', file_name),
                ('tag', tag),
                ('commit', commit),
                ('schema_version', schema),
                ('generated_on', today),
                ('origin', f'git archive {tag}（{", ".join(ARCHIVE_PATHS)}），'
                           f'子进程 python -I 用该版本 data.database.Database 写库'),
                ('write_api', style),
                ('journal_mode', report['journal_mode']),
                ('sqlite_version', report['sqlite_version']),
                ('db_bytes', os.path.getsize(db_path)),
                ('gz_bytes', gz_size),
                ('scenario', _scenario_text(style, extras=False)),
                ('history_task_id', 'golden-a'),
                ('history_metric', 'memory_private'),
                ('totals', _totals(tasks)),
                ('tasks', tasks),
            ]))
            base_paths[tag] = db_path
            base_scenarios[tag] = scenario
            print(f'{file_name}: {os.path.getsize(db_path)} -> {gz_size} 字节')

        for target, base_tag in DERIVED_SAMPLES:
            db_path = derive_sample(base_paths[base_tag], target,
                                    os.path.join(workdir, f'derived_v{target}'))
            scenario = json.loads(json.dumps(base_scenarios[base_tag]))
            notes, markers = {}, {}
            if target == 3:
                extra, notes, markers = add_v3_extras(db_path)
                scenario['tasks'].append(extra['task'])
                scenario['points'][extra['task']['task_id']] = extra['points']
            _assert_no_sidecars(db_path)
            file_name = f'derived_v{target}_from_{base_tag}.db.gz'
            gz_size = gzip_to(db_path, os.path.join(FIXTURE_DIR, file_name))
            tasks = expected_after_migration(scenario, notes, markers)
            raw = _raw_counts(db_path)
            if raw['user_version'] != target:
                raise SystemExit(f'派生库版本 {raw["user_version"]} != {target}')
            samples.append(OrderedDict([
                ('file', file_name),
                ('tag', None),
                ('commit', _git('rev-parse', 'HEAD').decode().strip()),
                ('schema_version', target),
                ('generated_on', today),
                ('origin', f'{base_tag} 样本经当前仓库 data/db/migrations 冻结迁移链'
                           f'（v1..v{target}）截断迁移'
                           + ('，再按 v3 结构补写备注与打点任务' if target == 3 else '')),
                ('write_api', 'derived'),
                ('journal_mode', raw['journal_mode']),
                ('sqlite_version', sqlite3.sqlite_version),
                ('db_bytes', os.path.getsize(db_path)),
                ('gz_bytes', gz_size),
                ('scenario', _scenario_text('multi', extras=(target == 3))),
                ('history_task_id', 'golden-a'),
                ('history_metric', 'memory_private'),
                ('totals', _totals(tasks)),
                ('tasks', tasks),
            ]))
            print(f'{file_name}: {os.path.getsize(db_path)} -> {gz_size} 字节')
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f'临时工作目录已保留: {workdir}')

    manifest = OrderedDict([
        ('description', '真实旧库样本清单；由 generate.py 生成，勿手改。'
                        '期望值按现有迁移链口径独立推算（v2 去重：同任务同时间戳的三项重复'
                        '内存指标合并为一行 memory_private；不跨任务合并）。' + TIME_FMT_NOTE),
        ('samples', samples),
    ])
    with open(MANIFEST_PATH, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f'已写入 {MANIFEST_PATH}')


def _scenario_text(style: str, extras: bool) -> str:
    if style == 'single':
        text = (f'任务 A：一任务一指标（v1.0.x 用法），同一进程 {PROCESS_A} 拆成 5 个已结束任务'
                f'（memory_rss / memory_vms[golden-a] / memory_private / memory_pagefile / '
                f'num_handles），各 {A_POINTS} 个点、时间戳相同；'
                f'任务 B：running 遗留，memory_pagefile，{B_POINTS} 个点；'
                f'任务 C：已结束空任务（memory_vms）')
    else:
        text = (f'任务 A：已结束，{PROCESS_A}，5 指标（memory_rss / memory_vms / memory_private / '
                f'memory_pagefile / num_handles），{A_POINTS} 个周期；'
                f'任务 B：running 遗留，memory_pagefile + num_handles，{B_POINTS} 个周期；'
                f'任务 C：已结束空任务（memory_vms + cpu_percent）')
    if extras:
        text += (f'；v3 补写：任务 A 中文备注；任务 D：{PROCESS_D}，memory_private + num_handles，'
                 f'{D_POINTS} 个周期，3 个打点')
    return text + '。该版本无备注/打点功能时不写。'


if __name__ == '__main__':
    main()
