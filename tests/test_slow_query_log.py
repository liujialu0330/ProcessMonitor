"""
仓储慢操作告警（data/repos/_guard.storage_errors）：耗时超过 SLOW_QUERY_WARN_MS 记 WARNING
（操作名 + 耗时），同类操作 60 秒内只记一次；快操作不记；流式读取不计时；告警不改变异常语义。

计时函数 _guard._clock 换成假时钟，不真实等待。
"""
import logging
import sqlite3

import pytest

from data.errors import StorageError
from data.repos import _guard
from data.repos._guard import action_kind, storage_errors
from data.repos.task_repo import TaskRepo
from tests.helpers import ConnectionFactoryStub, make_v4_db

LOGGER = 'data.repos._guard'


class FakeTimer:
    """每次读取返回当前值；step 为每次读取后自动前进的秒数"""

    def __init__(self, start: float = 1000.0, step: float = 0.0):
        self.t = start
        self.step = step

    def __call__(self) -> float:
        value = self.t
        self.t += self.step
        return value


@pytest.fixture
def timer(monkeypatch):
    fake = FakeTimer()
    monkeypatch.setattr(_guard, '_clock', fake)
    _guard.reset_slow_log()
    yield fake
    _guard.reset_slow_log()


def _slow(timer, action: str, seconds: float, exc: BaseException = None):
    with storage_errors(action):
        timer.t += seconds
        if exc is not None:
            raise exc


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.WARNING]


def test_action_kind_strips_variables():
    assert action_kind("保存任务 abc ") == "保存任务"
    assert action_kind("写入数据点（5 条）") == "写入数据点"
    assert action_kind("读取任务列表") == "读取任务列表"


def test_slow_operation_logs_name_and_duration(timer, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    _slow(timer, "读取任务列表", 0.501)
    assert _warnings(caplog) == ["慢数据库操作: 读取任务列表耗时 501 ms"]


def test_fast_operation_is_silent(timer, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    _slow(timer, "读取任务列表", 0.5)     # 恰好等于阈值：不记
    assert _warnings(caplog) == []


def test_same_kind_logged_once_per_minute_with_suppressed_count(timer, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    _slow(timer, "保存任务 a ", 0.6)
    _slow(timer, "保存任务 b ", 0.7)      # 同类、60 秒内：抑制
    _slow(timer, "保存任务 c ", 0.8)
    _slow(timer, "读取任务列表", 0.9)      # 不同种类：照记
    timer.t += 60
    _slow(timer, "保存任务 d ", 1.0)
    assert _warnings(caplog) == [
        "慢数据库操作: 保存任务 a 耗时 600 ms",
        "慢数据库操作: 读取任务列表耗时 900 ms",
        "慢数据库操作: 保存任务 d 耗时 1000 ms（此前 60 秒内另有 2 次同类慢操作未记录）",
    ]


def test_streaming_read_is_not_timed(timer, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    _slow(timer, "流式读取数据点 x ", 30.0)
    assert _warnings(caplog) == []


def test_slow_failure_still_raises_storage_error(timer, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    with pytest.raises(StorageError) as info:
        _slow(timer, "删除任务 x ", 2.0, sqlite3.OperationalError("database is locked"))
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
    assert _warnings(caplog) == ["慢数据库操作: 删除任务 x 耗时 2000 ms"]
    with pytest.raises(ValueError):       # 编程错误原样抛出
        _slow(timer, "删除任务 y ", 0.0, ValueError("bad"))


def test_real_repository_operation_is_timed(timer, caplog, tmp_path):
    """经仓储的真实操作：每次读计时函数前进 0.6 秒，进出各读一次 → 600 ms"""
    caplog.set_level(logging.WARNING, logger=LOGGER)
    repo = TaskRepo(ConnectionFactoryStub(make_v4_db(str(tmp_path / 'slow.db'))))
    timer.step = 0.6
    assert repo.get('missing') is None
    assert _warnings(caplog) == ["慢数据库操作: 读取任务 missing 耗时 600 ms"]
