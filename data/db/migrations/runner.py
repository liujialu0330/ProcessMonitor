"""
迁移运行器

两种迁移路径，结果语义（MigrationOutcome）相同：

重建式（目标 v5，生产路径；见 data.db.migrations.rebuild）：
1. 用不设任何 PRAGMA 的裸连接探测：库里没有 tasks 表（全新库）→ 直接建当前结构
   （UP_TO_DATE，created=True；原路径没有库但留有换库中断的痕迹时例外，见第 6 步）；user_version >= 目标 → UP_TO_DATE（更新的库原样使用，只记警告）
2. 磁盘预检：库所在磁盘的可用空间须 ≥ 旧库大小（含 -wal）× DISK_HEADROOM_RATIO +
   DISK_HEADROOM_BYTES，不足 → BACKUP_ABORTED（报告带 required_bytes / free_bytes），旧库原样保留；
   查询可用空间本身失败只记警告、照常迁移
3. 旧库是 WAL 模式时做一次 wal_checkpoint(TRUNCATE)，让旧文件自包含（回滚日志模式的旧库不做
   任何写入）；checkpoint 被其他连接挡住 → BACKUP_ABORTED，旧库原样保留
4. 在 <库>.migrating 建 v5 结构，以只读 immutable 方式 ATTACH 旧库，SELECT 搬入全部数据；
   失败时删掉新文件再试一次，仍失败 → FAILED_RESTORED
5. 换库：先用 os.link 给旧文件建硬链接 <库>.bak_v{起始版本}（即备份，与旧库是同一份文件，
   逐字节就是迁移前的旧库），再 os.replace 把新文件换到原路径 → MIGRATED。原路径任何时刻都有
   完整的库：换入失败只需删掉多出来的硬链接，FAILED_RESTORED。
   文件系统不支持硬链接时退回"改名"：先写 <库>.swap_pending 标记（旧库名、新库名、备份名、
   时间），旧文件改名为备份、新文件改名为库，换完删除标记。改名式下任何失败之后旧库都回到
   原路径——旧文件改名失败 → 删掉新文件，FAILED_RESTORED；新文件改名失败 → 把旧文件改回原名、
   删掉新文件，FAILED_RESTORED；只有连改回也失败才是 FAILED_KEPT（原路径暂无数据库，数据在
   backup_path，报告的 backup_path 与 broken_path 都指向它；标记与完整的新文件留着，供下次
   启动恢复）。组合根遇到 FAILED_KEPT 不得继续启动：否则连接工厂会在原路径建出空库，用户的
   数据看上去"没了"
   换库成功后，与本次备份是同一份文件（os.path.samefile）的其他 .bak_v* 一并删除：它们是
   上次硬链接换库中断（建了链接、没换成）留下的多余硬链接，不是另一份数据。
   重建式迁移从不改动旧库，所以这里的 FAILED_RESTORED 含义是"迁移没有完成，原路径仍是未被
   改动的旧库"——与就地迁移"已从备份还原"对用户的效果相同：本次运行新数据无法保存，下次
   启动重试。不会出现 RESET。磁盘峰值约为旧库 + 新库。
   只对 sqlite3.OperationalError（锁、IO、磁盘满）与 OSError 重试一次；数据转换等程序性异常
   重试也必然再失败，直接 FAILED_RESTORED。失败报告的 reason 带原因文案（主窗口提示用）。
6. 换库中断的恢复（探测阶段）：原路径没有库（文件不存在或为 0 字节），且存在 <库>.swap_pending
   标记或完整的 <库>.migrating 时才恢复、不建空库——.migrating 完整（能打开、user_version
   为 5、quick_check 通过；重建时 user_version 在全部数据落盘后才写）→ 换到原路径，按 MIGRATED
   报告；否则（有标记）把标记里记的备份（找不到时取修改时间最新的 .bak_v*）改回原路径、重新
   迁移；都不行 → FAILED_KEPT。
   只有裸的 .bak_v*（没有标记、没有完整的 .migrating）不算换库中断：硬链接式换库从不让原路径
   变空，改名式先写标记再改名，这种现场只会来自用户或外部程序删掉了库。此时按全新库启动、
   不自动放回备份，只记一条 WARNING 列出发现的备份文件及手动找回的办法；不完整的 .migrating
   不含可恢复的数据，一并删掉。

就地式（目标 v1–v4，只供测试与 golden 样本生成器截断迁移链，生产路径不再调用）：
1. 同上探测；全新库建 v4 冻结结构
2. 旧库：WAL checkpoint(TRUNCATE) + 文件级备份
   （checkpoint 被其他连接挡住或复制失败 → BACKUP_ABORTED，旧库不做任何写入）
   → 单事务按链执行迁移，末尾统一写 user_version（成功 → MIGRATED）
   → 失败则从备份还原后重试一次（应对磁盘满/文件锁等瞬时故障），仍失败 → FAILED_RESTORED
   → 还原失败但原路径的旧库仍可读（tasks 表在、能读版本号）→ 原样保留 → FAILED_KEPT
   → 还原失败且旧库已不可读：连同 -wal/-shm 改名保留并在原路径新建空库 → RESET；
     改名失败，或改名后新建空库也失败（此时把旧库放回原路径）→ FAILED_KEPT
   FAILED_KEPT 时原路径不建表、不写版本号，下次启动版本号仍是旧值，会重新尝试迁移
   还原是原子的：先把备份复制到 <库>.restore_tmp，复制完整后再 os.replace 到原路径。

第 1 步检测库状态时出错（库被锁、文件损坏等）异常原样抛出、不记入 guard：
这属于启动即失败，交由上层处理，下次启动重新检测。

备份命名：<库>.bak_v{起始版本}；该文件已存在时不覆盖（它可能是更早一次迁移留下的
唯一原始数据），改用 <库>.bak_v{起始版本}_{YYYYmmdd_HHMMSS}[_序号]。

同一库路径每进程只尝试一次迁移：失败时版本号始终不变，若不记住结果，
每个新的 Database() 实例都会重新触发一次迁移。
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from data.db.connection import ConnectionFactory
from data.db.migrations import MIGRATIONS, Migration, rebuild
from data.db.migrations._frozen import create_schema_v4
from data.db.schema import (
    SCHEMA_VERSION, create_current, read_version, table_exists, write_version,
)

logger = logging.getLogger(__name__)

_SIDECARS = ('-wal', '-shm')

# 探测与 checkpoint 用的裸连接等待其他连接让路的秒数（与连接工厂的 busy_timeout 一致）
PROBE_TIMEOUT = 5.0

# 重建式迁移的磁盘预检：可用空间 ≥ 旧库大小 × 比例 + 固定余量。新库是紧凑结构，
# 实测约为旧库的 0.1~0.15 倍，比例取 0.3 留出余量；固定余量覆盖小库、索引与临时文件
DISK_HEADROOM_RATIO = 0.3
DISK_HEADROOM_BYTES = 200 * 1024 * 1024

_MB = 1024 * 1024

# 重建式迁移的临时新库与改名式换库的标记文件（后缀接在库路径后面）
MIGRATING_SUFFIX = '.migrating'
SWAP_PENDING_SUFFIX = '.swap_pending'

# 失败原因文案（MigrationReport.reason；主窗口提示与只读横幅用）
REASON_SOURCE_BUSY = "旧数据库正被其他程序占用"
REASON_SWAP_FAILED = "新数据库无法换到原位置（{error}）"
REASON_IO_ERROR = "读写数据库文件出错（{error}）"
REASON_DATA_ERROR = "旧数据转换出错（{error}）"
REASON_MARKER_FAILED = "无法写入换库标记（{error}）"
REASON_RECOVERY_FAILED = "上次升级中断，未能自动恢复"


def required_free_bytes(db_bytes: int) -> int:
    """迁移 db_bytes 大小的旧库所需的可用空间（字节）"""
    return int(db_bytes * DISK_HEADROOM_RATIO) + DISK_HEADROOM_BYTES


def disk_shortage_message(required_bytes: int, free_bytes: int) -> str:
    """磁盘空间不足时的说明（日志与界面共用）：所需向上取整、当前向下取整到 MB"""
    required_mb = -(-int(required_bytes) // _MB)
    free_mb = max(int(free_bytes), 0) // _MB
    return f"需要约 {required_mb} MB 可用空间（当前 {free_mb} MB），数据库未改动"


class MigrationOutcome(Enum):
    """迁移结果"""
    UP_TO_DATE = 'up_to_date'            # 无需迁移（含全新建库、库版本比目标还新）
    MIGRATED = 'migrated'                # 迁移成功
    BACKUP_ABORTED = 'backup_aborted'    # 迁移前备份失败，已中止，旧库原样保留
    FAILED_RESTORED = 'failed_restored'  # 迁移两次失败，已从备份还原为旧库
    RESET = 'reset'                      # 还原失败且旧库已不可读才重置：旧库改名保留，原路径为新建空库
    # 重建式：旧库没能改回原名，原路径暂无数据库（数据在 backup_path），组合根据此退出；
    # 就地式：还原失败，旧库留在原路径（完好，或改名/建空库失败），下次启动重试
    FAILED_KEPT = 'failed_kept'


@dataclass(frozen=True)
class MigrationReport:
    """一次迁移尝试的结果"""
    outcome: MigrationOutcome
    from_version: int
    to_version: int                     # 结束时原路径库的版本号
    backup_path: Optional[str] = None   # 本次迁移前新建的备份文件（未做备份时为 None）
    # 旧库改名后的路径：RESET 时必有；FAILED_KEPT 时仅当旧库改名后没能放回原路径
    # （原路径此时没有库，数据在这里和 backup_path），其余情况为 None
    broken_path: Optional[str] = None
    created: bool = False               # 本次是否新建了库结构（全新库）
    # 磁盘预检不足而中止（BACKUP_ABORTED）时：所需与当前可用的字节数；其余情况为 None
    required_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    # 重建时因时间戳为 NULL 或无法解析而跳过的数据点行数（MIGRATED 时有意义）
    skipped_rows: int = 0
    # 失败原因文案（失败结果时给出，主窗口提示用）；其余情况为 None
    reason: Optional[str] = None

    @property
    def disk_shortage(self) -> bool:
        """是否因磁盘空间不足而中止"""
        return self.required_bytes is not None


@dataclass(frozen=True)
class ProbeResult:
    """迁移前探测的结果（MigrationRunner.probe）"""
    has_tasks: bool          # 库里有 tasks 表（否则是全新库，迁移时直接建当前结构）
    version: int             # user_version；全新库为 0
    target_version: int      # 本次迁移的目标版本
    file_bytes: int = 0      # 库文件大小（含 -wal）
    estimated_rows: int = 0  # 待搬迁的数据点行数估计（data_points 的最大 rowid，偏大不偏小）

    @property
    def needs_migration(self) -> bool:
        """是否需要迁移（已有库且版本低于目标）"""
        return self.has_tasks and self.version < self.target_version


# 按（库路径, 目标版本）记录本进程内已完成的迁移尝试；只在库仍低于目标版本时复用
_ATTEMPTS: Dict[Tuple[str, int], MigrationReport] = {}


def _guard_key(db_path: str, target_version: int) -> Tuple[str, int]:
    return os.path.normcase(os.path.abspath(db_path)), target_version


def reset_guard_for_tests() -> None:
    """清空进程级迁移记录（测试在用例间调用，避免不同临时库互相影响）"""
    _ATTEMPTS.clear()


def _unique_path(base: str) -> str:
    """base 不存在时原样返回，否则依次尝试 base_2、base_3 ……（不覆盖已有文件）"""
    candidate, n = base, 2
    while os.path.exists(candidate):
        candidate = f'{base}_{n}'
        n += 1
    return candidate


def _timestamp() -> str:
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def _remove_quietly(path: str) -> bool:
    """删除 path（不存在视为成功）；失败只记日志"""
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except OSError:
        logger.error("删除文件失败: %s", path, exc_info=True)
        return False


def _remove_with_sidecars(path: str) -> bool:
    """删除 path 及其 -journal/-wal/-shm（不存在视为成功）"""
    ok = all([_remove_quietly(path + s) for s in ('-journal',) + _SIDECARS])
    return _remove_quietly(path) and ok


def _retryable(exc: BaseException) -> bool:
    """重建失败是否值得重试：锁、IO、磁盘满这类瞬时故障才重试，程序性异常重试必然再失败"""
    return isinstance(exc, (sqlite3.OperationalError, OSError))


def failure_reason(exc: BaseException) -> str:
    """重建失败的原因文案"""
    template = REASON_IO_ERROR if _retryable(exc) else REASON_DATA_ERROR
    return template.format(error=exc)


# 进度回调：(阶段名, 已完成行数, 总行数)
ProgressCallback = Callable[[str, int, int], None]


class _RestoreResult(Enum):
    RESTORED = 'restored'
    RESET = 'reset'
    KEPT = 'kept'


class MigrationRunner:
    """把 factory 指向的库迁移到目标版本"""

    def __init__(self, factory: ConnectionFactory,
                 migrations: Sequence[Migration] = MIGRATIONS,
                 target_version: int = SCHEMA_VERSION,
                 on_progress: Optional[ProgressCallback] = None,
                 disk_usage: Callable[[str], Any] = shutil.disk_usage):
        """
        Args:
            migrations: 就地迁移链（只在 target_version < 5 时使用）
            target_version: 5 走重建式迁移；1–4 走冻结的就地迁移链（测试用）
            on_progress: 重建式迁移搬样本时的进度回调（阶段名, 已完成行数, 总行数），
                每 rebuild.CHUNK_ROWS 行一次；在执行迁移的线程里同步调用，不得抛异常
            disk_usage: 查询目录所在磁盘空间的函数（返回值带 free 属性），默认
                shutil.disk_usage；测试注入替身模拟空间不足
        """
        self._factory = factory
        self._disk_usage = disk_usage
        self.db_path = factory.db_path
        self._target = target_version
        self._rebuild = target_version >= rebuild.VERSION
        self._on_progress = on_progress
        self._migrations: List[Migration] = sorted(migrations, key=lambda m: m.version)
        if self._rebuild:
            if target_version != rebuild.VERSION:
                raise ValueError(f"不支持的目标版本 v{target_version}")
        else:
            versions = [m.version for m in self._migrations if m.version <= target_version]
            if versions != list(range(1, target_version + 1)):
                raise ValueError(
                    f"迁移链不连续或与目标版本不符: {versions} -> v{target_version}")
        # 迁移期间要备份、覆盖、改名库文件，不能有任何常驻句柄（Windows 上打开的文件无法改名）
        self._conn = ConnectionFactory(self.db_path, keep_alive=False)
        self._broken_path: Optional[str] = None

    def _probe(self) -> Tuple[bool, int]:
        """
        用不设任何 PRAGMA 的裸连接读 tasks 表是否存在与 user_version。

        不能用连接工厂：工厂的每个连接都设 journal_mode=WAL，会把回滚日志模式的旧库在备份
        之前就改掉（文件头 18/19 等字节）。文件不存在时不连接（不让 sqlite3 建出空文件）。
        """
        if not os.path.exists(self.db_path):
            return False, 0
        conn = sqlite3.connect(self.db_path, timeout=PROBE_TIMEOUT)
        try:
            if not table_exists(conn, 'tasks'):
                return False, 0
            return True, read_version(conn)
        finally:
            conn.close()

    def probe(self) -> ProbeResult:
        """
        迁移前探测（不做任何写入）：组合根据此判断是否需要迁移、要不要先显示进度窗口。

        Raises:
            sqlite3.Error: 库被锁或文件损坏（与 migrate() 第 1 步相同，交由上层处理）
        """
        has_tasks, version = self._probe()
        source = self.db_path
        if (not has_tasks and self._rebuild and self._db_absent()
                and self._read_swap_marker() is not None):
            # 改名式换库中断：迁移时会把备份放回原路径重新迁移，按备份估计（据此决定要不要
            # 进度窗口）；没有标记的裸备份不恢复（见模块说明第 6 步）
            bak = self._recovery_backup()
            if bak is not None and not self._is_complete_v5(self.db_path + MIGRATING_SUFFIX):
                conn = sqlite3.connect(rebuild.sqlite_uri(bak, mode='ro'), uri=True,
                                       timeout=PROBE_TIMEOUT)
                try:
                    if table_exists(conn, 'tasks'):
                        has_tasks, version, source = True, read_version(conn), bak
                finally:
                    conn.close()
        file_bytes = self._source_bytes(source) if has_tasks else 0
        rows = 0
        if has_tasks and version < self._target:
            conn = sqlite3.connect(rebuild.sqlite_uri(source, mode='ro'), uri=True,
                                   timeout=PROBE_TIMEOUT)
            try:
                if table_exists(conn, 'data_points'):
                    # 最大 rowid 走 B 树最右端，几百万行也是瞬间；删过数据时偏大，只用于估计
                    row = conn.execute('SELECT MAX(rowid) FROM data_points').fetchone()
                    rows = int(row[0] or 0)
            finally:
                conn.close()
        return ProbeResult(has_tasks=has_tasks, version=version,
                           target_version=self._target, file_bytes=file_bytes,
                           estimated_rows=rows)

    def backup_path_for(self, version: int) -> str:
        """从 version 迁移时备份将使用的文件名（进度窗口提前告诉用户；规则见 _new_backup_path）"""
        return self._new_backup_path(version)

    def _source_bytes(self, source: Optional[str] = None) -> int:
        """旧库占用的字节数：主文件 + 未合并的 -wal（checkpoint 后会并入主文件）"""
        source = source or self.db_path
        total = 0
        for path in (source, source + '-wal'):
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total

    def _create_fresh(self) -> None:
        """在原路径建目标版本的空库（已有 tasks 表时不动）"""
        with self._conn.transaction() as cursor:
            if table_exists(cursor, 'tasks'):
                return
            if self._rebuild:
                create_current(cursor)
            else:
                create_schema_v4(cursor)
                write_version(cursor, self._target)

    def migrate(self) -> MigrationReport:
        try:
            has_tasks, version = self._probe()
            if not has_tasks and self._rebuild and self._db_absent():
                recovered = self._recover_interrupted_swap()
                if recovered is not None:
                    return recovered
                has_tasks, version = self._probe()
            if not has_tasks:
                self._create_fresh()
                logger.info("已新建数据库: %s (v%d)", self.db_path, self._target)
                return MigrationReport(MigrationOutcome.UP_TO_DATE, self._target,
                                       self._target, created=True)
        except Exception:
            # 库被锁或文件损坏：无法判断状态，不记入 guard（下次启动重新检测），
            # 但不留下调用方的常驻句柄
            logger.error("检测数据库状态失败: %s", self.db_path, exc_info=True)
            self._factory.close()
            raise

        if version >= self._target:
            if version > self._target:
                logger.warning("数据库版本 v%d 高于程序支持的 v%d，按原样使用: %s",
                               version, self._target, self.db_path)
            return MigrationReport(MigrationOutcome.UP_TO_DATE, version, version)

        key = _guard_key(self.db_path, self._target)
        previous = _ATTEMPTS.get(key)
        if previous is not None:
            logger.warning("本进程已尝试过迁移该数据库，跳过重复迁移，复用既有结果: %s",
                           previous)
            return previous

        report = self._run(version)
        _ATTEMPTS[key] = report
        return report

    # ========== 换库中断的恢复 ==========

    def _db_absent(self) -> bool:
        """原路径没有库：文件不存在或为 0 字节（没有任何表的空文件）"""
        try:
            return os.path.getsize(self.db_path) == 0
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _backup_candidates(self) -> List[str]:
        """<库>.bak_v0 ~ .bak_v4（含带时间戳、序号的变体），按修改时间从新到旧"""
        directory = os.path.dirname(os.path.abspath(self.db_path))
        pattern = re.compile(re.escape(os.path.basename(self.db_path))
                             + r'\.bak_v[0-4](?:_\d{8}_\d{6})?(?:_\d+)?$')
        try:
            names = [n for n in os.listdir(directory) if pattern.match(n)]
        except OSError:
            logger.error("列出数据目录失败: %s", directory, exc_info=True)
            return []
        paths = [os.path.join(directory, n) for n in names]

        def _key(path: str) -> Tuple[float, str]:
            try:
                return os.path.getmtime(path), path
            except OSError:
                return 0.0, path
        return sorted(paths, key=_key, reverse=True)

    def _read_swap_marker(self) -> Optional[Dict[str, Any]]:
        """读改名式换库留下的标记；不存在或读不出时为 None（读不出也按"有中断"处理）"""
        path = self.db_path + SWAP_PENDING_SUFFIX
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            logger.error("换库标记读取失败: %s", path, exc_info=True)
            return {}

    def _recovery_backup(self) -> Optional[str]:
        """恢复时放回原路径的备份：标记里记的那个，否则修改时间最新的 .bak_v*"""
        marker = self._read_swap_marker() or {}
        named = marker.get('backup')
        if isinstance(named, str) and os.path.isfile(named):
            return named
        candidates = self._backup_candidates()
        return candidates[0] if candidates else None

    @staticmethod
    def _is_complete_v5(path: str) -> bool:
        """path 是否是一份完整的 v5 库：能打开、user_version 为 5、有 tasks 表、quick_check 通过"""
        if not os.path.isfile(path):
            return False
        try:
            conn = sqlite3.connect(rebuild.sqlite_uri(path, mode='ro', immutable='1'), uri=True)
            try:
                if read_version(conn) != rebuild.VERSION or not table_exists(conn, 'tasks'):
                    return False
                row = conn.execute('PRAGMA quick_check').fetchone()
                return row is not None and row[0] == 'ok'
            finally:
                conn.close()
        except sqlite3.Error:
            logger.warning("迁移临时文件不完整: %s", path, exc_info=True)
            return False

    def _note_orphan_traces(self, incomplete_migrating: Optional[str]) -> None:
        """
        原路径没有库、也没有换库中断标记：按全新库处理（不自动放回备份，见模块说明第 6 步）。
        不完整的临时文件不含可恢复的数据，删掉；发现的备份只记 WARNING 提示用户手动找回。
        """
        if incomplete_migrating is not None:
            logger.warning("删除不完整的迁移临时文件，按全新库处理: %s", incomplete_migrating)
            _remove_with_sidecars(incomplete_migrating)
        backups = self._backup_candidates()
        if backups:
            logger.warning(
                "原路径没有数据库，也没有换库中断标记，按全新库启动，不自动放回备份。发现旧数据库"
                "备份: %s；如需找回，请退出程序，删除新建的 %s，把其中一个备份改名为 %s 后再启动",
                ', '.join(os.path.basename(p) for p in backups),
                os.path.basename(self.db_path), os.path.basename(self.db_path))

    def _drop_duplicate_backups(self, keep: str) -> None:
        """
        换库成功后删掉与 keep 是同一份文件的其他备份：上次硬链接换库在建链接之后、换入之前
        中断时留下的多余硬链接（与这次的备份同一 inode，不是另一份数据）
        """
        for path in self._backup_candidates():
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(keep)):
                continue
            try:
                same = os.path.samefile(path, keep)
            except OSError:
                continue
            if same and _remove_quietly(path):
                logger.info("已删除与本次备份相同的多余硬链接: %s", path)

    @staticmethod
    def _version_from_backup_name(path: Optional[str]) -> Optional[int]:
        match = re.search(r'\.bak_v(\d)', os.path.basename(path or ''))
        return int(match.group(1)) if match else None

    def _recover_interrupted_swap(self) -> Optional[MigrationReport]:
        """
        原路径没有库时检查上次换库是否中断（规则见模块说明第 6 步）。

        Returns:
            已换入新库（MIGRATED）或无法恢复（FAILED_KEPT）时返回报告；没有中断痕迹、
            或已把备份放回原路径（调用方重新探测后照常迁移）时返回 None
        """
        marker_path = self.db_path + SWAP_PENDING_SUFFIX
        migrating = self.db_path + MIGRATING_SUFFIX
        marker = self._read_swap_marker()
        has_migrating = os.path.exists(migrating)
        complete = has_migrating and self._is_complete_v5(migrating)
        if marker is None and not complete:
            self._note_orphan_traces(migrating if has_migrating else None)
            return None
        bak = self._recovery_backup()
        logger.warning("原路径没有数据库，但留有上次升级的痕迹（标记=%s，临时文件=%s，备份=%s），"
                       "尝试恢复: %s", marker is not None, has_migrating, bak, self.db_path)
        from_version = (marker or {}).get('from_version')
        if not isinstance(from_version, int):
            from_version = self._version_from_backup_name(bak)
        if from_version is None:
            from_version = 0

        if complete:
            try:
                os.replace(migrating, self.db_path)
            except OSError as exc:
                logger.error("完整的新数据库未能换到原路径: %s", migrating, exc_info=True)
                kept = bak or migrating
                return MigrationReport(MigrationOutcome.FAILED_KEPT, from_version, from_version,
                                       backup_path=kept, broken_path=kept,
                                       reason=REASON_SWAP_FAILED.format(error=exc))
            _remove_quietly(marker_path)
            logger.warning("已把上次升级完成的新数据库换到原路径（旧文件保留为 %s）", bak)
            return MigrationReport(MigrationOutcome.MIGRATED, from_version, self._target,
                                   backup_path=bak)

        if bak is None:
            logger.error("上次升级中断且找不到旧数据库的备份，原路径保持为空: %s", self.db_path)
            kept = migrating if has_migrating else None
            return MigrationReport(MigrationOutcome.FAILED_KEPT, from_version, from_version,
                                   backup_path=kept, broken_path=kept,
                                   reason=REASON_RECOVERY_FAILED)
        try:
            os.rename(bak, self.db_path)
        except OSError:
            logger.error("旧数据库备份未能放回原路径，数据保存在: %s", bak, exc_info=True)
            return MigrationReport(MigrationOutcome.FAILED_KEPT, from_version, from_version,
                                   backup_path=bak, broken_path=bak,
                                   reason=REASON_RECOVERY_FAILED)
        _remove_quietly(marker_path)
        if has_migrating:
            _remove_with_sidecars(migrating)
        logger.warning("已把旧数据库备份放回原路径，重新升级: %s -> %s", bak, self.db_path)
        return None

    # ========== 迁移主流程 ==========

    def _run(self, version: int) -> MigrationReport:
        # 调用方的常驻连接会阻止还原/改名，并可能让换上来的库与残留 -wal 不一致
        self._factory.close()
        if self._rebuild:
            return self._run_rebuild(version)
        return self._run_inplace(version)

    # ========== 重建式迁移 ==========

    def _run_rebuild(self, version: int) -> MigrationReport:
        shortage = self._check_disk_space(version)
        if shortage is not None:
            return shortage
        if not self._checkpoint_source():
            logger.error("迁移前无法让旧数据库自包含（WAL checkpoint 未完成），已中止迁移，"
                         "旧数据库保持不变: %s", self.db_path)
            return MigrationReport(MigrationOutcome.BACKUP_ABORTED, version, version,
                                   reason=REASON_SOURCE_BUSY)

        new_path = self.db_path + MIGRATING_SUFFIX
        stats: Optional[rebuild.RebuildStats] = None
        reason = None
        for attempt in (1, 2):
            if not _remove_with_sidecars(new_path):
                logger.error("无法删除残留的迁移临时文件: %s", new_path)
                reason = REASON_IO_ERROR.format(error=f"无法删除 {new_path}")
                break
            try:
                started = time.perf_counter()
                stats = rebuild.rebuild_into(new_path, self.db_path, version,
                                             on_progress=self._on_progress)
                logger.info("重建式迁移完成: v%d -> v%d，任务 %d、读入 %d 行、样本 %d、"
                            "打点 %d、跳过时间戳无效的行 %d，用时 %.2f 秒", version, self._target,
                            stats.tasks, stats.rows_read, stats.samples, stats.markers,
                            stats.skipped_rows, time.perf_counter() - started)
                break
            except Exception as exc:
                logger.error("重建式迁移失败（第%d次）", attempt, exc_info=True)
                _remove_with_sidecars(new_path)
                reason = failure_reason(exc)
                if not _retryable(exc):
                    break
        if stats is None:
            logger.error("数据库迁移失败，旧数据库未被改动，本次运行新数据无法保存，"
                         "下次启动重试：%s", reason)
            return MigrationReport(MigrationOutcome.FAILED_RESTORED, version, version,
                                   reason=reason)
        report = self._swap_in(new_path, version)
        if stats.skipped_rows and report.outcome is MigrationOutcome.MIGRATED:
            report = replace(report, skipped_rows=stats.skipped_rows)
        return report

    def _check_disk_space(self, version: int) -> Optional[MigrationReport]:
        """
        磁盘预检：可用空间不足时返回 BACKUP_ABORTED 报告（此时尚未做任何写入，旧库原样），
        足够时返回 None。查询可用空间失败不阻止迁移：只记警告，按旧行为继续。
        """
        required = required_free_bytes(self._source_bytes())
        directory = os.path.dirname(os.path.abspath(self.db_path))
        try:
            free = int(self._disk_usage(directory).free)
        except Exception:
            logger.warning("查询磁盘可用空间失败，跳过迁移前的空间预检: %s", directory,
                           exc_info=True)
            return None
        if free >= required:
            return None
        message = disk_shortage_message(required, free)
        logger.error("磁盘空间不足，已中止数据库升级：%s（%s）", message, self.db_path)
        return MigrationReport(MigrationOutcome.BACKUP_ABORTED, version, version,
                               required_bytes=required, free_bytes=free,
                               reason=f"磁盘空间不足，{message}")

    def _checkpoint_source(self) -> bool:
        """
        WAL 模式的旧库做一次 wal_checkpoint(TRUNCATE)，把未合并的提交写回主文件；
        回滚日志模式的旧库不做任何写入。用裸连接（不切日志模式）；被挡住或出错返回 False。
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=PROBE_TIMEOUT)
            try:
                mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
                if str(mode).lower() != 'wal':
                    return True
                row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.error("旧数据库 checkpoint 失败: %s", self.db_path, exc_info=True)
            return False
        if row is None or row[0] != 0:
            logger.error("旧数据库 checkpoint 被其他连接阻挡: %s, 结果=%s", self.db_path,
                         tuple(row) if row is not None else None)
            return False
        return True

    def _clear_source_sidecars(self) -> bool:
        """
        改名旧库前确认它没有残留日志：checkpoint 后裸连接关闭时 SQLite 已删除 -wal/-shm，
        这里只清理意外残留的空 -wal 与 -shm。-wal 非空或存在 -journal 说明有其他进程在写，
        返回 False——不能换库：残留日志会被当成新库的日志回放。
        """
        wal = self.db_path + '-wal'
        if os.path.exists(self.db_path + '-journal'):
            logger.error("旧数据库存在回滚日志，可能有其他进程正在写入: %s", self.db_path)
            return False
        try:
            if os.path.exists(wal) and os.path.getsize(wal) > 0:
                logger.error("旧数据库的 -wal 非空，可能有其他进程正在写入: %s", wal)
                return False
        except OSError:
            logger.error("检查旧数据库 -wal 失败: %s", wal, exc_info=True)
            return False
        return _remove_quietly(wal) and _remove_quietly(self.db_path + '-shm')

    def _swap_in(self, new_path: str, version: int) -> MigrationReport:
        """
        把新文件换到原路径，旧文件留作备份（见模块说明第 5 步）。

        首选"硬链接备份 + os.replace"：两步之间任何时刻原路径都是完整的库（先是旧库，
        替换后是新库），中途崩溃不会让原路径变空。不支持硬链接时退回改名式。
        """
        if not self._clear_source_sidecars():
            _remove_with_sidecars(new_path)
            return MigrationReport(MigrationOutcome.FAILED_RESTORED, version, version,
                                   reason=REASON_SOURCE_BUSY)
        backup_path = self._new_backup_path(version)
        try:
            os.link(self.db_path, backup_path)
        except (OSError, AttributeError, NotImplementedError):
            logger.warning("无法为旧数据库建立硬链接备份，改用改名方式换库: %s", backup_path,
                           exc_info=True)
            return self._swap_by_rename(new_path, backup_path, version)
        try:
            os.replace(new_path, self.db_path)
        except OSError as exc:
            logger.error("新数据库换到原路径失败，旧数据库保持不变: %s", new_path, exc_info=True)
            # 原路径仍是旧库；多出来的硬链接与它是同一份文件，删不掉也无害
            _remove_quietly(backup_path)
            _remove_with_sidecars(new_path)
            return MigrationReport(MigrationOutcome.FAILED_RESTORED, version, version,
                                   reason=REASON_SWAP_FAILED.format(error=exc))
        logger.info("数据库迁移完成: v%s -> v%s，旧库已保留为 %s", version, self._target,
                    backup_path)
        self._drop_duplicate_backups(backup_path)
        return MigrationReport(MigrationOutcome.MIGRATED, version, self._target,
                               backup_path=backup_path)

    def _write_swap_marker(self, new_path: str, backup_path: str, version: int) -> None:
        """改名式换库前写标记（落盘后才改名）：中途崩溃时下次启动据此恢复"""
        path = self.db_path + SWAP_PENDING_SUFFIX
        data = {'old': self.db_path, 'new': new_path, 'backup': backup_path,
                'from_version': version, 'time': datetime.now().isoformat()}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

    def _swap_by_rename(self, new_path: str, backup_path: str, version: int) -> MigrationReport:
        """
        改名式换库：标记 → 旧文件改名为备份 → 新文件改名为库 → 删标记。

        不变式：任何失败之后旧库都回到原路径。新文件换不进来时先把旧库改回原名、再清理
        新文件：清理失败不影响旧库归位；旧库归位失败时报告带上它的去向（FAILED_KEPT 的
        backup_path / broken_path），标记与完整的新文件留给下次启动恢复。
        """
        marker = self.db_path + SWAP_PENDING_SUFFIX
        try:
            self._write_swap_marker(new_path, backup_path, version)
        except OSError as exc:
            logger.error("写入换库标记失败，已放弃迁移，旧数据库保持不变: %s", marker,
                         exc_info=True)
            _remove_quietly(marker)
            _remove_with_sidecars(new_path)
            return MigrationReport(MigrationOutcome.FAILED_RESTORED, version, version,
                                   reason=REASON_MARKER_FAILED.format(error=exc))
        try:
            os.rename(self.db_path, backup_path)
        except OSError as exc:
            logger.error("旧数据库改名为备份失败，已放弃迁移，旧数据库保持不变: %s",
                         self.db_path, exc_info=True)
            _remove_quietly(marker)
            _remove_with_sidecars(new_path)
            return MigrationReport(MigrationOutcome.FAILED_RESTORED, version, version,
                                   reason=REASON_SWAP_FAILED.format(error=exc))
        try:
            os.replace(new_path, self.db_path)
        except OSError as exc:
            logger.error("新数据库改名失败: %s", new_path, exc_info=True)
            reason = REASON_SWAP_FAILED.format(error=exc)
            try:
                os.rename(backup_path, self.db_path)
            except OSError:
                logger.error("旧数据库未能改回原名，原路径暂无数据库，数据保存在: %s"
                             "（下次启动会按换库标记恢复）", backup_path, exc_info=True)
                return MigrationReport(MigrationOutcome.FAILED_KEPT, version, version,
                                       backup_path=backup_path, broken_path=backup_path,
                                       reason=reason)
            _remove_quietly(marker)
            _remove_with_sidecars(new_path)
            logger.error("已把旧数据库改回原名，本次运行新数据无法保存，下次启动重试迁移")
            return MigrationReport(MigrationOutcome.FAILED_RESTORED, version, version,
                                   reason=reason)
        _remove_quietly(marker)
        logger.info("数据库迁移完成: v%s -> v%s，旧库已保留为 %s", version, self._target,
                    backup_path)
        self._drop_duplicate_backups(backup_path)
        return MigrationReport(MigrationOutcome.MIGRATED, version, self._target,
                               backup_path=backup_path)

    # ========== 就地迁移（冻结链，只供测试与样本生成器） ==========

    def _run_inplace(self, version: int) -> MigrationReport:
        backup_path = self._new_backup_path(version)
        if not self._backup(backup_path):
            logger.error("迁移前备份失败，已中止迁移，旧数据库保持不变: %s", self.db_path)
            return MigrationReport(MigrationOutcome.BACKUP_ABORTED, version, version)

        restore = _RestoreResult.RESTORED
        for attempt in (1, 2):
            try:
                # 单事务执行迁移（含 DDL），整条链任一步失败整体回滚
                with self._conn.transaction() as cursor:
                    for migration in self._migrations:
                        if version < migration.version <= self._target:
                            migration.apply(cursor)
                    write_version(cursor, self._target)
                logger.info("数据库迁移完成: v%s -> v%s", version, self._target)
                return MigrationReport(MigrationOutcome.MIGRATED, version, self._target,
                                       backup_path=backup_path)
            except Exception:
                logger.error("数据库迁移失败（第%d次）", attempt, exc_info=True)
                restore = self._restore_from_backup(backup_path)
                if restore is not _RestoreResult.RESTORED:
                    # 没能还原出旧库（已重置为空库，或旧库原样留着）：原路径已不是
                    # 迁移起点的那个旧库，不能再拿旧版本号重跑迁移链
                    break

        if restore is _RestoreResult.RESET:
            return MigrationReport(MigrationOutcome.RESET, version, self._target,
                                   backup_path=backup_path, broken_path=self._broken_path)
        if restore is _RestoreResult.KEPT:
            return MigrationReport(MigrationOutcome.FAILED_KEPT, version, version,
                                   backup_path=backup_path, broken_path=self._broken_path)
        logger.error("数据库迁移两次尝试均失败，已还原旧数据，本次运行新数据无法保存")
        return MigrationReport(MigrationOutcome.FAILED_RESTORED, version, version,
                               backup_path=backup_path)

    def _new_backup_path(self, version: int) -> str:
        """
        本次备份的文件名：<库>.bak_v{version}；已存在时不覆盖（可能是更早一次迁移留下的
        唯一原始数据），改用 <库>.bak_v{version}_{YYYYmmdd_HHMMSS}[_序号]
        """
        base = f'{self.db_path}.bak_v{version}'
        if not os.path.exists(base):
            return base
        return _unique_path(f'{base}_{_timestamp()}')

    def _backup(self, backup_path: str) -> bool:
        """
        迁移前对旧库做文件级备份：先 WAL checkpoint(TRUNCATE) 把 -wal 中的提交合并进主文件，
        再 copy2 拷贝主文件。checkpoint 被其他连接挡住时 -wal 里可能还有未合并的提交，
        只拷主文件会漏数据，因此按备份失败处理。拷贝中途失败时删掉不完整的备份文件。
        """
        if not self._conn.checkpoint(truncate=True):
            logger.error("迁移前 WAL checkpoint 未完成，备份可能缺少最新数据: %s", self.db_path)
            return False
        try:
            shutil.copy2(self.db_path, backup_path)
            logger.info("已备份数据库: %s", backup_path)
            return True
        except Exception:
            logger.error("迁移前备份失败", exc_info=True)
            # backup_path 是本次新选的不存在的文件名，残留的只可能是本次写了一半的副本
            _remove_quietly(backup_path)
            return False

    def _restore_from_backup(self, backup_path: str) -> _RestoreResult:
        """
        迁移失败后从备份还原。

        还原失败但旧库仍可读时原样保留（KEPT）。
        旧库已不可读时把它连同 -wal/-shm 改名保留，再在原路径新建空库（RESET）。
        改名也失败时绝不在原路径建表或写 user_version：否则旧表结构会被标成当前版本，
        以后永不再迁移、新数据永久写不进去；此时原样留下旧库（KEPT），
        下次启动版本号仍是旧值，会重新尝试迁移。
        新建空库失败（如磁盘满）时同理：删掉建了一半的空库并把旧库放回原路径（KEPT）。
        若不放回，原路径为空，下次启动会被当成全新安装悄悄建出空库，用户无从得知旧数据去向。
        """
        if self._replace_from_backup(backup_path):
            logger.warning("已从备份还原数据库: %s", backup_path)
            return _RestoreResult.RESTORED

        # 还原是原子的，失败时原路径仍是迁移事务回滚后的旧库，通常完好：
        # 此时原样保留比改名后换成空库更好，用户数据照常可见，下次启动重试
        if self._db_still_readable():
            logger.error("迁移失败且未能从备份还原，但旧数据完好，已原样保留，"
                         "本次运行新数据无法保存，下次启动重试迁移: %s", self.db_path)
            return _RestoreResult.KEPT

        if not self._move_aside_broken_db():
            logger.error("损坏的数据库无法改名，保持原样不新建空库，本次运行新数据无法保存: %s",
                         self.db_path)
            return _RestoreResult.KEPT

        # 原路径已腾空，新建空库（目标版本的结构）
        try:
            self._create_fresh()
        except Exception:
            logger.error("在原路径新建空库失败: %s", self.db_path, exc_info=True)
            if self._discard_new_db() and self._move_back_broken_db():
                logger.error("已把旧数据库放回原路径，本次运行新数据无法保存，"
                             "下次启动将重新尝试迁移: %s", self.db_path)
            else:
                logger.error("旧数据库未能放回原路径，数据保存在: %s（迁移前备份: %s）",
                             self._broken_path, backup_path)
            return _RestoreResult.KEPT
        logger.warning("数据库已重置为空库: %s", self.db_path)
        return _RestoreResult.RESET

    def _db_still_readable(self) -> bool:
        """
        原路径上的库是否仍可读：tasks 表存在且能读出 user_version。
        用不经工厂的裸连接（不开常驻连接、探测完立即关闭，不妨碍之后改名库文件）并设
        query_only：工厂连接会设 journal_mode=WAL，对 0 字节的空文件这会写入库文件头，
        探测本身就改了现场。文件不存在时直接视为不可读（不让 sqlite3.connect 建出空文件）。
        """
        if not os.path.exists(self.db_path):
            return False
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute('PRAGMA query_only=1')
                if not table_exists(conn, 'tasks'):
                    return False
                read_version(conn)
            finally:
                conn.close()
            return True
        except Exception:
            logger.error("旧数据库已不可读: %s", self.db_path, exc_info=True)
            return False

    def _replace_from_backup(self, backup_path: str) -> bool:
        """
        原子地用备份替换原路径的库：复制到同目录临时文件，完整后再 os.replace。
        直接 copy2 到原路径会先截断再写，中途失败就把已回滚的完好旧库写坏。

        Returns:
            bool: 是否已替换；失败时临时文件已删除，原路径的库未被触碰
        """
        tmp_path = self.db_path + '.restore_tmp'
        try:
            shutil.copy2(backup_path, tmp_path)
            os.replace(tmp_path, self.db_path)
            return True
        except Exception:
            logger.error("从备份还原失败: %s", backup_path, exc_info=True)
            _remove_quietly(tmp_path)
            return False

    def _move_aside_broken_db(self) -> bool:
        """
        把损坏库连同 -wal/-shm 改名为 <库>.broken_<时间戳>[_序号][-wal|-shm] 保留。

        伴随文件必须一起挪走：残留的旧 -wal 会被 SQLite 当成新建空库的日志回放。
        先挪伴随文件、最后挪主库；主库挪不动时把已挪走的伴随文件放回，保持旧库原样。

        Returns:
            bool: 主库是否已挪走（原路径已腾空）；成功时 _broken_path 记录新路径
        """
        broken_path = self.db_path + '.broken_' + _timestamp()
        candidate, n = broken_path, 2
        while any(os.path.exists(candidate + s) for s in ('',) + _SIDECARS):
            candidate = f'{broken_path}_{n}'
            n += 1
        broken_path = candidate

        moved: List[str] = []
        try:
            for suffix in _SIDECARS + ('',):
                src = self.db_path + suffix
                if suffix and not os.path.exists(src):
                    continue
                os.replace(src, broken_path + suffix)
                moved.append(suffix)
        except Exception:
            logger.error("保留损坏数据库失败", exc_info=True)
            for suffix in moved:
                try:
                    os.replace(broken_path + suffix, self.db_path + suffix)
                except Exception:
                    logger.error("放回数据库伴随文件失败: %s", broken_path + suffix,
                                 exc_info=True)
            return False
        self._broken_path = broken_path
        logger.warning("损坏的数据库已改名保留: %s", broken_path)
        return True

    def _discard_new_db(self) -> bool:
        """
        删掉原路径上新建失败的空库（连同 -wal/-shm），为放回旧库腾出位置。
        只删 0 字节或没有任何表的文件，防止误删别处在这期间建出的真实数据库。

        Returns:
            bool: 原路径及伴随文件是否已清空
        """
        if os.path.exists(self.db_path) and os.path.getsize(self.db_path) > 0:
            try:
                conn = sqlite3.connect(self.db_path)
                try:
                    has_tables = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
                    ).fetchone() is not None
                finally:
                    conn.close()
            except sqlite3.Error:
                logger.error("无法确认原路径上的库是否为空，不删除: %s", self.db_path,
                             exc_info=True)
                return False
            if has_tables:
                logger.error("原路径上的库已有表，不删除: %s", self.db_path)
                return False
        # 伴随文件先删，主库最后删：主库删不掉时伴随文件已无意义
        ok = all([_remove_quietly(self.db_path + s) for s in _SIDECARS])
        return _remove_quietly(self.db_path) and ok

    def _move_back_broken_db(self) -> bool:
        """
        把 _move_aside_broken_db 挪走的旧库连同伴随文件放回原路径。
        先放主库：主库放不回时伴随文件留在 broken 路径，与主库保持在一起。
        （迁移期间所有连接都已关闭，SQLite 关闭最后一个连接时已合并并删除 -wal，
        伴随文件通常不存在；放回失败只记日志）

        Returns:
            bool: 主库是否已放回原路径；成功时清空 _broken_path
        """
        broken_path = self._broken_path
        if broken_path is None:
            return False
        try:
            os.replace(broken_path, self.db_path)
        except Exception:
            logger.error("放回旧数据库失败: %s", broken_path, exc_info=True)
            return False
        for suffix in _SIDECARS:
            src = broken_path + suffix
            if not os.path.exists(src):
                continue
            try:
                os.replace(src, self.db_path + suffix)
            except Exception:
                logger.error("放回数据库伴随文件失败: %s", src, exc_info=True)
        self._broken_path = None
        return True
