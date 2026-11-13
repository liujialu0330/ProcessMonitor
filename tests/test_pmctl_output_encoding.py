"""机器 JSON 输出在 Windows 传统编码下仍是可按 UTF-8 解码的原始字节。"""
import io
import json

import pytest

import pmctl
from tests.test_pmctl import FakeSession, err, ok


@pytest.mark.parametrize('encoding', ['gbk', 'utf-8', 'ascii'])
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
