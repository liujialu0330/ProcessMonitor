"""采集分派表 SAMPLERS：键与指标注册表一致、各采样器单位换算、collect_metrics 分派与不可用归类。"""
import contextlib
import os
from types import SimpleNamespace

import psutil
import pytest

import core.process_collector as pc
from core.contracts import UnavailableReason
from core.process_collector import SAMPLERS, MetricNotSupported, ProcessCollector
from utils import metrics
from utils.metrics import DEPRECATED_METRICS, MetricType, all_metric_types

_FAKE_PID = 4321


def _fake_process(**overrides):
    """假 psutil.Process：各字段取值互不相同，便于发现取错字段/漏换算"""
    mem = SimpleNamespace(
        rss=10240 * 1024, peak_wset=20480 * 1024, private=3072 * 1024 + 512,
        peak_pagefile=4096 * 1024, paged_pool=1536, peak_paged_pool=2560,
        nonpaged_pool=768, peak_nonpaged_pool=1280, num_page_faults=12345)
    io = SimpleNamespace(
        read_bytes=5 * 1024 * 1024, write_bytes=3000, other_bytes=1024 * 7 + 256,
        read_count=11, write_count=22, other_count=33)
    attrs = dict(
        pid=_FAKE_PID,
        is_running=lambda: True,
        oneshot=contextlib.nullcontext,
        memory_info=lambda: mem,
        memory_full_info=lambda: SimpleNamespace(uss=900 * 1024),
        memory_percent=lambda: 12.5,
        cpu_percent=lambda interval=None: 37.5,
        num_threads=lambda: 17,
        num_handles=lambda: 345,
        io_counters=lambda: io,
        cpu_times=lambda: SimpleNamespace(user=1.25, system=0.75),
        nice=lambda: 32,
        num_ctx_switches=lambda: SimpleNamespace(voluntary=1000, involuntary=0),
    )
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


class _FakeKernel32:
    def __init__(self, handle=1):
        self.handle = handle
        self.closed = []

    def OpenProcess(self, *_):
        return self.handle

    def CloseHandle(self, h):
        self.closed.append(h)
        return True


class _FakeUser32:
    """GDI 返回 40、USER 返回 25"""

    def GetGuiResources(self, _handle, flag):
        return 40 if flag == pc.GR_GDIOBJECTS else 25


@pytest.fixture
def fake_win32(monkeypatch):
    """把 GetGuiResources 换成假实现，并假装运行在 Windows 上（非 Windows 也能跑）"""
    kernel32 = _FakeKernel32()
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(pc, "_init_win32", lambda: (kernel32, _FakeUser32()))
    monkeypatch.setattr(pc.ctypes, "set_last_error", lambda _v: None)
    return kernel32


def _collector(proc):
    c = ProcessCollector(_FAKE_PID)
    c._process = proc
    return c


# ---------- 键一致性 ----------

def test_sampler_keys_match_metric_registry():
    """可选指标 + 已废弃指标都要能采（旧任务"重新监控"仍会带上废弃键）"""
    assert set(SAMPLERS) == set(all_metric_types()) | set(DEPRECATED_METRICS)
    assert set(SAMPLERS) == set(metrics.METRIC_DISPLAY_NAMES)
    registry = getattr(metrics, "REGISTRY", None)
    if registry is not None:
        assert set(SAMPLERS) == set(registry)


# ---------- 逐个采样器的换算 ----------

# 改造前的 if/elif 实现对 _fake_process() 采到的结果，固化为期望值
_EXPECTED = {
    MetricType.MEMORY_RSS: 10240.0,
    MetricType.MEMORY_PERCENT: 12.5,
    MetricType.MEMORY_PEAK_WSET: 20480.0,
    MetricType.MEMORY_PRIVATE: 3072.5,
    MetricType.MEMORY_PEAK_PAGEFILE: 4096.0,
    MetricType.MEMORY_PAGED_POOL: 1.5,
    MetricType.MEMORY_PEAK_PAGED_POOL: 2.5,
    MetricType.MEMORY_NONPAGED_POOL: 0.75,
    MetricType.MEMORY_PEAK_NONPAGED_POOL: 1.25,
    MetricType.MEMORY_NUM_PAGE_FAULTS: 12345.0,
    MetricType.MEMORY_USS: 900.0,
    MetricType.CPU_PERCENT: 37.5,
    MetricType.CPU_USER_TIME: 1.25,
    MetricType.CPU_SYSTEM_TIME: 0.75,
    MetricType.CPU_PRIORITY: 32.0,
    MetricType.NUM_THREADS: 17.0,
    MetricType.NUM_HANDLES: 345.0,
    MetricType.NUM_GDI_OBJECTS: 40.0,
    MetricType.NUM_USER_OBJECTS: 25.0,
    MetricType.NUM_CTX_SWITCHES_VOL: 1000.0,
    MetricType.NUM_CTX_SWITCHES_INVOL: 0.0,
    MetricType.IO_READ_COUNT: 11.0,
    MetricType.IO_WRITE_COUNT: 22.0,
    MetricType.IO_READ_BYTES: 5120.0,
    MetricType.IO_WRITE_BYTES: 3000 / 1024,
    MetricType.IO_OTHER_COUNT: 33.0,
    MetricType.IO_OTHER_BYTES: 7.25,
}


def test_expected_table_covers_every_sampler():
    assert set(_EXPECTED) == set(SAMPLERS)


@pytest.mark.parametrize("metric_type", sorted(_EXPECTED))
def test_each_sampler_converts_to_storage_unit(fake_win32, metric_type):
    value = SAMPLERS[metric_type](_fake_process())
    assert isinstance(value, float)
    assert value == pytest.approx(_EXPECTED[metric_type])


def test_num_handles_falls_back_to_fds_without_num_handles():
    """非 Windows 的 Process 没有 num_handles：退回文件描述符数量"""
    proc = _fake_process(num_fds=lambda: 9)
    del proc.num_handles
    assert SAMPLERS[MetricType.NUM_HANDLES](proc) == 9.0


def test_gui_sampler_uses_process_pid_and_closes_handle(monkeypatch):
    opened = []

    class _K(_FakeKernel32):
        def OpenProcess(self, access, inherit, pid):
            opened.append(pid)
            return 77

    kernel32 = _K()
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(pc, "_init_win32", lambda: (kernel32, _FakeUser32()))
    monkeypatch.setattr(pc.ctypes, "set_last_error", lambda _v: None)
    assert SAMPLERS[MetricType.NUM_USER_OBJECTS](_fake_process()) == 25.0
    assert opened == [_FAKE_PID]
    assert kernel32.closed == [77]


# ---------- collect_metrics 分派 ----------

def test_collect_metrics_mixed_list_matches_old_implementation(fake_win32):
    keys = list(_EXPECTED)
    c = _collector(_fake_process())
    values = c.collect_metrics(keys)
    assert values == pytest.approx(_EXPECTED)
    assert list(values) == keys   # 结果按请求顺序
    assert c.last_unavailable == {}


def test_collect_metrics_skips_unknown_metric():
    c = _collector(_fake_process())
    values = c.collect_metrics(["no_such_metric", MetricType.NUM_THREADS])
    assert values == {MetricType.NUM_THREADS: 17.0}
    assert c.last_unavailable == {}


# ---------- 不可用路径 ----------

def _raiser(exc):
    def _f(*_a, **_k):
        raise exc
    return _f


@pytest.mark.parametrize("exc, reason", [
    (psutil.AccessDenied(_FAKE_PID), UnavailableReason.ACCESS_DENIED),
    # ZombieProcess 是 NoSuchProcess 的子类：只跳过该指标，不能当成进程消失
    (psutil.ZombieProcess(_FAKE_PID), UnavailableReason.PROCESS_EXITING),
    (RuntimeError("boom"), UnavailableReason.ERROR),
])
def test_sampler_exception_maps_to_reason(exc, reason):
    c = _collector(_fake_process(memory_full_info=_raiser(exc)))
    values = c.collect_metrics([MetricType.MEMORY_RSS, MetricType.MEMORY_USS])
    assert values == {MetricType.MEMORY_RSS: 10240.0}
    assert c.last_unavailable == {MetricType.MEMORY_USS: reason}


def test_sampler_no_such_process_returns_none():
    c = _collector(_fake_process(io_counters=_raiser(psutil.NoSuchProcess(_FAKE_PID))))
    assert c.collect_metrics([MetricType.MEMORY_RSS, MetricType.IO_READ_BYTES]) is None


def test_gui_metric_not_supported_off_windows(monkeypatch):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform="linux"))
    c = _collector(_fake_process())
    values = c.collect_metrics([MetricType.NUM_GDI_OBJECTS, MetricType.NUM_THREADS])
    assert values == {MetricType.NUM_THREADS: 17.0}
    assert c.last_unavailable == {MetricType.NUM_GDI_OBJECTS: UnavailableReason.NOT_SUPPORTED}


@pytest.mark.parametrize("last_error, reason", [
    (87, UnavailableReason.NOT_IN_SESSION),   # 其他会话的进程（session 0 服务）
    (5, UnavailableReason.ACCESS_DENIED),
])
def test_gui_metric_zero_with_last_error(fake_win32, monkeypatch, last_error, reason):
    class _ZeroUser32:
        def GetGuiResources(self, *_):
            return 0

    monkeypatch.setattr(pc, "_init_win32", lambda: (fake_win32, _ZeroUser32()))
    monkeypatch.setattr(pc.ctypes, "get_last_error", lambda: last_error)
    c = _collector(_fake_process())
    values = c.collect_metrics([MetricType.NUM_USER_OBJECTS, MetricType.NUM_THREADS])
    assert values == {MetricType.NUM_THREADS: 17.0}
    assert c.last_unavailable == {MetricType.NUM_USER_OBJECTS: reason}
    assert fake_win32.closed == [1]   # 失败也要关句柄


def test_gui_metric_zero_without_error_is_real_zero(fake_win32, monkeypatch):
    class _ZeroUser32:
        def GetGuiResources(self, *_):
            return 0

    monkeypatch.setattr(pc, "_init_win32", lambda: (fake_win32, _ZeroUser32()))
    monkeypatch.setattr(pc.ctypes, "get_last_error", lambda: 0)
    c = _collector(_fake_process())
    assert c.collect_metrics([MetricType.NUM_GDI_OBJECTS]) == {MetricType.NUM_GDI_OBJECTS: 0.0}
    assert c.last_unavailable == {}


@pytest.mark.parametrize("pid_exists, expect_none", [(True, False), (False, True)])
def test_gui_open_process_failure(fake_win32, monkeypatch, pid_exists, expect_none):
    """OpenProcess 失败：进程还在 -> 权限不足；进程已不在 -> 整体返回 None"""
    fake_win32.handle = 0
    monkeypatch.setattr(pc.psutil, "pid_exists", lambda _pid: pid_exists)
    c = _collector(_fake_process())
    values = c.collect_metrics([MetricType.NUM_GDI_OBJECTS, MetricType.NUM_THREADS])
    if expect_none:
        assert values is None
    else:
        assert values == {MetricType.NUM_THREADS: 17.0}
        assert c.last_unavailable == {
            MetricType.NUM_GDI_OBJECTS: UnavailableReason.ACCESS_DENIED}


def test_metric_not_supported_reason_passes_through(monkeypatch):
    monkeypatch.setitem(SAMPLERS, MetricType.MEMORY_USS,
                        _raiser(MetricNotSupported(UnavailableReason.NOT_IN_SESSION)))
    c = _collector(_fake_process())
    assert c.collect_metrics([MetricType.MEMORY_USS]) == {}
    assert c.last_unavailable == {MetricType.MEMORY_USS: UnavailableReason.NOT_IN_SESSION}


def test_real_current_process_collects_all_keys():
    """真实进程上把全部键跑一遍：值要么采到、要么记了原因，进程本身不会被判定消失"""
    c = ProcessCollector(os.getpid())
    values = c.collect_metrics(list(SAMPLERS))
    assert values is not None
    assert set(values) | set(c.last_unavailable) == set(SAMPLERS)
    assert values[MetricType.MEMORY_RSS] > 0
