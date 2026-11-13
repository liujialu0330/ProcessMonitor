"""CPU 差分时钟与调度时钟分离，避免 Windows 毫秒刻度损失。"""
from core import process_collector


def test_psutil_cpu_clock_uses_high_resolution_counter(monkeypatch):
    monkeypatch.setattr(process_collector.time, 'perf_counter', lambda: 123.456789)
    monkeypatch.setattr(process_collector.time, 'monotonic', lambda: 123.45)
    assert process_collector._monotonic() == 123.456789


def test_cpu_window_keeps_sub_millisecond_precision():
    collector = process_collector.ProcessCollector(1)
    assert collector._cpu_percent_step(1.0, 100.0) is None
    assert collector._cpu_percent_step(1.0005, 100.001) == 50.0
