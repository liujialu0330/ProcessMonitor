"""
证据包构建

把一次测试的结论与依据打成一个 zip，便于提交缺陷单或转交他人复核：
- 报告三件套（报告图片、明细 CSV、摘要 CSV，由 core.report_worker 先行写出）；
- task.json：机器可读的任务信息、被测目标元数据（core.target_info）、数据质量、自动化方案、
  打点与判定（整体 + 每指标）。判定数值直接取自分析快照（HistorySnapshot）的字段，
  与分析页显示的是同一份数据，这里不重新拟合；
- README.txt：中文结论（首行整体判定，随后每指标一行）、文件清单、生成时间与工具版本；
- log_excerpt.txt：app.log（含轮转备份）在任务时间段内、含任务 ID 前 8 位或 ERROR/WARNING
  的行，最多 LOG_EXCERPT_MAX_LINES 行；不给日志路径时省略。

zip 先写到同目录的临时文件，全部写完再 os.replace 到目标；任何失败都删除临时文件，
不留半个 zip，也不破坏已存在的同名文件。时间一律为 ISO 格式的本地时间，取自注入的时钟。

本模块不依赖 Qt 与界面层，可在后台线程或命令行里调用。
"""
import json
import logging
import math
import os
import re
import tempfile
import zipfile
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import config
from core import verdict as verdict_rules
from core.analysis.intervals import mean_round_delta
from core.history_query import HistorySnapshot, TaskAnalysis
from core.report import process_stem, quality_text, safe_filename_part
from core.target_info import TargetInfo, target_info_from_json_or_none
from data.models import TARGET_NAME, TARGET_PID, TARGET_TREE, Marker, MonitorTask, QualityStats
from utils.logger import LOG_BACKUP_COUNT
from utils.metrics import (VALUE_CLASS_PEAK, VALUE_CLASS_RATE, format_metric_delta,
                           format_metric_rate, format_metric_value,
                           get_metric_display_name, get_metric_unit, split_member_key)

logger = logging.getLogger(__name__)

EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_SUFFIX = '.zip'
TASK_JSON_NAME = 'task.json'
README_NAME = 'README.txt'
LOG_EXCERPT_NAME = 'log_excerpt.txt'
LOG_EXCERPT_MAX_LINES = 500
# 文件名里备注的最大字符数
FILENAME_NOTE_MAX_CHARS = 40
FILENAME_TIME_FORMAT = '%Y%m%d_%H%M%S'
TEXT_TIME_FORMAT = '%Y-%m-%d %H:%M:%S'

# 判定取值与规则统一在 core.verdict（pmctl verdict 同一口径）；这里再导出，保持原有名字可用
VERDICT_RISING = verdict_rules.VERDICT_RISING
VERDICT_STABLE = verdict_rules.VERDICT_STABLE
VERDICT_FLUCTUATING = verdict_rules.VERDICT_FLUCTUATING
VERDICT_INSUFFICIENT = verdict_rules.VERDICT_INSUFFICIENT
VERDICT_TARGET_EXITED = verdict_rules.VERDICT_TARGET_EXITED
METRIC_NOT_JUDGED = verdict_rules.METRIC_NOT_JUDGED
REASON_NO_DATA = verdict_rules.REASON_NO_DATA
# start 字段的来源：基线值 / 统计范围首值
START_BASELINE = 'baseline'
START_FIRST = 'first'

# 日志行首时间戳（utils.logger 的格式：'%Y-%m-%d %H:%M:%S [LEVEL] name: message'）
_LOG_TIME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
_LOG_KEYWORDS = ('ERROR', 'WARNING', 'CRITICAL')

# ---- 文案 ----
VERDICT_TEXTS = {
    VERDICT_RISING: "持续上涨",
    VERDICT_STABLE: "平稳",
    VERDICT_FLUCTUATING: "波动",
    VERDICT_INSUFFICIENT: "数据不足",
}
NOT_JUDGED_TEXTS = {
    VALUE_CLASS_RATE: "累计量（不判定趋势）",
    VALUE_CLASS_PEAK: "峰值（不判定趋势）",
}
TEXT_NOT_JUDGED = "不判定趋势"
TEXT_NO_DATA = "无数据"
OVERALL_RISING = "结论：疑似泄漏——{names}持续上涨"
OVERALL_STABLE = "结论：未见持续上涨"
OVERALL_INSUFFICIENT = "结论：数据不足，无法判定（采集时长或样本数不够）"
OVERALL_TARGET_EXITED = "结论：被测进程在停止条件达成之前退出，测试未完成（以下各指标结论仅供参考）"
NAME_SEPARATOR = "、"
PART_SEPARATOR = " · "
TEXT_RATE = "增速 {rate}"
TEXT_RATE_CI = "增速 {rate}（{ci}）"
TEXT_R2 = "R² {r2:.2f}"
TEXT_PER_ROUND = "每轮 {delta}（{count} 轮）"
TEXT_VS_BASELINE = "较基线 {delta}（{start} → {last}）"
TEXT_VS_FIRST = "较开始 {delta}（{start} → {last}）"
TEXT_CURRENT = "当前 {value}"
README_TASK = "任务：{process}（PID {pid}）"
README_TASK_TREE = "任务：{process}（进程树，根进程 PID {pid}）"
README_TASK_NAME = "任务：全部名为 {process} 的进程"
README_MEMBER = "成员 {member}：{verdict}"
README_MEMBER_INDENT = "  "
README_NOTE = "备注：{note}"
README_PERIOD = "时间：{start} ~ {end}"
README_RUNNING = "进行中"
README_QUALITY = "数据质量：{text}"
README_FILES_TITLE = "文件清单："
README_FILE_LINE = "  {name}  —  {desc}"
README_GENERATED = "生成时间：{time}"
README_TOOL = "工具版本：{app} {version}"
FILE_DESC_REPORT = "报告图片（结论与多指标曲线）"
FILE_DESC_DETAIL = "明细数据（CSV，文件头为任务元数据）"
FILE_DESC_SUMMARY = "打点区间摘要（CSV）"
FILE_DESC_TASK_JSON = "机器可读的任务信息、被测目标、数据质量与判定"
FILE_DESC_LOG = "运行日志摘录（任务时间段内与本任务相关或告警的行）"
FILE_DESC_README = "本说明"
LOG_HEADER = "日志摘录：{path}"
LOG_RANGE = "时间段：{start} ~ {end}；筛选：含任务 ID 前 8 位“{short}”或 ERROR/WARNING 的行"
LOG_TRUNCATED = "（共 {total} 行匹配，只收录前 {limit} 行）"
LOG_MISSING = "（日志文件不存在或无法读取）"
LOG_NONE = "（时间段内没有匹配的行）"
MSG_MISSING_FILE = "证据包缺少文件：{path}"
MSG_ANALYSIS_FAILED = "分析数据读取失败，无法生成证据包：{detail}"


class EvidenceError(ValueError):
    """输入不完整（缺文件、分析失败），未写出任何文件。"""


# ---------------------------------------------------------------- 文件名


def evidence_filename(task: MonitorTask, clock, *, build: Optional[str] = None,
                      scenario: Optional[str] = None) -> str:
    """
    证据包的默认文件名：{进程}_{版本}_{场景}_{备注}_{时间}.zip。

    版本与场景是参考基线库的任务字段（尚未加入模型时缺省省略）；备注为空也省略；
    时间取时钟当前时刻（同一任务可多次导出）。各段去掉文件名非法字符，空白换成 "_"。
    """
    build = build if build is not None else getattr(task, 'build', None)
    scenario = scenario if scenario is not None else getattr(task, 'scenario', None)
    note = ' '.join((task.note or '').split())[:FILENAME_NOTE_MAX_CHARS]
    parts = [process_stem(task)]
    for text in (build, scenario, note):
        part = safe_filename_part('_'.join(str(text or '').split()))
        if part:
            parts.append(part)
    parts.append(clock.now().strftime(FILENAME_TIME_FORMAT))
    return '_'.join(parts) + EVIDENCE_SUFFIX


# ---------------------------------------------------------------- 判定（取自快照）


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec='milliseconds') if value is not None else None


def _clean(value: Any) -> Any:
    """递归把非有限浮点数（NaN / inf）换成 None：JSON 不允许它们。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _baseline_in_use(snapshot: HistorySnapshot) -> bool:
    """与分析页摘要表同一口径：有基线值且"只统计基线之后"时以基线为起点。"""
    baseline = snapshot.baseline
    return (baseline is not None and baseline.value is not None
            and snapshot.request.baseline_only)


def metric_verdict(metric: str, snapshot: HistorySnapshot) -> Dict[str, Any]:
    """
    一个指标的机器可读判定，全部取自快照字段（与分析页一致）：

    - verdict / reason / confidence：snapshot.judgement；非水位量为 not_judged，无数据为
      insufficient + no_data；
    - first / last / samples：统计范围聚合（snapshot.stats）；baseline：基线值；
      start：摘要表"基线"列的取值（有基线用基线值，否则用统计范围首值），delta = last - start；
    - rate_per_hour：回归斜率（stats.slope_per_hour）；ci：其 95% 置信区间半宽（每小时，
      由 snapshot.rate 换算单位）；rate_display：分析页显示的增速原值（单位按跨度为 /分 或 /小时）；
      r2：判定拟合的 R²；
    - rate_per_round：各轮增量均值；rounds：计入的轮数；round_deltas：逐轮明细。
    """
    stats = snapshot.stats or {}
    judgement = snapshot.judgement
    verdict = verdict_rules.metric_verdict(snapshot)
    reason = verdict_rules.metric_reason(snapshot)
    confidence = judgement.confidence if judgement is not None else None

    baseline = snapshot.baseline.value if snapshot.baseline is not None else None
    last = stats.get('last')
    from_baseline = _baseline_in_use(snapshot)
    start = baseline if from_baseline else stats.get('first')
    delta = last - start if last is not None and start is not None else None

    rate = snapshot.rate
    ci = (rate.ci * 3600.0 / rate.unit_seconds
          if rate is not None and rate.ci is not None and rate.unit_seconds else None)
    r2 = judgement.fit.r2 if judgement is not None and judgement.fit is not None else None
    counted = [r for r in snapshot.rounds if r.delta is not None]
    return _clean({
        'key': metric,
        'name': get_metric_display_name(metric),
        'unit': get_metric_unit(metric),
        'value_class': snapshot.value_class,
        'verdict': verdict,
        'reason': reason,
        'confidence': confidence,
        'samples': stats.get('count', snapshot.total_count),
        'stats_since': _iso(snapshot.stats_since),
        'first': stats.get('first'),
        'last': last,
        'baseline': baseline,
        'start': start,
        'start_source': START_BASELINE if from_baseline else START_FIRST,
        'delta': delta,
        'rate_per_hour': stats.get('slope_per_hour'),
        'ci': ci,
        'r2': r2,
        'rate_display': ({'value': rate.value, 'ci': rate.ci, 'unit_suffix': rate.unit_suffix}
                         if rate is not None else None),
        'rate_per_round': mean_round_delta(snapshot.rounds),
        'rounds': len(counted),
        'round_deltas': [{
            'label': r.label, 'start': _iso(r.start), 'end': _iso(r.end),
            'start_value': r.start_value, 'end_value': r.end_value, 'delta': r.delta,
        } for r in snapshot.rounds],
    })


def overall_verdict(metrics: Sequence[Dict[str, Any]], *, stop_reason: Any = None,
                    stopped_before_condition: bool = False) -> str:
    """由 metric_verdict 的结果算整体判定（规则在 core.verdict，与 pmctl verdict 一致）。"""
    return verdict_rules.combine_verdicts(
        (m['verdict'] for m in metrics), stop_reason=stop_reason,
        stopped_before_condition=stopped_before_condition)


def target_exited_before_condition(task: MonitorTask, stop_reason: Any,
                                   markers: Sequence[Marker]) -> bool:
    """被测进程是否在方案的停止条件达成之前退出（用任务的方案、起止时间与打点判断）。"""
    if not verdict_rules.is_target_exit(stop_reason):
        return False
    from core.automation.plan import plan_from_json_or_none
    plan = plan_from_json_or_none(task.plan_json)
    end = task.end_time or task.last_sample_at
    elapsed = ((end - task.start_time).total_seconds()
               if end is not None and task.start_time is not None else None)
    rounds = sum(1 for m in markers if m.kind == 'user')
    return verdict_rules.exited_before_condition(stop_reason, plan, elapsed_s=elapsed,
                                                 rounds=rounds)


def analysis_verdict(analysis: TaskAnalysis, *, stop_reason: Any = None,
                     stopped_before_condition: bool = False) -> Dict[str, Any]:
    """{overall, metrics: [...]}（给了 stop_reason 时另附 stop_reason）；任一快照查询失败
    抛 EvidenceError。进程组任务另附 members = [{key, verdict, metrics}]：每个成员各指标的
    判定（条目同 metrics，key 为 "指标@成员键"，另有 metric / member）；整体判定把合计与
    全部成员一起看。pid 任务没有 members 字段。"""
    metrics = []
    for metric, snapshot in analysis.snapshots.items():
        if snapshot.error is not None:
            raise EvidenceError(MSG_ANALYSIS_FAILED.format(detail=snapshot.error))
        metrics.append(metric_verdict(metric, snapshot))
    members: List[Dict[str, Any]] = []
    for key, snapshot in verdict_rules.member_snapshots_of(analysis).items():
        if snapshot.error is not None:
            raise EvidenceError(MSG_ANALYSIS_FAILED.format(detail=snapshot.error))
        metric, member = split_member_key(key)
        entry = {**metric_verdict(key, snapshot), 'metric': metric, 'member': member}
        group = next((g for g in members if g['key'] == member), None)
        if group is None:
            group = {'key': member, 'verdict': None, 'metrics': []}
            members.append(group)
        group['metrics'].append(entry)
    for group in members:
        group['verdict'] = verdict_rules.combine_verdicts(m['verdict'] for m in group['metrics'])
    result = {'overall': verdict_rules.overall_verdict(
        analysis, stop_reason=stop_reason, stopped_before_condition=stopped_before_condition),
        'metrics': metrics}
    if members:
        result['members'] = members
    if stop_reason is not None:
        result['stop_reason'] = verdict_rules.stop_reason_value(stop_reason)
    return result


# ---------------------------------------------------------------- task.json


def _quality_dict(quality: Optional[QualityStats]) -> Optional[Dict[str, Any]]:
    if quality is None:
        return None
    return {**asdict(quality), 'completion_ratio': quality.completion_ratio}


def _plan_value(plan_json: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    """(解析后的方案, 解析失败时的原文)"""
    if not plan_json:
        return None, None
    try:
        return json.loads(plan_json), None
    except (ValueError, TypeError):
        return None, plan_json


def _marker_dicts(markers: Iterable[Marker]) -> List[Dict[str, Any]]:
    return [{'timestamp': _iso(m.timestamp), 'label': m.label, 'kind': m.kind}
            for m in sorted(markers, key=lambda m: m.timestamp)]


def _task_dict(task: MonitorTask) -> Dict[str, Any]:
    create_time = (datetime.fromtimestamp(task.create_time)
                   if task.create_time and task.create_time > 0 else None)
    result = {
        'task_id': task.task_id,
        'process_name': task.process_name,
        'pid': task.pid,
        'note': task.note or '',
        'metrics': list(task.metric_types),
        'metric_names': [get_metric_display_name(m) for m in task.metric_types],
        'interval_seconds': task.interval,
        'status': getattr(task.status, 'value', task.status),
        'start_time': _iso(task.start_time),
        'end_time': _iso(task.end_time),
        'last_sample_at': _iso(task.last_sample_at),
        'sample_count': task.sample_count,
        'process_create_time': _iso(create_time),
    }
    if task.target_kind != TARGET_PID:
        # 进程组任务：监控目标的类型与参数（pid 任务沿用旧结构，不加这两个字段）
        result['target_kind'] = task.target_kind
        result['target_spec'] = _plan_value(task.target_spec)[0]
    return result


def build_task_payload(task: MonitorTask, analysis: TaskAnalysis, *, target: Optional[TargetInfo],
                       markers: Sequence[Marker], files: Sequence[str],
                       generated_at: datetime, stop_reason: Any = None) -> Dict[str, Any]:
    """task.json 的内容（字典）。stop_reason：任务的停止原因（被测进程在停止条件达成之前
    退出时整体判定为 target_exited）；None 表示调用方不知道，按普通判定。"""
    plan, plan_raw = _plan_value(task.plan_json)
    first = next(iter(analysis.snapshots.values()), None)
    payload = {
        'schema_version': EVIDENCE_SCHEMA_VERSION,
        'generated_at': _iso(generated_at),
        'tool': {'name': config.APP_NAME, 'version': config.APP_VERSION},
        'task': _task_dict(task),
        'target': target.to_dict() if target is not None else None,
        'quality': _quality_dict(task.quality),
        'plan': plan,
        'markers': _marker_dicts(markers),
        'analysis': {
            'range_key': analysis.range_key,
            'baseline_only': first.request.baseline_only if first is not None else None,
        },
        'verdict': analysis_verdict(
            analysis, stop_reason=stop_reason,
            stopped_before_condition=target_exited_before_condition(task, stop_reason, markers)),
        'files': list(files),
    }
    if plan_raw is not None:
        payload['plan_raw'] = plan_raw
    return _clean(payload)


# ---------------------------------------------------------------- README.txt


def _value_text(metric: str, value: Optional[float]) -> str:
    return format_metric_value(metric, value) if value is not None else "--"


def metric_line(item: Dict[str, Any]) -> str:
    """README 里一个指标的一行，如
    "私有字节：持续上涨 · 增速 +12.3 MB/小时（±0.4 MB/小时）· R² 0.98 · 每轮 +2.6 MB（3 轮）·
    较基线 +120 MB（100 MB → 220 MB）"。"""
    metric, name = item['key'], item['name']
    verdict = item['verdict']
    if verdict == VERDICT_INSUFFICIENT and item['reason'] == REASON_NO_DATA:
        return f"{name}：{TEXT_NO_DATA}"
    if verdict == METRIC_NOT_JUDGED:
        parts = [NOT_JUDGED_TEXTS.get(item['value_class'], TEXT_NOT_JUDGED)]
    else:
        parts = [VERDICT_TEXTS.get(verdict, verdict)]
        if item['rate_per_hour'] is not None and verdict != VERDICT_INSUFFICIENT:
            rate = format_metric_rate(metric, item['rate_per_hour'])
            ci = (format_metric_rate(metric, item['ci']).lstrip('+±')
                  if item['ci'] is not None else None)
            # 置信区间舍入后为 0（拟合几乎完美）时不写，避免"±0"噪声
            if ci and not ci.startswith('0'):
                parts.append(TEXT_RATE_CI.format(rate=rate, ci='±' + ci))
            else:
                parts.append(TEXT_RATE.format(rate=rate))
        if item['r2'] is not None:
            parts.append(TEXT_R2.format(r2=item['r2']))
    if item['rate_per_round'] is not None:
        parts.append(TEXT_PER_ROUND.format(
            delta=format_metric_delta(metric, item['rate_per_round']), count=item['rounds']))
    if item['delta'] is not None:
        template = TEXT_VS_BASELINE if item['start_source'] == START_BASELINE else TEXT_VS_FIRST
        parts.append(template.format(delta=format_metric_delta(metric, item['delta']),
                                     start=_value_text(metric, item['start']),
                                     last=_value_text(metric, item['last'])))
    elif item['last'] is not None:
        parts.append(TEXT_CURRENT.format(value=_value_text(metric, item['last'])))
    return f"{name}：{PART_SEPARATOR.join(parts)}"


def overall_line(verdict: Dict[str, Any]) -> str:
    """README 首行：整体判定。"""
    overall = verdict['overall']
    if overall == VERDICT_RISING:
        names = [m['name'] for m in verdict['metrics'] if m['verdict'] == VERDICT_RISING]
        names += [m['name'] for g in verdict.get('members') or [] for m in g['metrics']
                  if m['verdict'] == VERDICT_RISING]
        return OVERALL_RISING.format(names=NAME_SEPARATOR.join(names))
    if overall == VERDICT_INSUFFICIENT:
        return OVERALL_INSUFFICIENT
    if overall == VERDICT_TARGET_EXITED:
        return OVERALL_TARGET_EXITED
    return OVERALL_STABLE


def _time_text(value: Optional[datetime]) -> str:
    return value.strftime(TEXT_TIME_FORMAT) if value is not None else README_RUNNING


def readme_text(task: MonitorTask, payload: Dict[str, Any], *, target: Optional[TargetInfo],
                files: Sequence[Tuple[str, str]], generated_at: datetime) -> str:
    """README.txt 全文（CRLF 换行，记事本友好）。files 为 (文件名, 说明)。"""
    verdict = payload['verdict']
    lines = [overall_line(verdict)]
    lines += [metric_line(item) for item in verdict['metrics']]
    for group in verdict.get('members') or []:
        lines.append(README_MEMBER.format(
            member=group['key'], verdict=VERDICT_TEXTS.get(group['verdict'], group['verdict'])))
        lines += [README_MEMBER_INDENT + metric_line(item) for item in group['metrics']]
    lines.append('')
    task_text = {TARGET_TREE: README_TASK_TREE, TARGET_NAME: README_TASK_NAME}.get(
        task.target_kind, README_TASK)
    lines.append(task_text.format(process=task.process_name, pid=task.pid))
    if (task.note or '').strip():
        lines.append(README_NOTE.format(note=' '.join(task.note.split())))
    lines.append(README_PERIOD.format(start=_time_text(task.start_time),
                                      end=_time_text(task.end_time)))
    quality = quality_text(task.quality)
    if quality:
        lines.append(README_QUALITY.format(text=quality))
    if target is not None:
        lines += target.summary_lines()
    lines.append('')
    lines.append(README_FILES_TITLE)
    lines += [README_FILE_LINE.format(name=name, desc=desc) for name, desc in files]
    lines.append('')
    lines.append(README_GENERATED.format(time=generated_at.strftime(TEXT_TIME_FORMAT)))
    lines.append(README_TOOL.format(app=config.APP_NAME, version=config.APP_VERSION))
    return '\r\n'.join(lines) + '\r\n'


# ---------------------------------------------------------------- 日志摘录


def _log_files(log_path: str) -> List[str]:
    """按时间先后排列的日志文件：轮转备份 app.log.N … app.log.1，最后是 app.log。"""
    candidates = [f"{log_path}.{n}" for n in range(LOG_BACKUP_COUNT, 0, -1)] + [log_path]
    return [path for path in candidates if os.path.isfile(path)]


def log_excerpt_lines(log_path: str, task_id: str, start: Optional[datetime],
                      end: Optional[datetime], *,
                      max_lines: int = LOG_EXCERPT_MAX_LINES) -> Tuple[List[str], int, bool]:
    """
    日志摘录：[start, end] 时间段内（按秒比较）含任务 ID 前 8 位或 ERROR/WARNING 的行；
    没有行首时间戳的续行（异常堆栈）随其所属的行一起取舍。

    Returns:
        (收录的行（至多 max_lines 行，不含换行符）, 匹配的总行数, 是否读到了日志文件)
    """
    short_id = task_id[:8]
    low = start.replace(microsecond=0) if start is not None else None
    high = end
    kept: List[str] = []
    total = 0
    files = _log_files(log_path)
    for path in files:
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                include = False
                for raw in f:
                    line = raw.rstrip('\r\n')
                    match = _LOG_TIME_RE.match(line)
                    if match:
                        try:
                            ts = datetime.strptime(match.group(1), TEXT_TIME_FORMAT)
                        except ValueError:
                            include = False
                            continue
                        in_range = ((low is None or ts >= low)
                                    and (high is None or ts <= high))
                        include = in_range and (
                            short_id in line or any(k in line for k in _LOG_KEYWORDS))
                    if include:
                        total += 1
                        if len(kept) < max_lines:
                            kept.append(line)
        except OSError:
            logger.warning("读取日志文件失败，证据包摘录跳过它: %s", path, exc_info=True)
    return kept, total, bool(files)


def log_excerpt_text(log_path: str, task: MonitorTask, end: Optional[datetime]) -> str:
    """log_excerpt.txt 全文（CRLF 换行）。"""
    lines, total, found = log_excerpt_lines(log_path, task.task_id, task.start_time, end)
    header = [LOG_HEADER.format(path=log_path),
              LOG_RANGE.format(start=_time_text(task.start_time), end=_time_text(end),
                               short=task.task_id[:8])]
    if not found:
        header.append(LOG_MISSING)
    elif not lines:
        header.append(LOG_NONE)
    elif total > len(lines):
        header.append(LOG_TRUNCATED.format(total=total, limit=len(lines)))
    return '\r\n'.join(header + [''] + lines) + '\r\n'


# ---------------------------------------------------------------- 打包


def _zip_info(name: str, at: datetime) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=at.timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _merge_stored(task: MonitorTask, db) -> MonitorTask:
    """调用方手里的任务对象可能是旧的（如刚开始时缓存的）：元数据、方案与数据质量缺失时
    从库里补上。读库失败只记日志，按手里的对象继续。"""
    if db is None or (task.meta_json and task.plan_json and task.quality):
        return task
    try:
        stored = db.tasks.get(task.task_id)
    except Exception:   # noqa: BLE001 —— 补字段失败不影响证据包的其余内容
        logger.warning("证据包读取任务记录失败，按现有任务信息继续: task_id=%s",
                       task.task_id, exc_info=True)
        return task
    if stored is None:
        return task
    return replace(task,
                   meta_json=task.meta_json or stored.meta_json,
                   plan_json=task.plan_json or stored.plan_json,
                   quality=task.quality or stored.quality)


def build_evidence_package(db, task: MonitorTask, analysis: TaskAnalysis, *,
                           report_png: Optional[str], detail_csv: Optional[str],
                           summary_csv: Optional[str], out_path: str,
                           log_path: Optional[str], clock, stop_reason: Any = None) -> str:
    """
    生成证据包 zip，返回 out_path。

    Args:
        db: Database（只用 tasks.get 补全任务的元数据/方案/数据质量；可为 None）
        task: 任务
        analysis: 分析页当前的 TaskAnalysis（判定数值取自它，与界面一致）
        report_png / detail_csv / summary_csv: 已写好的三件套路径；为 None 的不收录，
            给出但不存在时抛 EvidenceError
        out_path: 目标 zip 路径（已存在则在全部写完后被替换）
        log_path: app.log 路径；为 None 不收录日志摘录
        clock: 时钟（只用 now()）
        stop_reason: 任务的停止原因（StopReason 或其值；由调用方传入）。被测进程在方案的
            停止条件达成之前退出时整体判定为 target_exited；None 按普通判定

    Raises:
        EvidenceError: 缺文件或分析快照查询失败（什么也不写）
        OSError: 写文件失败（临时文件已删除，目标保持原样）
    """
    generated_at = clock.now()
    inputs = [(p, d) for p, d in ((report_png, FILE_DESC_REPORT), (detail_csv, FILE_DESC_DETAIL),
                                  (summary_csv, FILE_DESC_SUMMARY)) if p]
    for path, _ in inputs:
        if not os.path.isfile(path):
            raise EvidenceError(MSG_MISSING_FILE.format(path=path))

    task = _merge_stored(task, db)
    target = target_info_from_json_or_none(task.meta_json)
    markers = analysis.markers if analysis.snapshots else (
        db.markers.list(task.task_id) if db is not None else [])
    files = [(os.path.basename(p), d) for p, d in inputs]
    files.append((TASK_JSON_NAME, FILE_DESC_TASK_JSON))
    if log_path:
        files.append((LOG_EXCERPT_NAME, FILE_DESC_LOG))
    files.append((README_NAME, FILE_DESC_README))

    payload = build_task_payload(task, analysis, target=target, markers=markers,
                                 files=[name for name, _ in files], generated_at=generated_at,
                                 stop_reason=stop_reason)
    task_json = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    readme = readme_text(task, payload, target=target, files=files, generated_at=generated_at)
    excerpt = None
    if log_path:
        end = task.end_time or task.last_sample_at or generated_at
        excerpt = log_excerpt_text(log_path, task, end)

    directory = os.path.dirname(os.path.abspath(out_path))
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(out_path) + '.', suffix='.tmp',
                               dir=directory)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path, _ in inputs:
                    archive.write(path, arcname=os.path.basename(path))
                archive.writestr(_zip_info(TASK_JSON_NAME, generated_at),
                                 task_json.encode('utf-8'))
                if excerpt is not None:
                    archive.writestr(_zip_info(LOG_EXCERPT_NAME, generated_at),
                                     excerpt.encode('utf-8-sig'))
                archive.writestr(_zip_info(README_NAME, generated_at),
                                 readme.encode('utf-8-sig'))
        os.replace(tmp, out_path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            logger.error("清理证据包临时文件失败: %s", tmp, exc_info=True)
        raise
    logger.info("证据包已生成: task_id=%s, path=%s", task.task_id, out_path)
    return out_path
