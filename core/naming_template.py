"""
导出文件的命名模板

模板由字面文字与 {字段} 组成，如默认的 `{process}_{build}_{note}_{date}`；渲染结果是不带扩展名的
文件名主干，三种导出（报告、明细 CSV、证据包）的保存对话框都用它生成默认文件名。

字段：
- process：进程名，去掉 .exe；
- build：被测程序的文件版本（任务的 build 列优先，其次是被测目标元数据里的文件版本）；
- scenario：场景，暂无来源，恒为空（留给参考基线库）；
- note：任务备注，截断到 NOTE_MAX_CHARS 字；
- date：任务开始时间 YYYYMMDD_HHMM；
- kind：这份文件的类别（报告 / 明细 / 摘要 / 证据包）；
- pid：被测进程 PID（进程组任务没有，为空）——要在同一分钟里区分同名进程时用。

规则：
- 空字段连同它旁边多余的分隔符一起去掉（相邻的同一种分隔符 _ - . 空格只留一个，开头与结尾的
  分隔符去掉），所以不会出现 `__`，也不会以 `_` 开头或结尾；
- Windows 不允许的字符（\\ / : * ? " < > | 与控制字符）换成 `-`；字段值里的空白换成 `_`；
- Windows 保留名（CON、PRN、NUL、COM1…）加后缀；总长不超过 MAX_NAME_LENGTH，超长时先缩短最长的
  字段值，使日期等其余字段保留；
- 未知字段（{foo}）原样保留，由 unknown_fields 供设置页提示。

本模块不依赖 Qt，不读写文件。
"""
import re
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Tuple

from core.report import DATA_CSV_SUFFIX, REPORT_IMAGE_SUFFIX, SUMMARY_CSV_SUFFIX
from core.target_info import TargetInfo, target_info_from_json_or_none
from data.models import MonitorTask

DEFAULT_TEMPLATE = '{process}_{build}_{note}_{date}'
FIELD_NAMES = ('process', 'build', 'scenario', 'note', 'date', 'kind', 'pid')
NOTE_MAX_CHARS = 20
MAX_NAME_LENGTH = 120
DATE_FORMAT = '%Y%m%d_%H%M'

KIND_REPORT = '报告'
KIND_DETAIL = '明细'
KIND_SUMMARY = '摘要'
KIND_EVIDENCE = '证据包'

ZIP_SUFFIX = '.zip'
PNG_SUFFIX = '.png'
# 各类文件的扩展名：(模板里没有 {kind} 时沿用的后缀, 模板里有 {kind} 时的后缀)
FILE_SUFFIXES = {
    KIND_REPORT: (REPORT_IMAGE_SUFFIX, PNG_SUFFIX),
    KIND_DETAIL: (DATA_CSV_SUFFIX, DATA_CSV_SUFFIX),
    KIND_SUMMARY: (SUMMARY_CSV_SUFFIX, DATA_CSV_SUFFIX),
    KIND_EVIDENCE: (ZIP_SUFFIX, ZIP_SUFFIX),
}

INVALID_CHAR = '-'
RESERVED_SUFFIX = '_file'
SEPARATORS = '_-. '

_FIELD_RE = re.compile(r'\{([^{}]*)\}')
_INVALID_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_RESERVED_RE = re.compile(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])$')


def parse_template(template: str) -> List[Tuple[bool, str]]:
    """拆成 [(是否字段, 文本)]：字段给字段名，字面文字给原文。未知字段当作字面文字。"""
    tokens: List[Tuple[bool, str]] = []
    position = 0
    for match in _FIELD_RE.finditer(template):
        name = match.group(1)
        if name not in FIELD_NAMES:
            continue
        if match.start() > position:
            tokens.append((False, template[position:match.start()]))
        tokens.append((True, name))
        position = match.end()
    if position < len(template):
        tokens.append((False, template[position:]))
    return _merge_literals(tokens)


def _merge_literals(tokens: List[Tuple[bool, str]]) -> List[Tuple[bool, str]]:
    merged: List[Tuple[bool, str]] = []
    for is_field, text in tokens:
        if merged and not is_field and not merged[-1][0]:
            merged[-1] = (False, merged[-1][1] + text)
        else:
            merged.append((is_field, text))
    return merged


def unknown_fields(template: str) -> List[str]:
    """模板里不认识的 {字段}（按出现顺序、去重，含花括号），如 ['{foo}']。"""
    found: List[str] = []
    for match in _FIELD_RE.finditer(template):
        if match.group(1) not in FIELD_NAMES and match.group(0) not in found:
            found.append(match.group(0))
    return found


def uses_field(template: str, name: str) -> bool:
    return any(is_field and text == name for is_field, text in parse_template(template))


def _clean_value(text: object) -> str:
    """字段值：空白折成 "_"、非法字符换成 "-"。"""
    joined = '_'.join(str(text or '').split())
    return _INVALID_RE.sub(INVALID_CHAR, joined)


def _collapse_separators(text: str) -> str:
    """字面文字里相邻的同一种分隔符只留一个。"""
    return re.sub(r'([' + re.escape(SEPARATORS) + r'])\1+', r'\1', text)


def _fit_length(pieces: List[List], limit: int) -> None:
    """总长超限时，反复缩短最长的字段值（pieces 为 [是否字段, 文本]，原地修改）。"""
    while True:
        total = sum(len(text) for _f, text in pieces)
        excess = total - limit
        if excess <= 0:
            return
        values = [p for p in pieces if p[0] and len(p[1]) > 1]
        if not values:
            return
        longest = max(values, key=lambda p: len(p[1]))
        longest[1] = longest[1][:max(1, len(longest[1]) - excess)]


def render_name(template: str, fields: Mapping[str, str]) -> str:
    """按模板与字段值生成文件名主干（规则见模块说明）。字段缺失按空处理。"""
    pieces: List[List] = []
    for is_field, text in parse_template(template or DEFAULT_TEMPLATE):
        if is_field:
            value = _clean_value(fields.get(text, ''))
            if value:
                pieces.append([True, value])
            else:
                pieces.append([None, ''])           # 空字段占位：邻接的字面文字要合并
        else:
            pieces.append([False, _INVALID_RE.sub(INVALID_CHAR, text)])
    # 去掉空字段占位，合并因此相邻的字面文字并折叠分隔符
    flat: List[List] = []
    for flag, text in pieces:
        if flag is None:
            continue
        if flat and not flag and not flat[-1][0]:
            flat[-1][1] += text
        else:
            flat.append([flag, text])
    for piece in flat:
        if not piece[0]:
            piece[1] = _collapse_separators(piece[1])
    _fit_length(flat, MAX_NAME_LENGTH)
    name = ''.join(text for _f, text in flat)
    name = name[:MAX_NAME_LENGTH].strip(SEPARATORS)
    if not name:
        return 'task'
    if _RESERVED_RE.match(name.split('.')[0]):
        name = name[:MAX_NAME_LENGTH - len(RESERVED_SUFFIX)] + RESERVED_SUFFIX
    return name


def fields_for_task(task, *, kind: str = '', now: Optional[datetime] = None) -> Dict[str, str]:
    """任务 → 模板字段值（未清洗；render_name 统一清洗）。"""
    name = task.process_name or ''
    if name.lower().endswith('.exe'):
        name = name[:-4]
    build = getattr(task, 'build', None)
    if not build:
        info = target_info_from_json_or_none(getattr(task, 'meta_json', None))
        build = info.file_version if info is not None else None
    start = task.start_time or now
    pid = getattr(task, 'pid', None)
    return {
        'process': name,
        'build': str(build or ''),
        'scenario': str(getattr(task, 'scenario', None) or ''),
        'note': ' '.join((task.note or '').split())[:NOTE_MAX_CHARS],
        'date': start.strftime(DATE_FORMAT) if start is not None else '',
        'kind': kind,
        'pid': str(pid) if pid else '',
    }


def render_for_task(template: str, task, *, kind: str = '',
                    now: Optional[datetime] = None) -> str:
    """任务的默认文件名主干（不含扩展名）。"""
    return render_name(template, fields_for_task(task, kind=kind, now=now))


def default_filename(template: str, task, kind: str, *, now: Optional[datetime] = None) -> str:
    """某类文件在保存对话框里的默认文件名（带扩展名）。模板里没有 {kind} 时沿用旧的后缀
    （报告图片 _报告.png、摘要 _摘要.csv），有 {kind} 时类别已在名字里，只加扩展名。"""
    without_kind, with_kind = FILE_SUFFIXES[kind]
    suffix = with_kind if uses_field(template, 'kind') else without_kind
    return render_for_task(template, task, kind=kind, now=now) + suffix


def sample_task() -> MonitorTask:
    """设置页预览用的示例任务（不入库）。"""
    return MonitorTask(
        task_id='sample', pid=4321, process_name='chrome.exe', metric_types=[], interval=1.0,
        start_time=datetime(2026, 7, 16, 9, 30), end_time=None, status='stopped', note='内存回归',
        meta_json=TargetInfo(file_version='120.0.6099.109').to_json())


def preview_filename(template: str, kind: str = KIND_REPORT) -> str:
    """用示例任务渲染的默认文件名（设置页的实时预览）。"""
    return default_filename(template, sample_task(), kind)


def available_fields_text() -> str:
    """设置页提示用：可用字段列表，如 "{process}、{build}、…"。"""
    return '、'.join('{' + name + '}' for name in FIELD_NAMES)
