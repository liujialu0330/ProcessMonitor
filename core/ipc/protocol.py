"""
本地 IPC 协议（版本 1）：UTF-8 JSON 行

帧：每条消息一行 JSON（json.dumps(ensure_ascii=True)，因此一行内只有 ASCII），以 \\n 结尾；
单行（不含换行符）不得超过 MAX_LINE_BYTES，超出时服务端回 bad_request 并断开。
未知字段一律忽略（新版本加字段、旧版本读）。

请求  {"v":1,"id":7,"cmd":"marker","args":{...}}
成功  {"v":1,"id":7,"ok":true,"result":{...}}
失败  {"v":1,"id":7,"ok":false,"error":{"code":"invalid_args","message":"…"}}
事件  {"v":1,"id":7,"event":"heartbeat","data":{...}}      （没有 ok 字段，不是最终回复）

v 不是本版本支持的值时回 bad_request，error 里附 supported:[1]。id 原样回显（整数、字符串
或 null），客户端据此配对；一条请求可以先收到若干事件行，最后恰好一条带 ok 的回复。

本模块只用标准库：服务端（Qt）与 pmctl（无 Qt）共用。
"""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS: Tuple[int, ...] = (PROTOCOL_VERSION,)
# 单行上限（字节，不含换行符）
MAX_LINE_BYTES = 1024 * 1024
LINE_TERMINATOR = b'\n'

# ---------- 错误码（稳定字符串，客户端据此映射退出码） ----------
ERR_BAD_REQUEST = 'bad_request'          # 不是合法 JSON、缺字段、版本不符、超长
ERR_UNKNOWN_COMMAND = 'unknown_command'  # 命令不在白名单
ERR_INVALID_ARGS = 'invalid_args'        # 参数缺失、类型或取值不对
ERR_NOT_READY = 'not_ready'              # 程序仍在启动（数据库、管理器尚未就绪）
ERR_DISABLED = 'disabled'                # 设置里关闭了"允许命令行与脚本控制"
ERR_NOT_FOUND = 'not_found'              # 任务或进程不存在
ERR_INVALID_STATE = 'invalid_state'      # 当前状态不允许（任务已停止、只读库、无窗口模式等）
ERR_BUSY = 'busy'                        # 资源占用（导出进行中、数据库维护中、任务数已满）
ERR_TIMEOUT = 'timeout'                  # 服务端等待超时
ERR_INTERNAL = 'internal'                # 服务端内部错误
ERR_FORBIDDEN = 'forbidden'              # 客户端权限（完整性级别）低于服务端

ERROR_CODES = (
    ERR_BAD_REQUEST, ERR_UNKNOWN_COMMAND, ERR_INVALID_ARGS, ERR_NOT_READY, ERR_DISABLED,
    ERR_NOT_FOUND, ERR_INVALID_STATE, ERR_BUSY, ERR_TIMEOUT, ERR_INTERNAL, ERR_FORBIDDEN,
)

# 事件名
EVENT_HEARTBEAT = 'heartbeat'

# 文案
MSG_NOT_JSON = "请求不是合法的 JSON：{detail}"
MSG_NOT_OBJECT = "请求必须是 JSON 对象"
MSG_BAD_VERSION = "协议版本 {version!r} 不受支持，本程序支持的版本：{supported}"
MSG_MISSING_CMD = "请求缺少 cmd 字段或其不是字符串"
MSG_BAD_ARGS = "args 必须是 JSON 对象"
MSG_BAD_ID = "id 只能是整数、字符串或 null"
MSG_LINE_TOO_LONG = "单行超过 {limit} 字节上限"
MSG_NOT_ASCII = "消息必须是 UTF-8 编码"
MSG_TOO_DEEP = "嵌套层数过深"
MSG_BAD_RESPONSE = "响应格式不正确：{detail}"


class ProtocolError(Exception):
    """协议层错误：code 是错误码，extra 会并入回复的 error 对象（如 supported）。"""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class LineTooLong(ProtocolError):
    """一行超过 MAX_LINE_BYTES：连接必须断开（无法再找到下一行的起点）。"""

    def __init__(self, limit: int = MAX_LINE_BYTES):
        super().__init__(ERR_BAD_REQUEST, MSG_LINE_TOO_LONG.format(limit=limit))


@dataclass(frozen=True)
class Request:
    """解析后的请求。"""
    id: Any
    cmd: str
    args: Dict[str, Any] = field(default_factory=dict)
    version: int = PROTOCOL_VERSION


@dataclass(frozen=True)
class Response:
    """解析后的服务端消息：事件（event 非空）或最终回复（ok 为 True/False）。"""
    id: Any
    ok: Optional[bool] = None
    result: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: str = ''
    error: Optional[Dict[str, Any]] = None
    event: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    raw: Optional[Dict[str, Any]] = None

    @property
    def is_event(self) -> bool:
        return self.event is not None


# ---------- 编码 ----------

def encode_message(obj: Dict[str, Any]) -> bytes:
    """一条消息 → 一行字节（含结尾换行）。超出单行上限抛 LineTooLong。"""
    text = json.dumps(obj, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    data = text.encode('ascii')
    if len(data) > MAX_LINE_BYTES:
        raise LineTooLong()
    return data + LINE_TERMINATOR


def encode_request(request_id: Any, cmd: str, args: Optional[Dict[str, Any]] = None) -> bytes:
    return encode_message({'v': PROTOCOL_VERSION, 'id': request_id, 'cmd': cmd,
                           'args': dict(args or {})})


def encode_ok(request_id: Any, result: Optional[Dict[str, Any]] = None) -> bytes:
    return encode_message({'v': PROTOCOL_VERSION, 'id': request_id, 'ok': True,
                           'result': dict(result or {})})


def encode_error(request_id: Any, code: str, message: str, **extra: Any) -> bytes:
    error: Dict[str, Any] = {'code': code, 'message': message}
    error.update(extra)
    return encode_message({'v': PROTOCOL_VERSION, 'id': request_id, 'ok': False,
                           'error': error})


def encode_event(request_id: Any, event: str, data: Optional[Dict[str, Any]] = None) -> bytes:
    return encode_message({'v': PROTOCOL_VERSION, 'id': request_id, 'event': event,
                           'data': dict(data or {})})


# ---------- 解码 ----------

def decode_line(line: bytes) -> Dict[str, Any]:
    """一行字节（不含换行）→ JSON 对象；不是合法 JSON 对象时抛 ProtocolError(bad_request)。"""
    if len(line) > MAX_LINE_BYTES:
        raise LineTooLong()
    try:
        text = line.decode('utf-8')
    except UnicodeDecodeError:
        raise ProtocolError(ERR_BAD_REQUEST, MSG_NOT_ASCII) from None
    if text.endswith('\r'):
        text = text[:-1]
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(ERR_BAD_REQUEST, MSG_NOT_JSON.format(detail=exc)) from None
    except RecursionError:
        # 嵌套过深（如几十万层 [[[…]]]）：标准库解析器递归超限，按不合法的请求处理
        raise ProtocolError(ERR_BAD_REQUEST, MSG_NOT_JSON.format(detail=MSG_TOO_DEEP)) from None
    if not isinstance(obj, dict):
        raise ProtocolError(ERR_BAD_REQUEST, MSG_NOT_OBJECT)
    return obj


def _valid_id(value: Any) -> bool:
    return value is None or isinstance(value, str) or (
        isinstance(value, int) and not isinstance(value, bool))


def request_id_of(obj: Any) -> Any:
    """尽力取出请求 id（用于给不合法的请求回错；取不到时为 None）。"""
    if isinstance(obj, dict) and _valid_id(obj.get('id')):
        return obj.get('id')
    return None


def parse_request(obj: Dict[str, Any]) -> Request:
    """校验请求对象；不合法抛 ProtocolError(bad_request)。未知字段忽略。"""
    version = obj.get('v')
    if isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        raise ProtocolError(
            ERR_BAD_REQUEST,
            MSG_BAD_VERSION.format(version=version, supported=list(SUPPORTED_VERSIONS)),
            supported=list(SUPPORTED_VERSIONS))
    request_id = obj.get('id')
    if not _valid_id(request_id):
        raise ProtocolError(ERR_BAD_REQUEST, MSG_BAD_ID)
    cmd = obj.get('cmd')
    if not isinstance(cmd, str) or not cmd:
        raise ProtocolError(ERR_BAD_REQUEST, MSG_MISSING_CMD)
    args = obj.get('args')
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ProtocolError(ERR_BAD_REQUEST, MSG_BAD_ARGS)
    return Request(id=request_id, cmd=cmd, args=args, version=version)


def parse_response(obj: Dict[str, Any]) -> Response:
    """校验服务端消息；格式不对抛 ProtocolError(bad_request)。"""
    request_id = obj.get('id')
    event = obj.get('event')
    if event is not None and 'ok' not in obj:
        if not isinstance(event, str):
            raise ProtocolError(ERR_BAD_REQUEST, MSG_BAD_RESPONSE.format(detail='event'))
        data = obj.get('data')
        return Response(id=request_id, event=event,
                        data=data if isinstance(data, dict) else {}, raw=obj)
    ok = obj.get('ok')
    if ok is True:
        result = obj.get('result')
        return Response(id=request_id, ok=True,
                        result=result if isinstance(result, dict) else {}, raw=obj)
    if ok is False:
        error = obj.get('error')
        if not isinstance(error, dict):
            raise ProtocolError(ERR_BAD_REQUEST, MSG_BAD_RESPONSE.format(detail='error'))
        code = error.get('code')
        return Response(id=request_id, ok=False,
                        error_code=code if isinstance(code, str) else ERR_INTERNAL,
                        error_message=str(error.get('message') or ''),
                        error=error, raw=obj)
    raise ProtocolError(ERR_BAD_REQUEST, MSG_BAD_RESPONSE.format(detail='ok'))


# ---------- 分帧 ----------

class LineFramer:
    """增量分帧：feed() 喂入任意切分的字节，返回其中完整的行（不含换行符）。

    未完成的半行留在缓冲里等下一次 feed；缓冲中没有换行却已超过上限时抛 LineTooLong
    （调用方应断开连接）。
    """

    def __init__(self, max_bytes: int = MAX_LINE_BYTES):
        self._max = max_bytes
        self._buffer = bytearray()

    @property
    def pending(self) -> bytes:
        """尚未成行的字节。"""
        return bytes(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> List[bytes]:
        lines: List[bytes] = []
        if data:
            self._buffer.extend(data)
        while True:
            index = self._buffer.find(LINE_TERMINATOR)
            if index < 0:
                break
            if index > self._max:
                raise LineTooLong(self._max)
            lines.append(bytes(self._buffer[:index]))
            del self._buffer[:index + 1]
        if len(self._buffer) > self._max:
            raise LineTooLong(self._max)
        return lines
