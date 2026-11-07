"""
被测目标元数据探针：对本进程（python.exe 有版本资源）探测、进程不存在、AccessDenied 与
各环境字段失败时只留空不抛；Windows 11 版本号纠正；JSON 往返与容错；摘要行文案。
"""
import json
import os
import sys
from datetime import datetime

import psutil
import pytest

import config
import core.target_info as target_info
from core.target_info import (CMDLINE_SUMMARY_MAX_CHARS, TargetInfo, os_version_text, probe,
                              read_version_info, target_info_from_json_or_none)

NOW = datetime(2026, 11, 7, 10, 0, 0)
windows_only = pytest.mark.skipif(sys.platform != 'win32', reason="版本资源与令牌只在 Windows 上可读")


class _Clock:
    def now(self):
        return NOW


def _missing_pid() -> int:
    pid = 4_000_000
    while psutil.pid_exists(pid):
        pid += 4
    return pid


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# ---------------------------------------------------------------- 探针


@windows_only
def test_probe_self_reads_exe_version_and_environment():
    info = probe(os.getpid(), clock=_Clock(), dpi_scale=1.25)
    assert info.pid == os.getpid()
    assert _same_path(info.exe_path, sys.executable)
    # python.exe 的版本资源：FileVersion 形如 "3.11.9"，产品名 Python
    major_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert info.file_version.startswith(major_minor)
    assert info.product_version.startswith(major_minor)
    assert info.product_name == "Python"
    assert info.company
    assert isinstance(info.cmdline, tuple) and info.cmdline
    assert abs((info.create_time - datetime.fromtimestamp(
        psutil.Process().create_time())).total_seconds()) < 1
    assert info.is_admin_target in (True, False)
    assert info.tool_version == config.APP_VERSION
    assert info.tool_is_admin in (True, False)
    assert info.os_version.startswith("Windows ")
    assert info.cpu
    assert info.logical_cores == psutil.cpu_count(logical=True)
    assert info.total_memory_bytes == psutil.virtual_memory().total
    assert info.dpi_scale == 1.25
    assert info.captured_at == NOW


def test_probe_missing_pid_leaves_process_fields_empty():
    pid = _missing_pid()
    info = probe(pid, clock=_Clock())
    assert info.pid == pid
    assert (info.exe_path, info.file_version, info.cmdline, info.create_time,
            info.is_admin_target) == (None, None, None, None, None)
    # 环境字段照常
    assert info.tool_version == config.APP_VERSION and info.captured_at == NOW
    assert info.total_memory_bytes and info.logical_cores
    assert info.dpi_scale is None
    # 摘要只剩环境行
    assert [line.split(":")[0] for line in info.summary_lines()] == ["环境"]


def test_probe_access_denied_keeps_other_fields(monkeypatch):
    def _deny(self, *args, **kwargs):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, 'exe', _deny)
    monkeypatch.setattr(psutil.Process, 'cmdline', _deny)
    info = probe(os.getpid(), clock=_Clock())
    assert info.exe_path is None and info.cmdline is None
    assert info.file_version is None        # 没有路径就不读版本资源
    assert info.create_time is not None     # 其余进程字段不受影响
    assert info.tool_version == config.APP_VERSION


def test_probe_never_raises_when_every_source_fails(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("模拟失败")

    class _BadClock:
        def now(self):
            raise RuntimeError("时钟坏了")

    monkeypatch.setattr(target_info, 'read_version_info', _boom)
    monkeypatch.setattr(target_info, 'process_is_elevated', _boom)
    monkeypatch.setattr(target_info, 'current_user_is_admin', _boom)
    monkeypatch.setattr(target_info, 'os_version_text', _boom)
    monkeypatch.setattr(target_info, 'cpu_name', _boom)
    monkeypatch.setattr(target_info.psutil, 'virtual_memory', _boom)
    monkeypatch.setattr(target_info.psutil, 'cpu_count', _boom)
    monkeypatch.setattr(target_info.os, 'cpu_count', _boom)
    monkeypatch.setattr(psutil.Process, 'create_time', _boom)

    info = probe(os.getpid(), clock=_BadClock())
    assert info.exe_path is not None        # psutil.exe 未被替换
    assert (info.file_version, info.is_admin_target, info.tool_is_admin, info.os_version,
            info.cpu, info.logical_cores, info.total_memory_bytes, info.create_time,
            info.captured_at) == (None,) * 9


@windows_only
def test_read_version_info_without_resource_is_all_none(tmp_path):
    plain = tmp_path / "plain.exe"
    plain.write_bytes(b"not a pe file")
    assert read_version_info(str(plain)) == {
        'file_version': None, 'product_version': None, 'product_name': None, 'company': None}


def test_four_part_fixed_version():
    assert target_info._four_part((1 << 16) | 2, (3 << 16) | 4) == "1.2.3.4"


def test_os_version_text_corrects_windows_11(monkeypatch):
    monkeypatch.setattr(target_info.platform, 'win32_ver',
                        lambda: ('10', '10.0.26200', 'SP0', 'Multiprocessor Free'))
    assert os_version_text() == "Windows 11 10.0.26200"
    monkeypatch.setattr(target_info.platform, 'win32_ver',
                        lambda: ('10', '10.0.19045', 'SP0', 'Multiprocessor Free'))
    assert os_version_text() == "Windows 10 10.0.19045"


# ---------------------------------------------------------------- 序列化


def _sample(**kw) -> TargetInfo:
    fields = dict(
        pid=4321, exe_path=r"C:\Program Files\Demo\app.exe", file_version="1.2.3.4",
        product_version="1.2", product_name="Demo", company="示例公司",
        cmdline=(r"C:\Program Files\Demo\app.exe", "--mode", "回归 测试"),
        create_time=datetime(2026, 11, 7, 9, 0, 0, 123456), is_admin_target=False,
        tool_version="2.0.0-beta.1", tool_is_admin=True, os_version="Windows 11 10.0.26200",
        cpu="Intel(R) Core(TM) i7", logical_cores=16, total_memory_bytes=34_000_000_000,
        dpi_scale=1.5, captured_at=NOW)
    fields.update(kw)
    return TargetInfo(**fields)


def test_json_roundtrip_is_lossless():
    info = _sample()
    text = info.to_json()
    data = json.loads(text)
    assert data['schema_version'] == 1
    assert data['cmdline'] == [r"C:\Program Files\Demo\app.exe", "--mode", "回归 测试"]
    assert data['create_time'] == "2026-11-07T09:00:00.123456"
    assert "示例公司" in text                       # ensure_ascii=False
    assert TargetInfo.from_json(text) == info
    assert TargetInfo.from_json(TargetInfo().to_json()) == TargetInfo()


def test_from_json_tolerates_old_and_new_shapes():
    # 整串命令行、未知键、缺失键、坏时间
    info = TargetInfo.from_json(json.dumps({
        'schema_version': 9, 'cmdline': 'app.exe --x', 'future_field': 1,
        'exe_path': 'a.exe', 'captured_at': 'not-a-time'}))
    assert info == TargetInfo(exe_path='a.exe', cmdline='app.exe --x')
    with pytest.raises(ValueError):
        TargetInfo.from_json('[1, 2]')
    with pytest.raises(ValueError):
        TargetInfo.from_json('{坏')
    assert target_info_from_json_or_none('{坏') is None
    assert target_info_from_json_or_none(None) is None
    assert target_info_from_json_or_none(_sample().to_json()) == _sample()


# ---------------------------------------------------------------- 摘要


def test_summary_lines_text():
    assert _sample().summary_lines() == [
        r"被测程序: C:\Program Files\Demo\app.exe（文件版本 1.2.3.4）",
        r'命令行: "C:\Program Files\Demo\app.exe" --mode "回归 测试"',
        "环境: Windows 11 10.0.26200 · 16 核 · 32 GB · 缩放 150% · 工具 2.0.0-beta.1（管理员）",
    ]
    # 被测提权、本工具非管理员、无缩放、小内存保留一位小数
    lines = _sample(is_admin_target=True, tool_is_admin=False, dpi_scale=None,
                    total_memory_bytes=8 * 1024 ** 3 + 300 * 1024 ** 2).summary_lines()
    assert lines[0].endswith("（文件版本 1.2.3.4，以管理员身份运行）")
    assert lines[2] == "环境: Windows 11 10.0.26200 · 16 核 · 8.3 GB · 工具 2.0.0-beta.1（非管理员）"


def test_summary_lines_partial_and_sanitized():
    assert TargetInfo().summary_lines() == []
    # 路径读不到但有版本（不常见）时标明未知；换行不会破坏 CSV 元数据行
    info = TargetInfo(file_version="1.0", cmdline="a.exe\r\n--evil")
    assert info.summary_lines() == ["被测程序: 未知（无权读取或进程已退出）（文件版本 1.0）",
                                    "命令行: a.exe  --evil"]
    long = TargetInfo(cmdline=("x" * (CMDLINE_SUMMARY_MAX_CHARS + 50),))
    line = long.summary_lines()[0]
    assert line.endswith("…") and len(line) == len("命令行: ") + CMDLINE_SUMMARY_MAX_CHARS + 1
    # 工具管理员状态未知时不写括注
    assert TargetInfo(tool_version="2.0.0").summary_lines() == ["环境: 工具 2.0.0"]
