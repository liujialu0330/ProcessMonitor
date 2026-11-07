"""
LogWatcher：从末尾开始只看新行、增量读尾、半行等换行、同一行只触发一次、截断与替换
（轮转）后从头读、文件不存在时静默等待、出现后从头读、编码错误不崩、命名分组 label。
文件读写都在 tmp_path，时间用假时钟。
"""
import os

import pytest

from core.automation.log_watcher import MAX_READ_BYTES, LogWatcher
from tests.doubles import FakeClock

PATTERN = r"Case (?P<label>\d+) done|ROUND"


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def log(tmp_path):
    return tmp_path / "app.log"


def _append(path, text, encoding="utf-8"):
    with open(path, "ab") as f:
        f.write(text.encode(encoding) if isinstance(text, str) else text)


def _lines(matches):
    return [m.line for m in matches]


def test_existing_content_is_skipped_and_new_lines_matched(log, clock):
    _append(log, "Case 1 done\nnoise\n")
    watcher = LogWatcher(str(log), PATTERN, "utf-8", clock)
    assert watcher.poll() == []

    _append(log, "noise\nCase 2 done\nROUND\n")
    matches = watcher.poll()
    assert _lines(matches) == ["Case 2 done", "ROUND"]
    assert [m.label for m in matches] == ["2", None]
    assert all(m.timestamp == clock.now() for m in matches)


def test_each_line_triggers_once(log, clock):
    log.write_text("")
    watcher = LogWatcher(str(log), PATTERN, "utf-8", clock)
    _append(log, "ROUND\n")
    assert len(watcher.poll()) == 1
    assert watcher.poll() == []
    _append(log, "noise\n")
    assert watcher.poll() == []


def test_partial_line_waits_for_newline(log, clock):
    log.write_text("")
    watcher = LogWatcher(str(log), PATTERN, "utf-8", clock)
    _append(log, "Case 7 do")
    assert watcher.poll() == []
    _append(log, "ne\r\n")
    assert [m.label for m in watcher.poll()] == ["7"]
    assert watcher.poll() == []


def test_truncation_restarts_from_beginning(log, clock):
    log.write_text("")
    watcher = LogWatcher(str(log), PATTERN, "utf-8", clock)
    _append(log, "noise line that is fairly long\nROUND\n")
    assert len(watcher.poll()) == 1

    with open(log, "wb") as f:          # 截断后写入更短的新内容
        f.write(b"ROUND\n")
    assert _lines(watcher.poll()) == ["ROUND"]
    _append(log, "Case 3 done\n")
    assert [m.label for m in watcher.poll()] == ["3"]


def test_replaced_file_restarts_from_beginning(log, tmp_path, clock):
    """改名轮转：app.log → app.log.1，新建 app.log（新文件比旧偏移还长也能识别）"""
    log.write_text("")
    watcher = LogWatcher(str(log), PATTERN, "utf-8", clock)
    _append(log, "ROUND\n")
    assert len(watcher.poll()) == 1

    os.replace(log, tmp_path / "app.log.1")
    replacement = tmp_path / "new.log"
    replacement.write_bytes(b"Case 8 done\n" + b"x" * 100 + b"\nCase 9 done\n")
    os.replace(replacement, log)
    # 按旧偏移续读会跳过新文件开头的 Case 8
    assert [m.label for m in watcher.poll()] == ["8", "9"]


def test_missing_file_waits_then_reads_from_start(log, clock):
    watcher = LogWatcher(str(log), PATTERN, "utf-8", clock)
    assert watcher.poll() == [] and watcher.poll() == []
    _append(log, "Case 1 done\nCase 2 done\n")
    assert [m.label for m in watcher.poll()] == ["1", "2"]


def test_deleted_then_recreated_file_reads_from_start(log, clock):
    log.write_text("")
    watcher = LogWatcher(str(log), PATTERN, "utf-8", clock)
    _append(log, "ROUND\n")
    assert len(watcher.poll()) == 1
    os.remove(log)
    assert watcher.poll() == []
    _append(log, "ROUND\n")
    assert len(watcher.poll()) == 1


def test_decoding_errors_do_not_crash(log, clock):
    log.write_text("")
    watcher = LogWatcher(str(log), r"完成 (?P<label>\S+)", "utf-8", clock)
    _append(log, b"\xff\xfe garbage \x80\n")
    _append(log, "完成 第一轮\n")
    assert [m.label for m in watcher.poll()] == ["第一轮"]


def test_gbk_log(log, clock):
    log.write_text("")
    watcher = LogWatcher(str(log), r"完成 (?P<label>\S+)", "gbk", clock)
    _append(log, "完成 甲\n", encoding="gbk")
    assert [m.label for m in watcher.poll()] == ["甲"]


def test_blank_label_group_falls_back_to_none(log, clock):
    log.write_text("")
    watcher = LogWatcher(str(log), r"step(?P<label>\s*)$", "utf-8", clock)
    _append(log, "step\n")
    assert [m.label for m in watcher.poll()] == [None]


def test_large_append_is_read_in_chunks(log, clock):
    log.write_text("")
    watcher = LogWatcher(str(log), "ROUND", "utf-8", clock)
    filler = (b"y" * 1023 + b"\n") * (MAX_READ_BYTES // 1024 + 10)
    _append(log, filler + b"ROUND\n")
    first = watcher.poll()
    assert first == [] and watcher.offset == MAX_READ_BYTES
    assert _lines(watcher.poll()) == ["ROUND"]


def test_start_at_beginning_option(log, clock):
    _append(log, "ROUND\n")
    watcher = LogWatcher(str(log), "ROUND", "utf-8", clock, start_at_end=False)
    assert len(watcher.poll()) == 1
