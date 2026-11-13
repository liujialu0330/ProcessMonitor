"""可迁移测试方案：完整目标配置、版本化 JSON 与逐目标执行。只依赖标准库模块。"""
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Formatter
from typing import Callable, List, Optional, Tuple

from core.automation.plan import AutomationPlan, PlanFormatError, is_absolute_path
from core.process_launcher import LaunchSpec

logger = logging.getLogger(__name__)
PLAN_VERSION = 1
MAX_PLAN_BYTES = 1024 * 1024
MAX_TARGETS = 5
MAX_NOTE_CHARS = 2000
MAX_WAIT_TIMEOUT_S = 2592000
NOTE_FIELDS = frozenset({'process', 'scenario', 'build', 'date', 'index'})


def _text(data, key, default='', *, limit=4096):
    value = data.get(key, default)
    if not isinstance(value, str) or len(value) > limit:
        raise PlanFormatError(f'{key} 必须是不超过 {limit} 字的文本')
    return value.strip()


def _boolean(data, key, default=False):
    value = data.get(key, default)
    if type(value) is not bool:
        raise PlanFormatError(f'{key} 必须为 true 或 false')
    return value


def _number(data, key, default, low, high):
    value = data.get(key, default)
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise PlanFormatError(f'{key} 必须在 {low:g}～{high:g} 之间')
    return float(value)


def _unknown(data, allowed, context):
    for key in data.keys() - allowed:
        logger.warning('忽略%s中的未知字段：%s', context, key)


def validate_note_template(template: str) -> None:
    try:
        for _, key, spec, conversion in Formatter().parse(template):
            if key is not None and (key not in NOTE_FIELDS or spec or conversion):
                raise ValueError('只支持 ' + '、'.join(sorted(NOTE_FIELDS)))
    except ValueError as exc:
        raise PlanFormatError(f'备注模板不正确：{exc}') from None


@dataclass(frozen=True)
class PlanTarget:
    name: str = ''
    pid: Optional[int] = None
    target_kind: str = 'pid'
    launch: Optional[LaunchSpec] = None
    close_on_stop: bool = False
    metrics: Tuple[str, ...] = ()
    interval: float = 1.0
    note: str = ''
    scenario: str = ''
    build: str = ''
    wait: bool = False
    wait_timeout_s: Optional[float] = None
    automation: AutomationPlan = field(default_factory=AutomationPlan)

    @classmethod
    def from_dict(cls, data) -> 'PlanTarget':
        if not isinstance(data, dict):
            raise PlanFormatError('每个目标必须是 JSON 对象')
        _unknown(data, set(cls.__dataclass_fields__), '目标')
        kind = _text(data, 'target_kind', 'pid')
        if kind not in ('pid', 'tree', 'name'):
            raise PlanFormatError('目标类型必须为 pid、tree 或 name')
        name, pid = _text(data, 'name'), data.get('pid')
        if pid is not None and (type(pid) is not int or not 0 < pid <= 0xffffffff):
            raise PlanFormatError('PID 必须为正整数')
        raw_launch = data.get('launch')
        if isinstance(raw_launch, dict):
            _unknown(raw_launch, {'exe', 'args', 'cwd'}, '启动参数')
            raw_launch = {key: value for key, value in raw_launch.items()
                          if key in ('exe', 'args', 'cwd')}
        try:
            launch = LaunchSpec.from_dict(raw_launch) if raw_launch is not None else None
        except (TypeError, ValueError) as exc:
            raise PlanFormatError(f'启动参数不正确：{exc}') from None
        if launch is not None and (not is_absolute_path(launch.exe)
                                   or (launch.cwd and not is_absolute_path(launch.cwd))):
            raise PlanFormatError('启动程序与工作目录必须是完整的绝对路径')
        # 启动目标允许 name 作为展示名；其余情况下 PID 与名称不能同时选择。
        if kind == 'name':
            if not name or pid is not None or launch is not None:
                raise PlanFormatError('同名进程组必须指定名称，不能指定 PID 或启动程序')
        elif launch is not None:
            if pid is not None:
                raise PlanFormatError('启动程序与固定 PID 不能同时设置')
        elif (pid is None) == (not name):
            raise PlanFormatError('目标需要指定一个 PID、进程名或启动程序')
        if (kind == 'tree' and pid is None and launch is None
                and not (name and data.get('wait') is True)):
            raise PlanFormatError('进程树需要根 PID、启动程序或按名称等待根进程')
        raw_metrics = data.get('metrics', [])
        if (not isinstance(raw_metrics, list) or len(raw_metrics) > 64
                or any(not isinstance(m, str) or not m.strip() for m in raw_metrics)):
            raise PlanFormatError('指标必须为有效名称的数组')
        interval = _number(data, 'interval', 1, 1, 3600)
        if interval != int(interval):
            raise PlanFormatError('采集周期必须为整数秒')
        try:
            automation = AutomationPlan.from_dict(data.get('automation', {}))
        except (TypeError, ValueError) as exc:
            raise PlanFormatError(f'自动化配置不正确：{exc}') from None
        errors = automation.validate(interval)
        if errors:
            raise PlanFormatError('；'.join(errors))
        note = _text(data, 'note', limit=MAX_NOTE_CHARS)
        validate_note_template(note)
        wait = _boolean(data, 'wait')
        if wait and (pid is not None or launch is not None):
            raise PlanFormatError('等待启动仅适用于按名称选择的目标')
        close_on_stop = _boolean(data, 'close_on_stop')
        if close_on_stop and launch is None:
            raise PlanFormatError('只有由方案启动的程序才能设置停止时关闭')
        timeout = (None if data.get('wait_timeout_s') is None else
                   _number(data, 'wait_timeout_s', None, 1, MAX_WAIT_TIMEOUT_S))
        return cls(name=name, pid=pid, target_kind=kind, launch=launch,
                   close_on_stop=close_on_stop,
                   metrics=tuple(dict.fromkeys(m.strip() for m in raw_metrics)),
                   interval=interval, note=note, scenario=_text(data, 'scenario', limit=120),
                   build=_text(data, 'build', limit=120), wait=wait, wait_timeout_s=timeout,
                   automation=automation)

    def to_dict(self) -> dict:
        return {'name': self.name, 'pid': self.pid, 'target_kind': self.target_kind,
                'launch': self.launch.to_dict() if self.launch is not None else None,
                'close_on_stop': self.close_on_stop, 'metrics': list(self.metrics),
                'interval': self.interval, 'note': self.note, 'scenario': self.scenario,
                'build': self.build, 'wait': self.wait, 'wait_timeout_s': self.wait_timeout_s,
                'automation': self.automation.to_dict()}

    def start_args(self, *, index: int = 1, now: Optional[datetime] = None) -> dict:
        process = self.name or (self.launch.name if self.launch else str(self.pid))
        note = self.note.format(process=process, scenario=self.scenario, build=self.build,
                                date=(now or datetime.now()).strftime('%Y%m%d_%H%M%S'), index=index)
        if len(note) > MAX_NOTE_CHARS:
            raise PlanFormatError('展开后的备注不能超过 2000 字')
        args = {'target_kind': self.target_kind, 'interval': self.interval, 'note': note,
                'scenario': self.scenario, 'build': self.build, 'plan': self.automation.to_dict()}
        if self.metrics:
            args['metrics'] = list(self.metrics)
        if self.launch:
            args['launch'] = {**self.launch.to_dict(), 'close_on_stop': self.close_on_stop}
        elif self.pid is not None:
            args['pid'] = self.pid
        else:
            args['name'] = self.name
        if self.wait:
            args['wait'] = True
            if self.wait_timeout_s is not None:
                args['wait_timeout_s'] = self.wait_timeout_s
        return args


@dataclass(frozen=True)
class TestPlan:
    name: str
    targets: Tuple[PlanTarget, ...]
    shared_markers: bool = True
    __test__ = False

    @classmethod
    def from_dict(cls, data) -> 'TestPlan':
        if not isinstance(data, dict):
            raise PlanFormatError('测试方案必须是 JSON 对象')
        version = data.get('schema_version', 1)
        if type(version) is not int or version != PLAN_VERSION:
            raise PlanFormatError(f'不支持方案版本 {version}，请使用匹配版本的程序')
        _unknown(data, {'schema_version', 'name', 'targets', 'shared_markers'}, '方案')
        name = _text(data, 'name')
        if not name:
            raise PlanFormatError('方案名称不能为空')
        targets = data.get('targets')
        if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
            raise PlanFormatError(f'方案应包含 1～{MAX_TARGETS} 个目标')
        return cls(name, tuple(PlanTarget.from_dict(t) for t in targets),
                   _boolean(data, 'shared_markers', True))

    def to_dict(self) -> dict:
        return {'schema_version': PLAN_VERSION, 'name': self.name,
                'targets': [t.to_dict() for t in self.targets],
                'shared_markers': self.shared_markers}


def read_plan(path) -> TestPlan:
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanFormatError('方案文件不能超过 1 MB')
    try:
        return TestPlan.from_dict(json.loads(raw.decode('utf-8-sig')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlanFormatError(f'方案文件不是有效的 UTF-8 JSON：{exc}') from None


def write_plan(plan: TestPlan, path) -> Path:
    plan = TestPlan.from_dict(plan.to_dict())
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2).encode('utf-8')
    if len(payload) > MAX_PLAN_BYTES:
        raise PlanFormatError('方案文件不能超过 1 MB')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.plan-', delete=False) as stream:
            temporary = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return target
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class PlanStore:
    """数据目录内的 plans 文件夹；外部文件仅通过明确的导入/导出访问。"""

    def __init__(self, data_dir):
        self.directory = Path(data_dir) / 'plans'

    def list(self) -> List[TestPlan]:
        result = []
        for path in sorted(self.directory.glob('*.json')):
            try:
                result.append(read_plan(path))
            except (OSError, ValueError):
                logger.warning('无法读取测试方案：%s', path, exc_info=True)
        return sorted(result, key=lambda p: p.name.casefold())

    def path_for(self, name: str) -> Path:
        stem = re.sub(r'[^\w\-]+', '_', name, flags=re.UNICODE).strip('_')[:60] or 'plan'
        digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]
        return self.directory / f'{stem}_{digest}.json'

    def save(self, plan: TestPlan, *, overwrite: bool = False) -> Path:
        target = self.path_for(plan.name)
        if target.exists() and not overwrite:
            raise FileExistsError(f'已有同名方案：{plan.name}')
        return write_plan(plan, target)

    def import_file(self, path, *, overwrite: bool = False) -> TestPlan:
        plan = read_plan(path)
        self.save(plan, overwrite=overwrite)
        return plan

    def export_file(self, plan: TestPlan, path) -> Path:
        return write_plan(plan, path)


@dataclass(frozen=True)
class PlanResult:
    index: int
    target: PlanTarget
    task_id: Optional[str] = None
    error: Optional[str] = None


def execute_plan(plan: TestPlan, starter: Callable[[PlanTarget, int], Optional[str]]) -> List[PlanResult]:
    """逐目标执行，成功目标保留，失败逐个返回，调用方负责展示与共享打点分组。"""
    plan = TestPlan.from_dict(plan.to_dict())
    results = []
    for index, target in enumerate(plan.targets, 1):
        try:
            task_id = starter(target, index)
            results.append(PlanResult(index, target, task_id,
                                      None if task_id else '目标未能开始监控'))
        except Exception as exc:
            logger.warning('方案目标 %d 执行失败', index, exc_info=True)
            results.append(PlanResult(index, target, error=str(exc)))
    return results
