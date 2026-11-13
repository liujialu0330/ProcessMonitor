"""默认管道统一 UTF-8，注入流保留编码，中文与补充平面字符均不丢失。"""
import io
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import pmctl
from tests.test_pmctl import FakeSession, err, ok


@pytest.mark.parametrize('encoding', ['cp1252', 'gbk', 'utf-8', 'ascii'])
@pytest.mark.parametrize('failure', [False, True])
def test_json_output_raw_bytes_are_locale_independent(encoding, failure):
    response = (err('invalid_state', '当前暂停：磁盘不足 🧪') if failure else
                ok({'markers': [{'task_id': '中文任务', 'label': '基线 🧪 𝄞', 'timestamp': '现在'}]}))
    session = FakeSession({'marker': response})
    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding=encoding)
    errors = io.StringIO()
    code = pmctl.main(['--json', 'marker'], session_factory=lambda args: session,
                      out=output, err=errors)
    output.flush()
    encoded = raw.getvalue()
    assert encoded.isascii()
    assert json.loads(encoded.decode('utf-8')) == response.raw
    assert code == (1 if failure else 0)
    assert ('当前暂停' in errors.getvalue()) is failure
    output.detach()


def test_human_output_retains_readable_chinese():
    response = ok({'markers': [{'task_id': 't', 'label': '基线', 'timestamp': '现在'}]})
    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding='gbk')
    code = pmctl.main(['marker'], session_factory=lambda args: FakeSession({'marker': response}),
                      out=output, err=io.StringIO())
    output.flush()
    assert code == 0 and raw.getvalue().decode('gbk').splitlines() == ['t  基线  现在']
    output.detach()


@contextmanager
def encoded_stream(encoding, errors='strict', *, tty=False):
    class TextStream(io.TextIOWrapper):
        def isatty(self):
            return tty

    raw = io.BytesIO()
    stream = TextStream(raw, encoding=encoding, errors=errors)
    try:
        yield raw, stream
    finally:
        stream.detach()


def unescape_unicode(text):
    return re.sub(r'\\u([0-9a-f]{4})|\\U([0-9a-f]{8})',
                  lambda m: chr(int(m[1] or m[2], 16)), text)


@pytest.mark.parametrize('encoding', ['cp1252', 'gbk', 'utf-8', 'ascii'])
@pytest.mark.parametrize('errors', ['strict', 'replace', 'ignore'])
def test_explicit_stream_keeps_encoding_and_all_human_text(encoding, errors):
    response = ok({'markers': [{'task_id': 't', 'label': '基线 🧪 𝄞', 'timestamp': '现在'}]})
    with encoded_stream(encoding, errors) as (raw, output):
        code = pmctl.main(['marker'],
                          session_factory=lambda args: FakeSession({'marker': response}),
                          out=output, err=io.StringIO())
        text = raw.getvalue().decode(encoding)
        assert code == 0
        assert unescape_unicode(text).splitlines() == ['t  基线 🧪 𝄞  现在']
        assert (output.encoding, output.errors) == (encoding, errors)
        if encoding != 'utf-8':
            assert r'\U0001f9ea' in text and r'\U0001d11e' in text


@pytest.mark.parametrize('encoding', ['cp1252', 'gbk', 'ascii'])
def test_json_error_diagnostics_respect_explicit_error_stream(encoding):
    response = err('invalid_state', '当前暂停：磁盘不足 🧪')
    output = io.StringIO()
    with encoded_stream(encoding) as (raw, errors):
        code = pmctl.main(['--json', 'marker'],
                          session_factory=lambda args: FakeSession({'marker': response}),
                          out=output, err=errors)
        assert code == 1 and json.loads(output.getvalue()) == response.raw
        assert '当前暂停：磁盘不足 🧪' in unescape_unicode(raw.getvalue().decode(encoding))
        assert errors.encoding == encoding and errors.errors == 'strict'


@pytest.mark.parametrize('argv, expected, code', [
    (['--help'], '进程监控助手命令行', 0),
    (['start', '--help'], '被测进程 PID', 0),
    (['未知命令🧪'], '未知命令🧪', 2),
    (['start', '--pid', '中文🧪'], '中文🧪', 2),
])
def test_help_and_parse_errors_use_injected_streams(argv, expected, code, capsys):
    with encoded_stream('cp1252') as (out_raw, output), \
            encoded_stream('cp1252') as (err_raw, errors):
        assert pmctl.main(argv, out=output, err=errors) == code
        selected = out_raw if code == 0 else err_raw
        other = err_raw if code == 0 else out_raw
        assert expected in unescape_unicode(selected.getvalue().decode('cp1252'))
        assert other.getvalue() == b''
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def test_waiting_and_run_cleanup_diagnostics_use_same_encoding_contract(monkeypatch):
    def open_session(_name, **kwargs):
        kwargs['on_waiting']('正在等待实例就绪 🧪')
        return FakeSession({'ping': ok({'app_version': '2.0', 'pid': 1})})

    monkeypatch.setattr(pmctl.ipc, 'open_session', open_session)
    with encoded_stream('cp1252') as (raw, errors):
        assert pmctl.main(['--no-launch', 'ping'], out=io.StringIO(), err=errors) == 0
        assert '正在等待实例就绪 🧪' in unescape_unicode(raw.getvalue().decode('cp1252'))
    session = FakeSession({'start': ok({'task_id': '任务🧪', 'name': '程序', 'pid': 1}),
                           'wait': pmctl.ipc.PipeClosed('断开🧪'), 'stop': ok()})
    with encoded_stream('cp1252') as (raw, errors):
        code = pmctl.main(['run', '--pid', '1', '--stop-after', '1'],
                          session_factory=lambda args: session, out=io.StringIO(), err=errors)
        text = unescape_unicode(raw.getvalue().decode('cp1252'))
        assert code == 3 and '已停止本次开始的任务：任务🧪' in text and '断开🧪' in text


@pytest.mark.parametrize('tty, encoding', [(False, 'cp1252'), (True, 'cp1252'), (True, 'gbk')])
def test_default_stream_uses_utf8_only_for_redirection(monkeypatch, tty, encoding):
    response = ok({'markers': [{'task_id': 't', 'label': '基线 🧪', 'timestamp': '现在'}]})
    with encoded_stream(encoding, tty=tty) as (raw, output):
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'stdout', output)
            assert pmctl.main(['marker'],
                              session_factory=lambda args: FakeSession({'marker': response}),
                              err=io.StringIO()) == 0
        text = raw.getvalue().decode(encoding if tty else 'utf-8')
        assert unescape_unicode(text).splitlines() == ['t  基线 🧪  现在']
        assert output.encoding == encoding and output.errors == 'strict'
        if not tty:
            assert text == 't  基线 🧪  现在\n'


@pytest.mark.parametrize('argv, expected, code, channel', [
    (['ping'], '进程监控助手 版本🧪（PID 1，无窗口模式）', 0, 'stdout'),
    (['marker'], '中文任务  基线 🧪 𝄞  现在', 0, 'stdout'),
    (['--help'], '进程监控助手命令行', 0, 'stdout'),
    (['start', '--help'], '被测进程 PID', 0, 'stdout'),
    (['未知命令🧪'], '未知命令🧪', 2, 'stderr'),
    (['status'], '当前暂停：磁盘不足 🧪', 1, 'stderr'),
])
def test_real_cp1252_process_pipes_emit_utf8_bytes(tmp_path, argv, expected, code, channel):
    # 显式重配置模拟冻结解释器自己的编码，不依赖启动时是否读取环境变量。
    script = '''
import sys
import pmctl
from core.ipc.protocol import Response
sys.stdout.reconfigure(encoding='cp1252', errors='strict')
sys.stderr.reconfigure(encoding='cp1252', errors='strict')
class Session:
    def request(self, command, payload, timeout=None):
        if command == 'status':
            return Response(id=1, ok=False, error_code='invalid_state',
                            error_message='当前暂停：磁盘不足 🧪')
        return Response(id=1, ok=True, result={'app_version': '版本🧪', 'pid': 1,
            'headless': True, 'markers': [{'task_id': '中文任务', 'label': '基线 🧪 𝄞',
                                          'timestamp': '现在'}]})
    def close(self):
        pass
sys.exit(pmctl.main(ARGV, session_factory=lambda args: Session()))
'''.replace('ARGV', repr(argv))
    env = dict(os.environ, PYTHONIOENCODING='cp1252:strict', PYTHONUTF8='0',
               PROCESS_MONITOR_DATA_DIR=str(tmp_path / 'isolated'))
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True,
                               cwd=Path(__file__).resolve().parents[1], env=env, timeout=15)
    assert completed.returncode == code, completed.stderr.decode('utf-8')
    assert expected in getattr(completed, channel).decode('utf-8')
    assert getattr(completed, 'stderr' if channel == 'stdout' else 'stdout') == b''
