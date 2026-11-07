"""
工具异常退出后的续采

上次运行被强杀、崩溃或断电（见 core.session_lock）时，库里仍是 running 的任务有两种命运：
- 被测进程还活着（创建时间与记录一致）：可以接着采，由用户决定（"继续监控"对话框，无窗口
  模式不弹窗，按启动参数决定）；续采沿用原任务 ID、开始时间与已有数据，中断期间是数据空洞，
  任务里留一条"续采"系统打点；
- 其余（被测进程已退出、PID 已被别的程序占用）：照常由启动校正收尾（end_time 取最后一次采样）。

本模块只管"找出可续采的任务"与"按用户的选择执行"；界面（对话框）与时机（启动后何时询问）
在组合根与 ui 层。按进程名监控的任务没有单一被测进程，只要还是 running 就可续采。
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from core.session_lock import ProbeIndeterminate, ProcessProbe, probe_process
from data.models import TARGET_NAME

logger = logging.getLogger(__name__)

# 与 MonitorWorker 启动时的 PID 复用比对同一容差：这里放过去而线程里判为复用，任务会白白失败
CREATE_TIME_TOLERANCE_S = 0.01


@dataclass(frozen=True)
class RecoveryCandidate:
    """一个可续采的任务（对话框的一行）。"""
    task_id: str
    process_name: str
    pid: int
    target_kind: str
    start_time: Optional[datetime]
    last_sample_at: Optional[datetime]
    note: str = ''


@dataclass(frozen=True)
class RecoveryOutcome:
    """执行续采决定的结果：续采成功、放弃（已按孤儿任务收尾）、续采失败（同样已收尾）的任务 ID。"""
    resumed: Tuple[str, ...] = ()
    abandoned: Tuple[str, ...] = ()
    failed: Tuple[str, ...] = ()


def find_recoverable(db, *, probe: ProcessProbe = probe_process) -> List[RecoveryCandidate]:
    """库里 running 的任务中，被测进程仍存活且创建时间一致的（按开始时间升序）。

    读库失败返回空列表（随后的启动校正照常收尾）。"""
    try:
        running = db.tasks.list_running()
    except Exception:
        logger.error("读取遗留的运行中任务失败，不提供续采", exc_info=True)
        return []
    candidates: List[RecoveryCandidate] = []
    for task in running:
        if not _target_alive(task, probe):
            continue
        candidates.append(RecoveryCandidate(
            task_id=task.task_id, process_name=task.process_name, pid=task.pid,
            target_kind=task.target_kind, start_time=task.start_time,
            last_sample_at=task.last_sample_at, note=task.note or ''))
    if candidates:
        logger.info("发现 %d 个可续采的任务: %s", len(candidates),
                    ", ".join(c.task_id[:8] for c in candidates))
    return candidates


def _target_alive(task, probe: ProcessProbe) -> bool:
    if task.target_kind == TARGET_NAME:
        return True
    try:
        found = probe(task.pid)
    except ProbeIndeterminate:
        # 无法确认被测进程在不在：不续采（续采要的是"确定还活着、还是同一个"）
        logger.info("无法确认任务 %s 的被测进程 (PID %s) 是否仍在运行，不提供续采",
                    task.task_id[:8], task.pid)
        return False
    if found is None:
        return False
    create_time, name = found
    # 进程名一定要对得上：名字不同必然不是原来的进程（手动输入 PID、没有创建时间可比的任务，
    # PID 被别的程序占用时全靠它拦住）
    if not _same_name(task.process_name, name):
        return False
    if task.create_time is None or create_time is None:
        return True    # 创建时间无从比对：名字一致就算，线程启动后再做它自己的检查
    return abs(create_time - task.create_time) <= CREATE_TIME_TOLERANCE_S


def _same_name(expected: Optional[str], actual: Optional[str]) -> bool:
    """进程名是否一致（不区分大小写）；任一方未知时无从比对，按一致处理。"""
    if not expected or not actual:
        return True
    return expected.lower() == actual.lower()


def apply_decision(manager, db, candidates: Sequence[RecoveryCandidate],
                   selected_ids: Iterable[str], *, auto_marker=None) -> RecoveryOutcome:
    """执行用户的选择：选中的任务续采，其余（放弃的，以及续采失败的）按孤儿任务收尾。

    Args:
        manager: MonitorManager（recover_task 重建采集线程）
        db: Database（读方案、校正放弃的任务）
        candidates: find_recoverable 的结果
        selected_ids: 用户选择续采的任务 ID（不在 candidates 里的忽略）
        auto_marker: 自动打点服务（可选）：续采成功后按库里的方案重新挂接；停止时长从任务原来
            的开始时间算起，定时打点与阶段快照的节奏从续采时刻起
    """
    selected = set(selected_ids)
    resumed: List[str] = []
    failed: List[str] = []
    abandoned: List[str] = []
    for candidate in candidates:
        if candidate.task_id not in selected:
            abandoned.append(candidate.task_id)
            continue
        try:
            ok = manager.recover_task(candidate.task_id)
        except Exception:
            logger.error("续采任务失败: task_id=%s", candidate.task_id, exc_info=True)
            ok = False
        if ok:
            # 方案放在续采成功之后才挂：续采失败的任务马上被收尾，挂了方案也没人解除跟踪
            try:
                _attach_plan(db, auto_marker, candidate)
            except Exception:
                logger.error("续采任务挂接自动化方案失败（任务照常续采）: task_id=%s",
                             candidate.task_id, exc_info=True)
        (resumed if ok else failed).append(candidate.task_id)
    leftover = abandoned + failed
    if leftover:
        db.reconcile_orphan_tasks(only=leftover)
    return RecoveryOutcome(tuple(resumed), tuple(abandoned), tuple(failed))


def close_all(db, candidates: Sequence[RecoveryCandidate]) -> RecoveryOutcome:
    """不续采：把全部候选任务照常收尾（无窗口模式的缺省行为）。"""
    ids = [c.task_id for c in candidates]
    if ids:
        db.reconcile_orphan_tasks(only=ids)
    return RecoveryOutcome(abandoned=tuple(ids))


def _attach_plan(db, auto_marker, candidate: RecoveryCandidate) -> None:
    """续采成功后把库里的自动化方案重新挂到自动打点服务上（没有方案或没有服务则什么都不做）。
    停止时长按墙钟从任务原来的开始时间算（与 core.verdict.stop_condition_reached 同口径）：
    工具停摆的那段时间也算在内，不能从续采时刻重新起算，否则按时长停止的任务会多跑。"""
    if auto_marker is None:
        return
    from core.automation.plan import plan_from_json_or_none
    plan = plan_from_json_or_none(db.tasks.load_plan(candidate.task_id))
    if plan is not None:
        auto_marker.attach(candidate.task_id, plan, started_at=candidate.start_time)


ResumePrompt = Callable[[Sequence[RecoveryCandidate]], List[str]]
