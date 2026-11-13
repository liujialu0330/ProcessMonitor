"""
IPC 协议（core/ipc/protocol.py）的纯函数测试：编码、解码、请求与响应校验、增量分帧
"""
import json

import pytest

from core.ipc import protocol as p


class TestEncode:
    def test_request_is_one_ascii_json_line(self):
        data = p.encode_request(7, 'marker', {'label': '第 1 轮'})
        assert data.endswith(b'\n') and data.count(b'\n') == 1
        data.decode('ascii')  # ensure_ascii=True：中文以 \\u 转义
        obj = json.loads(data)
        assert obj == {'v': 1, 'id': 7, 'cmd': 'marker', 'args': {'label': '第 1 轮'}}

    def test_ok_error_event_shapes(self):
        assert json.loads(p.encode_ok(1, {'a': 1})) == {'v': 1, 'id': 1, 'ok': True,
                                                        'result': {'a': 1}}
        err = json.loads(p.encode_error(2, p.ERR_INVALID_ARGS, "坏参数", pids=[1, 2]))
        assert err == {'v': 1, 'id': 2, 'ok': False,
                       'error': {'code': 'invalid_args', 'message': "坏参数", 'pids': [1, 2]}}
        ev = json.loads(p.encode_event(3, p.EVENT_HEARTBEAT, {'elapsed_s': 5}))
        assert ev == {'v': 1, 'id': 3, 'event': 'heartbeat', 'data': {'elapsed_s': 5}}

    def test_oversize_message_raises(self):
        with pytest.raises(p.LineTooLong):
            p.encode_ok(1, {'blob': 'x' * p.MAX_LINE_BYTES})

    def test_nan_is_rejected(self):
        with pytest.raises(ValueError):
            p.encode_ok(1, {'x': float('nan')})

    def test_constants(self):
        assert p.PROTOCOL_VERSION == 1
        assert p.SUPPORTED_VERSIONS == (1,)
        assert p.MAX_LINE_BYTES == 1024 * 1024
        assert len(set(p.ERROR_CODES)) == 11


class TestDecodeRequest:
    def test_roundtrip_and_unknown_fields_ignored(self):
        obj = p.decode_line(b'{"v":1,"id":"a","cmd":"ping","args":{},"extra":true}')
        req = p.parse_request(obj)
        assert req == p.Request(id='a', cmd='ping', args={}, version=1)

    def test_crlf_tolerated(self):
        assert p.decode_line(b'{"v":1}\r') == {'v': 1}

    @pytest.mark.parametrize('line', [b'not json', b'[1,2]', b'\xff\xfe'])
    def test_bad_lines(self, line):
        with pytest.raises(p.ProtocolError) as info:
            p.decode_line(line)
        assert info.value.code == p.ERR_BAD_REQUEST

    def test_deep_nesting_is_bad_request_not_recursion_error(self):
        """几十万层嵌套（仍在单行上限内）：解析器递归超限，按不合法的请求回 bad_request。"""
        depth = 200_000
        line = b'{"v":1,"a":' + b'[' * depth + b']' * depth + b'}'
        assert len(line) < p.MAX_LINE_BYTES
        with pytest.raises(p.ProtocolError) as info:
            p.decode_line(line)
        assert info.value.code == p.ERR_BAD_REQUEST
        assert p.MSG_TOO_DEEP in info.value.message

    @pytest.mark.parametrize('version', [None, 2, '1', True])
    def test_version_mismatch_lists_supported(self, version):
        with pytest.raises(p.ProtocolError) as info:
            p.parse_request({'v': version, 'id': 1, 'cmd': 'ping'})
        assert info.value.code == p.ERR_BAD_REQUEST
        assert info.value.extra == {'supported': [1]}

    @pytest.mark.parametrize('obj', [
        {'v': 1, 'id': 1},
        {'v': 1, 'id': 1, 'cmd': ''},
        {'v': 1, 'id': 1, 'cmd': 'ping', 'args': []},
        {'v': 1, 'id': 1.5, 'cmd': 'ping'},
        {'v': 1, 'id': True, 'cmd': 'ping'},
    ])
    def test_invalid_requests(self, obj):
        with pytest.raises(p.ProtocolError) as info:
            p.parse_request(obj)
        assert info.value.code == p.ERR_BAD_REQUEST

    def test_missing_args_defaults_to_empty(self):
        assert p.parse_request({'v': 1, 'id': None, 'cmd': 'status'}).args == {}

    def test_request_id_of(self):
        assert p.request_id_of({'id': 5}) == 5
        assert p.request_id_of({'id': [1]}) is None
        assert p.request_id_of('x') is None


class TestParseResponse:
    def test_ok(self):
        resp = p.parse_response(json.loads(p.encode_ok(1, {'x': 1})))
        assert resp.ok is True and resp.result == {'x': 1} and not resp.is_event

    def test_error(self):
        resp = p.parse_response(json.loads(p.encode_error(1, p.ERR_NOT_FOUND, "没有")))
        assert resp.ok is False and resp.error_code == 'not_found'
        assert resp.error_message == "没有"

    def test_event(self):
        resp = p.parse_response(json.loads(p.encode_event(1, 'heartbeat')))
        assert resp.is_event and resp.event == 'heartbeat' and resp.data == {}

    def test_malformed(self):
        with pytest.raises(p.ProtocolError):
            p.parse_response({'id': 1})
        with pytest.raises(p.ProtocolError):
            p.parse_response({'id': 1, 'ok': False, 'error': 'x'})


class TestLineFramer:
    def test_sticky_packets(self):
        framer = p.LineFramer()
        assert framer.feed(b'a\nb\nc\n') == [b'a', b'b', b'c']
        assert framer.pending == b''

    def test_half_lines_across_feeds(self):
        framer = p.LineFramer()
        assert framer.feed(b'{"v":') == []
        assert framer.pending == b'{"v":'
        assert framer.feed(b'1}\n{"v"') == [b'{"v":1}']
        assert framer.feed(b':1}\n') == [b'{"v":1}']

    def test_byte_by_byte(self):
        framer = p.LineFramer()
        out = []
        for byte in b'ab\ncd\n':
            out += framer.feed(bytes([byte]))
        assert out == [b'ab', b'cd']

    def test_oversize_without_newline_raises(self):
        framer = p.LineFramer(max_bytes=8)
        framer.feed(b'12345678')
        with pytest.raises(p.LineTooLong):
            framer.feed(b'9')

    def test_oversize_complete_line_raises(self):
        framer = p.LineFramer(max_bytes=4)
        with pytest.raises(p.LineTooLong):
            framer.feed(b'123456\n')

    def test_limit_is_inclusive(self):
        framer = p.LineFramer(max_bytes=4)
        assert framer.feed(b'1234\n') == [b'1234']

    def test_clear(self):
        framer = p.LineFramer()
        framer.feed(b'half')
        framer.clear()
        assert framer.pending == b''
