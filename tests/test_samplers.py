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


# ========== 快照路径（注入 SnapshotHub） ==========

from core.process_collector import SNAPSHOT_DERIVED, SNAPSHOT_FIELDS  # noqa: E402
from core.snapshot_hub import SnapshotHub  # noqa: E402
from core.system_snapshot import SnapshotError  # noqa: E402
from tests.doubles import (  # noqa: E402
    FakeSnapshotSource, ManualMonotonic, fake_process_sample, fake_system_snapshot,
)
from utils.metrics import COST_CHEAP, COST_EXPENSIVE, COST_SNAPSHOT, METRIC_COSTS  # noqa: E402

_SAMPLE_CREATE_TIME = 1_794_000_000.0


def test_every_metric_has_valid_cost_and_snapshot_table_matches():
    """注册表里标 snapshot 的指标恰好是采集器能从快照取的那些"""
    assert all(s.cost in METRIC_COSTS for s in metrics.REGISTRY.values())
    snapshot_keys = {k for k, s in metrics.REGISTRY.items() if s.cost == COST_SNAPSHOT}
    assert snapshot_keys == set(SNAPSHOT_FIELDS) | set(SNAPSHOT_DERIVED)
    assert metrics.REGISTRY[MetricType.MEMORY_USS].cost == COST_EXPENSIVE
    for key in (MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS, MetricType.CPU_PRIORITY):
        assert metrics.REGISTRY[key].cost == COST_CHEAP


def _snap(monotonic=10.0, **sample_fields):
    sample_fields.setdefault('create_time', _SAMPLE_CREATE_TIME)
    sample = fake_process_sample(_FAKE_PID, **sample_fields)
    return fake_system_snapshot([sample], monotonic=monotonic,
                                context_switches={_FAKE_PID: 1000})


def _hub_of(*snapshots, available=True):
    source = FakeSnapshotSource(snapshots=list(snapshots), available=available)
    return SnapshotHub(source, clock=ManualMonotonic()), source


def _snapshot_collector(hub, proc=None, max_age_s=-1.0):
    """max_age_s 缺省为负：手动时钟不走，每次 get 都取下一份快照"""
    c = ProcessCollector(_FAKE_PID, hub=hub, max_age_s=max_age_s)
    c._process = proc if proc is not None else _fake_process(
        create_time=lambda: _SAMPLE_CREATE_TIME)
    return c


def _psutil_forbidden(**keep):
    """快照能给的字段一律不许走 psutil：调用即失败（证明取值来自快照）"""
    boom = _raiser(AssertionError("不应调用 psutil"))
    attrs = dict(memory_info=boom, cpu_times=boom, num_threads=boom, num_handles=boom,
                 io_counters=boom, num_ctx_switches=boom, memory_percent=boom,
                 cpu_percent=boom, is_running=boom, create_time=lambda: _SAMPLE_CREATE_TIME)
    attrs.update(keep)
    return _fake_process(**attrs)


@pytest.mark.parametrize("metric_type", sorted(SNAPSHOT_FIELDS))
def test_snapshot_field_converts_like_psutil_sampler(metric_type):
    """快照字段换算到存储单位后与 psutil 采样器对同一组数值的结果一致"""
    hub, _ = _hub_of(_snap())
    c = _snapshot_collector(hub, _psutil_forbidden())
    values = c.collect_metrics([metric_type])
    assert values == {metric_type: pytest.approx(_EXPECTED[metric_type])}
    assert c.last_source == 'snapshot'
    assert c.last_unavailable == {}


def test_snapshot_mixed_list_keeps_order_and_uses_psutil_only_for_rest(fake_win32):
    keys = [MetricType.NUM_GDI_OBJECTS, MetricType.MEMORY_PRIVATE, MetricType.MEMORY_USS,
            MetricType.NUM_HANDLES, MetricType.CPU_PRIORITY, MetricType.NUM_USER_OBJECTS,
            MetricType.NUM_THREADS, MetricType.NUM_CTX_SWITCHES_VOL,
            MetricType.NUM_CTX_SWITCHES_INVOL]
    hub, _ = _hub_of(_snap())
    c = _snapshot_collector(hub, _psutil_forbidden())
    values = c.collect_metrics(keys)
    assert list(values) == keys
    assert values == pytest.approx({k: _EXPECTED[k] for k in keys})
    assert c.last_source == 'snapshot'


def test_snapshot_memory_percent_uses_working_set(monkeypatch):
    monkeypatch.setattr(pc, "_total_physical_memory", 40960 * 1024)
    hub, _ = _hub_of(_snap())
    c = _snapshot_collector(hub, _psutil_forbidden())
    assert c.collect_metrics([MetricType.MEMORY_PERCENT]) == {MetricType.MEMORY_PERCENT: 25.0}


def test_snapshot_cpu_percent_is_time_delta_over_wall_delta():
    """prime 记下起点（user+kernel=2.0 s，t=10），2 秒后 3.0 s → 50%；不预热首采为 0"""
    hub, _ = _hub_of(_snap(10.0, user_time=1.25, kernel_time=0.75),
                     _snap(12.0, user_time=1.5, kernel_time=1.5),
                     _snap(13.0, user_time=1.5, kernel_time=3.5))
    c = _snapshot_collector(hub, _psutil_forbidden(cpu_percent=lambda interval=None: 0.0))
    c.prime_cpu()
    assert c.collect_metrics([MetricType.CPU_PERCENT]) == {MetricType.CPU_PERCENT: 50.0}
    # 多核并行可超过 100%（与 psutil 同口径，按单核计）
    assert c.collect_metrics([MetricType.CPU_PERCENT]) == {MetricType.CPU_PERCENT: 200.0}

    hub2, _ = _hub_of(_snap(10.0))
    fresh = _snapshot_collector(hub2, _psutil_forbidden())
    assert fresh.collect_metrics([MetricType.CPU_PERCENT]) == {MetricType.CPU_PERCENT: 0.0}


def test_snapshot_cpu_percent_same_snapshot_twice_keeps_window():
    clock = ManualMonotonic()
    source = FakeSnapshotSource(snapshots=[
        _snap(10.0, user_time=1.0, kernel_time=1.0), _snap(12.0, user_time=2.0, kernel_time=1.0)])
    hub = SnapshotHub(source, clock=clock)
    c = _snapshot_collector(hub, _psutil_forbidden(cpu_percent=lambda interval=None: 0.0),
                            max_age_s=5)
    c.prime_cpu()
    assert c.collect_metrics([MetricType.CPU_PERCENT]) == {MetricType.CPU_PERCENT: 0.0}
    clock.advance(10)
    assert c.collect_metrics([MetricType.CPU_PERCENT]) == {MetricType.CPU_PERCENT: 50.0}


def test_snapshot_pid_reuse_returns_none():
    """快照里同 PID 的创建时间与绑定进程不同：PID 已被复用"""
    hub, _ = _hub_of(_snap(create_time=_SAMPLE_CREATE_TIME + 5))
    c = _snapshot_collector(hub, _psutil_forbidden())
    assert c.collect_metrics([MetricType.MEMORY_PRIVATE]) is None


def test_snapshot_identity_taken_from_first_sighting_without_psutil():
    hub, _ = _hub_of(_snap(), _snap(create_time=_SAMPLE_CREATE_TIME + 1))
    c = ProcessCollector(_FAKE_PID, hub=hub, max_age_s=0.0)
    assert c.collect_metrics([MetricType.NUM_THREADS]) == {MetricType.NUM_THREADS: 17.0}
    hub._clock.advance(1)
    assert c.collect_metrics([MetricType.NUM_THREADS]) is None


@pytest.mark.parametrize("running, expected_source", [(False, None), (True, 'psutil')])
def test_snapshot_missing_pid_rechecks_with_psutil(running, expected_source):
    """快照里没有该进程：psutil 复核，已不在返回 None，仍在则本周期回退 psutil"""
    hub, _ = _hub_of(fake_system_snapshot([fake_process_sample(1)]))
    c = _snapshot_collector(hub, _fake_process(is_running=lambda: running))
    values = c.collect_metrics([MetricType.NUM_THREADS])
    if expected_source is None:
        assert values is None
    else:
        assert values == {MetricType.NUM_THREADS: 17.0}
        assert c.last_source == expected_source


def test_snapshot_missing_pid_and_no_such_process_returns_none():
    hub, _ = _hub_of(fake_system_snapshot([]))
    c = _snapshot_collector(hub, _fake_process(
        is_running=_raiser(psutil.NoSuchProcess(_FAKE_PID))))
    assert c.collect_metrics([MetricType.NUM_THREADS]) is None


def test_unavailable_hub_falls_back_to_psutil_and_logs_once(caplog):
    hub, source = _hub_of(available=False)
    c = _snapshot_collector(hub)
    with caplog.at_level("WARNING", logger=pc.__name__):
        for _ in range(3):
            assert c.collect_metrics([MetricType.NUM_THREADS]) == {MetricType.NUM_THREADS: 17.0}
            assert c.last_source == 'psutil'
    assert len([r for r in caplog.records if "快照不可用" in r.getMessage()]) == 1
    assert source.calls == 0


def test_snapshot_failure_falls_back_for_that_cycle_only():
    hub, _ = _hub_of(SnapshotError("偶发"), _snap())
    c = _snapshot_collector(hub, _fake_process(create_time=lambda: _SAMPLE_CREATE_TIME))
    assert c.collect_metrics([MetricType.NUM_HANDLES]) == {MetricType.NUM_HANDLES: 345.0}
    assert c.last_source == 'psutil'
    assert c.collect_metrics([MetricType.NUM_HANDLES]) == {MetricType.NUM_HANDLES: 345.0}
    assert c.last_source == 'snapshot'


def test_snapshot_path_psutil_metric_no_such_process_returns_none():
    hub, _ = _hub_of(_snap())
    proc = _psutil_forbidden(memory_full_info=_raiser(psutil.NoSuchProcess(_FAKE_PID)))
    c = _snapshot_collector(hub, proc)
    assert c.collect_metrics([MetricType.NUM_THREADS, MetricType.MEMORY_USS]) is None


def test_without_hub_path_is_unchanged():
    """不注入 hub：仍逐项走 psutil（原路径）"""
    c = _collector(_fake_process())
    assert c.collect_metrics([MetricType.NUM_THREADS]) == {MetricType.NUM_THREADS: 17.0}
    assert c.last_source == 'psutil'