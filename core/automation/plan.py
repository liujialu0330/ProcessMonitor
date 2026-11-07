"""
自动化方案（AutomationPlan）

一次监控任务的自动打点与停止条件：定时打点、日志匹配打点、按时长 / 轮数停止、停止后
自动导出。全部字段可选，缺省即"不做任何自动化"（与没有方案时的行为一致）。

方案随任务存进 tasks.plan_json（JSON 文本，带 schema_version）。后续的测试方案模板
（plans\\*.json）会在此基础上扩展字段，因此字段名一经发布就不再改名：
- 新增字段一律给缺省值，旧 JSON 缺这些字段时按缺省读入；
- 读入时不认识的字段忽略并记日志（新版本写、旧版本读）；
- schema_version 高于本版本能理解的版本时抛 PlanVersionError，由调用方提示升级，
  而不是静默丢掉看不懂的设置。

本模块只依赖标准库，不碰 Qt、不查数据库。
"""
import codecs
import json
import logging
import ntpath
import os
import re
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# 本版本写出、且能完整理解的方案结构版本
PLAN_SCHEMA_VERSION = 1
SCHEMA_VERSION_KEY = 'schema_version'

# 日志匹配规则里可选的命名分组：匹配到且非空时用作打点名称
LABEL_GROUP = 'label'

DEFAULT_LOG_ENCODING = 'utf-8'

# 日志读尾按字节找换行再逐行解码：UTF-16/32 的换行不是单字节 0x0A，无法这样切行
_UNSUPPORTED_ENCODING_PREFIXES = ('utf_16', 'utf_32')

# ---------- 校验文案 ----------
ERR_TIMED_TOO_SMALL = "定时打点间隔至少为 1 秒"
ERR_TIMED_BELOW_INTERVAL = "定时打点间隔（{value} 秒）不能小于采集间隔（{interval:g} 秒）"
ERR_LOG_PATH_MISSING = "已填写日志匹配规则，请同时选择日志文件"
ERR_LOG_PATTERN_MISSING = "已选择日志文件，请填写要匹配的内容（正则表达式）"
ERR_LOG_PATH_NOT_ABSOLUTE = "日志文件路径必须是完整的绝对路径（如 D:\\logs\\app.log）"
ERR_LOG_PATTERN_INVALID = "日志匹配规则不是合法的正则表达式：{detail}"
ERR_LOG_ENCODING_UNKNOWN = "不认识的日志文件编码：{encoding}"
ERR_LOG_ENCODING_UNSUPPORTED = "暂不支持 UTF-16 / UTF-32 编码的日志文件，请改用 UTF-8 或 GBK"
ERR_STOP_SECONDS_TOO_SMALL = "停止时长至少为 1 秒"
ERR_STOP_SECONDS_BELOW_INTERVAL = "停止时长（{value} 秒）不能小于采集间隔（{interval:g} 秒）"
ERR_STOP_ROUNDS_TOO_SMALL = "停止轮数至少为 1 轮"
ERR_EXPORT_DIR_MISSING = "已勾选停止后自动导出，请选择导出目录"
ERR_EXPORT_DIR_NOT_ABSOLUTE = "导出目录必须是完整的绝对路径"


class PlanFormatError(ValueError):
    """方案 JSON 无法解析，或字段类型不对。"""


class PlanVersionError(PlanFormatError):
    """方案由更高版本写出（schema_version 高于本版本），本版本无法完整理解。"""

    def __init__(self, version: Any):
        super().__init__(
            f"方案版本 {version} 高于本程序支持的版本 {PLAN_SCHEMA_VERSION}，请升级到最新版本")
        self.version = version


def is_absolute_path(path: str) -> bool:
    """是否为绝对路径。Windows 盘符路径与 UNC 路径在任何平台上都算（方案可能在别的
    机器上编辑），另认本平台的绝对路径。"""
    if not path:
        return False
    # 带盘符或 UNC 前缀且从根开始（"C:foo"、"\\foo" 这类相对当前盘/目录的写法不算）
    if ntpath.splitdrive(path)[0] and ntpath.isabs(path):
        return True
    return os.name != 'nt' and os.path.isabs(path)


def encoding_problem(encoding: str) -> Optional[str]:
    """日志编码的问题描述；可用返回 None。"""
    try:
        name = codecs.lookup(encoding).name.replace('-', '_')
    except (LookupError, TypeError):
        return ERR_LOG_ENCODING_UNKNOWN.format(encoding=encoding)
    if name.startswith(_UNSUPPORTED_ENCODING_PREFIXES):
        return ERR_LOG_ENCODING_UNSUPPORTED
    return None


@dataclass(frozen=True)
class AutomationPlan:
    """一个任务的自动化方案。字段名是持久化契约，发布后不改名。

    Attributes:
        timed_marker_seconds: 每隔多少秒自动打一轮点（从任务开始或恢复时刻起算，
            暂停期间不计时）；None 表示不定时打点
        log_path: 要监听的日志文件（绝对路径）；与 log_pattern 同时给出才生效
        log_pattern: 正则表达式，日志新增的一行匹配即打点；命名分组 label 匹配到且
            非空时作打点名称，否则用默认名称（基线 / 第 N 轮）
        log_encoding: 日志文件编码（需兼容 ASCII 换行，如 utf-8、gbk）
        stop_after_seconds: 任务运行满多少秒后自动停止（按墙钟计，暂停时间也算）
        stop_after_rounds: 打满多少轮（kind='user' 的打点数，手动与自动合计）后自动停止
        stop_when_target_exits: 被测进程退出时结束任务（现有行为，目前恒为 True；
            为续采等后续能力预留）
        export_on_stop: 任务结束后自动导出
        export_dir: 自动导出的目录（绝对路径）
    """
    timed_marker_seconds: Optional[int] = None
    log_path: Optional[str] = None
    log_pattern: Optional[str] = None
    log_encoding: str = DEFAULT_LOG_ENCODING
    stop_after_seconds: Optional[int] = None
    stop_after_rounds: Optional[int] = None
    stop_when_target_exits: bool = True
    export_on_stop: bool = False
    export_dir: Optional[str] = None

    # ---------- 派生判断 ----------

    @property
    def has_timed_marker(self) -> bool:
        return self.timed_marker_seconds is not None

    @property
    def has_log_trigger(self) -> bool:
        return bool(self.log_path) and bool(self.log_pattern)

    @property
    def has_stop_condition(self) -> bool:
        return self.stop_after_seconds is not None or self.stop_after_rounds is not None

    def is_empty(self) -> bool:
        """与缺省方案等价（没有任何自动化）。没有方案的任务不必存 plan_json。"""
        return self == AutomationPlan()

    def needs_service(self) -> bool:
        """是否需要自动打点服务跟踪该任务（打点、停止条件或停止后导出任一开启）。"""
        return (self.has_timed_marker or self.has_log_trigger or self.has_stop_condition
                or self.export_on_stop)

    def compiled_pattern(self) -> Optional["re.Pattern[str]"]:
        """编译后的日志匹配规则；未设置时为 None。规则不合法时抛 re.error。"""
        return re.compile(self.log_pattern) if self.log_pattern else None

    # ---------- 校验 ----------

    def validate(self, interval: Optional[float] = None) -> List[str]:
        """
        校验方案，返回中文错误列表（空列表表示可用）。

        Args:
            interval: 任务的采集间隔（秒）。给出时要求定时打点间隔与停止时长不小于它：
                比采集间隔还短的轮次里一个样本都没有
        """
        errors: List[str] = []
        if self.timed_marker_seconds is not None:
            if self.timed_marker_seconds < 1:
                errors.append(ERR_TIMED_TOO_SMALL)
            elif interval and self.timed_marker_seconds < interval:
                errors.append(ERR_TIMED_BELOW_INTERVAL.format(
                    value=self.timed_marker_seconds, interval=interval))

        if self.log_pattern and not self.log_path:
            errors.append(ERR_LOG_PATH_MISSING)
        if self.log_path and not self.log_pattern:
            errors.append(ERR_LOG_PATTERN_MISSING)
        if self.log_path and not is_absolute_path(self.log_path):
            errors.append(ERR_LOG_PATH_NOT_ABSOLUTE)
        if self.log_pattern:
            try:
                re.compile(self.log_pattern)
            except re.error as exc:
                errors.append(ERR_LOG_PATTERN_INVALID.format(detail=exc))
        if self.has_log_trigger or self.log_encoding != DEFAULT_LOG_ENCODING:
            problem = encoding_problem(self.log_encoding)
            if problem:
                errors.append(problem)

        if self.stop_after_seconds is not None:
            if self.stop_after_seconds < 1:
                errors.append(ERR_STOP_SECONDS_TOO_SMALL)
            elif interval and self.stop_after_seconds < interval:
                errors.append(ERR_STOP_SECONDS_BELOW_INTERVAL.format(
                    value=self.stop_after_seconds, interval=interval))
        if self.stop_after_rounds is not None and self.stop_after_rounds < 1:
            errors.append(ERR_STOP_ROUNDS_TOO_SMALL)

        if self.export_on_stop:
            if not self.export_dir:
                errors.append(ERR_EXPORT_DIR_MISSING)
            elif not is_absolute_path(self.export_dir):
                errors.append(ERR_EXPORT_DIR_NOT_ABSOLUTE)
        elif self.export_dir and not is_absolute_path(self.export_dir):
            errors.append(ERR_EXPORT_DIR_NOT_ABSOLUTE)
        return errors

    # ---------- JSON 往返 ----------

    def to_dict(self) -> Dict[str, Any]:
        """带 schema_version 的字典（字段顺序固定，便于人读与比对）。"""
        data: Dict[str, Any] = {SCHEMA_VERSION_KEY: PLAN_SCHEMA_VERSION}
        data.update(asdict(self))
        return data

    def to_json(self) -> str:
        """序列化为 JSON 文本（中文路径原样保留，不转义）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AutomationPlan":
        """
        由字典构造。缺失字段取缺省值；不认识的字段忽略并记日志；schema_version 缺失
        按 1 处理，高于 PLAN_SCHEMA_VERSION 抛 PlanVersionError；字段类型不对抛
        PlanFormatError。
        """
        if not isinstance(data, Mapping):
            raise PlanFormatError(f"方案必须是 JSON 对象，收到 {type(data).__name__}")
        version = data.get(SCHEMA_VERSION_KEY, PLAN_SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise PlanFormatError(f"schema_version 必须是整数，收到 {version!r}")
        if version > PLAN_SCHEMA_VERSION:
            raise PlanVersionError(version)

        known = {f.name: f for f in fields(cls)}
        unknown = sorted(k for k in data if k != SCHEMA_VERSION_KEY and k not in known)
        if unknown:
            logger.warning("自动化方案含本版本不认识的字段，已忽略: %s", ", ".join(unknown))

        values: Dict[str, Any] = {}
        for name in known:
            if name in data:
                values[name] = _check_field(name, data[name])
        return replace(cls(), **values)

    @classmethod
    def from_json(cls, text: str) -> "AutomationPlan":
        """由 JSON 文本构造；规则同 from_dict，文本不是合法 JSON 时抛 PlanFormatError。"""
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise PlanFormatError(f"方案不是合法的 JSON：{exc}") from exc
        return cls.from_dict(data)


# 字段 -> 允许的取值类型（None 另行允许的见 _NULLABLE）
_INT_FIELDS = ('timed_marker_seconds', 'stop_after_seconds', 'stop_after_rounds')
_STR_FIELDS = ('log_path', 'log_pattern', 'export_dir')
_BOOL_FIELDS = ('stop_when_target_exits', 'export_on_stop')


def _check_field(name: str, value: Any) -> Any:
    """检查单个字段的类型并做无损规整（整数值的浮点数接受为整数）。"""
    if name in _INT_FIELDS:
        if value is None:
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise PlanFormatError(f"{name} 必须是整数或 null，收到 {value!r}")
        return value
    if name in _STR_FIELDS:
        if value is None or value == '':
            return None
        if not isinstance(value, str):
            raise PlanFormatError(f"{name} 必须是字符串或 null，收到 {value!r}")
        return value
    if name in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise PlanFormatError(f"{name} 必须是 true / false，收到 {value!r}")
        return value
    if name == 'log_encoding':
        if value is None or value == '':
            return DEFAULT_LOG_ENCODING
        if not isinstance(value, str):
            raise PlanFormatError(f"log_encoding 必须是字符串，收到 {value!r}")
        return value
    return value


def plan_from_json_or_none(text: Optional[str]) -> Optional[AutomationPlan]:
    """
    tasks.plan_json → 方案；列为空、内容损坏或版本过高时返回 None 并记日志。
    给"读历史任务的方案用于展示 / 再测"的调用方：坏方案不应让界面出错。
    """
    if not text:
        return None
    try:
        return AutomationPlan.from_json(text)
    except PlanFormatError:
        logger.warning("无法读取任务的自动化方案，已忽略", exc_info=True)
        return None
