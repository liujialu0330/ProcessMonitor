"""
"复制结论"的文字：把分析页当前看到的结论整理成可贴进聊天、工单或文档的纯文本 / Markdown。

自上而下：
1. 元数据一行：进程名 · 文件版本 · PID（或进程组的目标类型）· 开始 ~ 结束 · 时长 · 采集间隔 · 备注；
2. 整体结论：判定胶囊 + 主句，**就是结论条上的那两段文字**（调用方原样传入，不在这里重写一套）；
   统计依据（结论条右侧的那句）跟在后面；
3. 各指标一行：判定、基线 → 当前、较基线、每轮均增、增速；数值是摘要表各行的单元格原文
   （MetricLine.from_texts 直接取 ui.formatting.SummaryRow.texts），所以与摘要表一致；
4. 有分段（被测进程重启续接）或中途异常退出时附分段摘要（core.verdict.segments_summary）；
5. 进程组任务附成员结论（core.verdict 的成员判定）。

本模块不依赖 Qt 与界面层：界面层把结论条的 verdict / sentence、摘要表各行的文本传进来。
"""
import json
from dataclasses import dataclass
from datetime import datetime
from typing import List, Mapping, Optional, Sequence, Tuple

from core.report import analysis_span, duration_text
from core.target_info import TargetInfo, target_info_from_json_or_none
from core.verdict import (VERDICT_INSUFFICIENT, VERDICT_RISING, VERDICT_STABLE, member_overall,
                          member_verdicts, segments_summary)
from data.models import TARGET_NAME, TARGET_TREE
from utils.metrics import get_metric_display_name

FORMAT_TEXT = 'text'
FORMAT_MARKDOWN = 'markdown'
FORMAT_LABELS = {FORMAT_TEXT: "纯文本", FORMAT_MARKDOWN: "Markdown"}

TIME_FORMAT = '%Y-%m-%d %H:%M:%S'
CLOCK_FORMAT = '%H:%M:%S'
# 摘要表里"没有值"的占位（STAT_EMPTY / STABLE_DASH）：复制的文字里省略这些项
PLACEHOLDERS = frozenset({'', '--', '—'})

LABEL_VERSION = "文件版本 {version}"
LABEL_TIME = "{start} ~ {end}"
LABEL_DURATION = "时长 {duration}"
LABEL_INTERVAL = "采集间隔 {seconds:g} 秒"
LABEL_NOTE = "备注：{note}"
TEXT_RUNNING = "进行中"
TEXT_TREE = "进程树 · 根 PID {pid}"
TEXT_NAME_GROUP = "同名进程"
TEXT_PID = "PID {pid}"
TEXT_NO_DATA = "无数据"

LABEL_OVERALL = "整体结论"
LABEL_BASIS = "统计依据"
LABEL_METRICS = "各指标"
LABEL_SEGMENTS = "分段"
LABEL_MEMBERS = "成员结论"

METRIC_HEADERS = ("指标", "判定", "基线", "当前", "较基线", "每轮均增", "增速 · R²")
MEMBER_HEADERS = ("成员", "实例数", "判定", "持续上涨的指标")

MEMBER_VERDICT_TEXTS = {VERDICT_RISING: "持续上涨", VERDICT_STABLE: "平稳",
                        VERDICT_INSUFFICIENT: "数据不足"}


def _real(text: str) -> str:
    """摘要表单元格原文；占位符（"--""—"）视为没有值。"""
    return '' if text.strip() in PLACEHOLDERS else text


@dataclass(frozen=True)
class MetricLine:
    """一个指标的结论行（单元格文本，口径同摘要表）。verdict 为占位符（没有数据）时用 note 说明。"""
    name: str
    verdict: str
    baseline: str = ''
    current: str = ''
    delta: str = ''
    per_round: str = ''
    rate: str = ''
    note: str = ''

    @classmethod
    def from_texts(cls, texts: Mapping[str, str], note: str = '') -> 'MetricLine':
        """由摘要表一行的 texts（键见 ui.formatting.SUMMARY_COLUMNS）构造；占位符留空。"""
        get = lambda key: _real(str(texts.get(key, '')))     # noqa: E731
        return cls(name=str(texts.get('metric', '')), verdict=get('verdict'),
                   baseline=get('baseline'), current=get('current'), delta=get('delta'),
                   per_round=get('per_round'), rate=get('rate'), note=note)

    @property
    def verdict_text(self) -> str:
        return self.verdict or self.note or TEXT_NO_DATA


@dataclass(frozen=True)
class MemberLine:
    """进程组任务一个成员的结论行。"""
    name: str
    instances: Optional[int]
    verdict: str
    rising: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Conclusion:
    """复制结论的全部内容（渲染前的结构化形式，测试可直接断言）。"""
    header: str
    verdict: str
    sentence: str
    basis: str
    metrics: Tuple[MetricLine, ...]
    segments: str
    members: Tuple[MemberLine, ...]


# ---------------------------------------------------------------- 元数据一行


def _identity_text(task) -> str:
    kind = getattr(task, 'target_kind', 'pid')
    if kind == TARGET_NAME:
        return TEXT_NAME_GROUP
    if kind == TARGET_TREE:
        pid = task.pid
        if not pid:
            try:
                pid = json.loads(getattr(task, 'target_spec', None) or '{}').get('root_pid') or ''
            except (ValueError, AttributeError):
                pid = ''
        return TEXT_TREE.format(pid=pid)
    return TEXT_PID.format(pid=task.pid)


def _time_range_text(task, end: Optional[datetime]) -> str:
    start = task.start_time
    if start is None:
        return ''
    if end is None:
        end_text = TEXT_RUNNING
    elif end.date() == start.date():
        end_text = end.strftime(CLOCK_FORMAT)
    else:
        end_text = end.strftime(TIME_FORMAT)
    return LABEL_TIME.format(start=start.strftime(TIME_FORMAT), end=end_text)


def header_parts(task, analysis, target: Optional[TargetInfo]) -> List[str]:
    """元数据一行的各段（缺失的段省略）。时长：已结束的任务取结束 − 开始，进行中的取分析范围的跨度。"""
    parts = [task.process_name]
    version = target.file_version if target is not None else None
    if version:
        parts.append(LABEL_VERSION.format(version=version))
    parts.append(_identity_text(task))
    parts.append(_time_range_text(task, task.end_time))
    seconds: Optional[float] = None
    if task.start_time is not None and task.end_time is not None:
        seconds = (task.end_time - task.start_time).total_seconds()
    else:
        span = analysis_span(analysis) if analysis is not None else None
        if span is not None:
            seconds = (span[1] - span[0]).total_seconds()
    if seconds is not None:
        parts.append(LABEL_DURATION.format(duration=duration_text(seconds)))
    if task.interval:
        parts.append(LABEL_INTERVAL.format(seconds=float(task.interval)))
    note = ' '.join((task.note or '').split())
    if note:
        parts.append(LABEL_NOTE.format(note=note))
    return [p for p in parts if p]


# ---------------------------------------------------------------- 成员


def member_lines(analysis, names: Optional[Mapping[str, str]] = None,
                 instances: Optional[Mapping[str, int]] = None) -> List[MemberLine]:
    """每个成员一行（持续上涨的在前，其余按显示名）。判定与上涨指标取自 core.verdict，
    names / instances 为 {成员键: 显示名 / 实例数}，缺省用成员键、不写实例数。"""
    names, instances = names or {}, instances or {}
    verdicts = member_verdicts(analysis)
    lines = []
    for member, overall in member_overall(analysis).items():
        rising = tuple(get_metric_display_name(metric)
                       for metric, verdict in verdicts.get(member, {}).items()
                       if verdict == VERDICT_RISING)
        lines.append(MemberLine(names.get(member, member), instances.get(member),
                                MEMBER_VERDICT_TEXTS.get(overall, overall), rising))
    lines.sort(key=lambda m: (m.verdict != MEMBER_VERDICT_TEXTS[VERDICT_RISING], m.name.casefold()))
    return lines


# ---------------------------------------------------------------- 组装


def build_conclusion(task, analysis, *, verdict: str, sentence: str,
                     metrics: Sequence[MetricLine], basis: str = '',
                     target: Optional[TargetInfo] = None,
                     member_names: Optional[Mapping[str, str]] = None,
                     member_instances: Optional[Mapping[str, int]] = None,
                     with_members: bool = False) -> Conclusion:
    """
    Args:
        task: 分析的任务
        analysis: TaskAnalysis（segments、member_snapshots 来自它）
        verdict / sentence: 结论条上的判定胶囊与主句，原样传入
        metrics: 摘要表各行（按表里的顺序）
        basis: 结论条上的统计依据（不带"依据："前缀）
        target: 被测目标元数据；缺省时从 task.meta_json 解析
        with_members: 是否附成员结论（进程组任务的合计视图）
    """
    if target is None:
        target = target_info_from_json_or_none(getattr(task, 'meta_json', None))
    members = (tuple(member_lines(analysis, member_names, member_instances))
               if with_members else ())
    return Conclusion(
        header=" · ".join(header_parts(task, analysis, target)),
        verdict=verdict, sentence=sentence, basis=basis, metrics=tuple(metrics),
        segments=segments_summary(analysis), members=members)


# ---------------------------------------------------------------- 纯文本


def overall_line(conclusion: Conclusion) -> str:
    """"整体结论：持续上涨。2 / 5 项持续上涨：…"：判定与主句同结论条（没有判定时只写主句）。"""
    verdict, sentence = conclusion.verdict, conclusion.sentence
    body = f"{verdict}。{sentence}" if verdict and sentence else (verdict or sentence)
    return f"{LABEL_OVERALL}：{body}" if body else ''


def metric_text_line(line: MetricLine) -> str:
    """"私有字节：持续上涨，基线 100 MB → 当前 155 MB（较基线 +55 MB），每轮均增 +5 MB，增速 …"。"""
    parts = [line.verdict_text]
    if line.baseline and line.current:
        parts.append(f"基线 {line.baseline} → 当前 {line.current}")
    elif line.current:
        parts.append(f"当前 {line.current}")
    if line.delta:
        parts[-1] += f"（较基线 {line.delta}）"
    if line.per_round:
        parts.append(f"每轮均增 {line.per_round}")
    if line.rate and line.verdict_text != line.rate:
        parts.append(f"增速 {line.rate}")
    return f"{line.name}：" + "，".join(parts)


def member_text_line(member: MemberLine) -> str:
    count = f"（{member.instances} 个实例）" if member.instances is not None else ''
    text = f"{member.name}{count}：{member.verdict}"
    if member.rising:
        text += f"（{'、'.join(member.rising)}）"
    return text


def to_text(conclusion: Conclusion) -> str:
    lines = [conclusion.header]
    overall = overall_line(conclusion)
    if overall:
        lines.append(overall)
    if conclusion.basis:
        lines.append(f"{LABEL_BASIS}：{conclusion.basis}")
    if conclusion.metrics:
        lines += ["", f"{LABEL_METRICS}："]
        lines += [f"- {metric_text_line(m)}" for m in conclusion.metrics]
    if conclusion.segments:
        lines += ["", f"{LABEL_SEGMENTS}：{conclusion.segments}"]
    if conclusion.members:
        lines += ["", f"{LABEL_MEMBERS}："]
        lines += [f"- {member_text_line(m)}" for m in conclusion.members]
    return "\n".join(lines)


# ---------------------------------------------------------------- Markdown


def _cell(text: object) -> str:
    """表格单元格：竖线转义、换行折成空格（保证每行列数不变）。"""
    return ' '.join(str(text).split()).replace('|', '\\|') or '—'


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]],
           right_from: int = 99) -> List[str]:
    align = ['---:' if i >= right_from else '---' for i in range(len(headers))]
    lines = ['| ' + ' | '.join(_cell(h) for h in headers) + ' |',
             '| ' + ' | '.join(align) + ' |']
    lines += ['| ' + ' | '.join(_cell(c) for c in row) + ' |' for row in rows]
    return lines


def to_markdown(conclusion: Conclusion) -> str:
    lines = [conclusion.header]
    overall = overall_line(conclusion)
    if overall:
        label, _sep, body = overall.partition('：')
        lines += ["", f"**{label}**：{body}"]
    if conclusion.basis:
        lines += ["", f"{LABEL_BASIS}：{conclusion.basis}"]
    if conclusion.metrics:
        lines.append("")
        lines += _table(METRIC_HEADERS, [
            (m.name, m.verdict_text, m.baseline, m.current, m.delta, m.per_round, m.rate)
            for m in conclusion.metrics], right_from=2)
    if conclusion.segments:
        lines += ["", f"**{LABEL_SEGMENTS}**：{conclusion.segments}"]
    if conclusion.members:
        lines += ["", f"**{LABEL_MEMBERS}**", ""]
        lines += _table(MEMBER_HEADERS, [
            (m.name, '' if m.instances is None else m.instances, m.verdict,
             '、'.join(m.rising)) for m in conclusion.members], right_from=99)
    return "\n".join(lines)


def render(conclusion: Conclusion, fmt: str) -> str:
    """按格式（FORMAT_TEXT / FORMAT_MARKDOWN）渲染。"""
    if fmt == FORMAT_TEXT:
        return to_text(conclusion)
    if fmt == FORMAT_MARKDOWN:
        return to_markdown(conclusion)
    raise ValueError(f"未知的结论格式：{fmt!r}")


__all__ = [
    'FORMAT_TEXT', 'FORMAT_MARKDOWN', 'FORMAT_LABELS', 'MetricLine', 'MemberLine', 'Conclusion',
    'build_conclusion', 'header_parts', 'member_lines', 'overall_line', 'metric_text_line',
    'member_text_line', 'to_text', 'to_markdown', 'render',
]
