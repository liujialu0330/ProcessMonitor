"""
泄漏排查相关的指标工具：预设、说明、变化量/增速格式化，以及"记住上次选择"配置项。
"""
import json

import pytest
from qfluentwidgets import qconfig

import app_config
from app_config import cfg, normalize_metrics, remember_last_metrics
from utils.metrics import (
    AVAILABLE_METRICS, LEAK_CHECK_PRESET, METRIC_DESCRIPTIONS,
    METRIC_DISPLAY_NAMES, MetricType, format_metric_delta, format_metric_rate,
    get_metric_description
)


@pytest.fixture(autouse=True)
def _restore_last_metrics():
    """last_metrics 不在全局 conftest 的复位清单里，这里前后各复位一次"""
    cfg.last_metrics.value = list(LEAK_CHECK_PRESET)
    yield
    cfg.last_metrics.value = list(LEAK_CHECK_PRESET)


# ---------- A1：预设 ----------

def test_leak_check_preset_content():
    """泄漏排查预设：私有字节、句柄数、GDI、USER、线程数，且都是可监控指标"""
    assert LEAK_CHECK_PRESET == [
        MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES,
        MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS,
        MetricType.NUM_THREADS,
    ]
    available = {m for group in AVAILABLE_METRICS.values() for m in group}
    assert set(LEAK_CHECK_PRESET) <= available


def test_last_metrics_defaults_to_preset():
    assert cfg.get(cfg.last_metrics) == LEAK_CHECK_PRESET


def test_normalize_metrics_filters_unknown_and_canonicalizes():
    """未知键丢弃、旧键归一、去重保序；全部无效或为空回落到预设"""
    assert normalize_metrics(
        ["num_threads", "bogus", "memory_vms", "memory_private", 5, None]
    ) == ["num_threads", "memory_private"]
    assert normalize_metrics([]) == LEAK_CHECK_PRESET
    assert normalize_metrics(["bogus"]) == LEAK_CHECK_PRESET
    assert normalize_metrics("memory_private") == LEAK_CHECK_PRESET
    assert normalize_metrics(None) == LEAK_CHECK_PRESET


def test_last_metrics_assignment_is_validated():
    """直接赋值非法内容也会被校验器纠正"""
    cfg.last_metrics.value = ["bogus", "memory_pagefile"]
    assert cfg.last_metrics.value == ["memory_private"]


def test_remember_last_metrics_does_not_write_when_config_not_loaded(tmp_path, monkeypatch):
    """配置未由应用加载（测试/独立脚本）时只更新内存，不往当前目录写文件"""
    monkeypatch.chdir(tmp_path)
    remember_last_metrics([MetricType.CPU_PERCENT])
    assert cfg.get(cfg.last_metrics) == [MetricType.CPU_PERCENT]
    assert not (tmp_path / "config").exists()


def test_last_metrics_roundtrip_through_config_file(tmp_path):
    """配置已加载时落盘；重新加载（含被手改的非法内容）后得到纠正后的列表"""
    path = tmp_path / "config.json"
    qconfig.load(str(path), cfg)

    remember_last_metrics([MetricType.NUM_HANDLES, MetricType.MEMORY_RSS])
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["Monitor"]["LastMetrics"] == ["memory_rss", "num_handles"] or \
        saved["Monitor"]["LastMetrics"] == ["num_handles", "memory_rss"]

    saved["Monitor"]["LastMetrics"] = ["bogus", "memory_vms"]
    path.write_text(json.dumps(saved), encoding="utf-8")
    qconfig.load(str(path), cfg)
    assert cfg.get(cfg.last_metrics) == ["memory_private"]


# ---------- A2：说明 ----------

def test_every_metric_has_one_sentence_description():
    """全部 27 项指标都有说明，且键集合与显示名一致"""
    assert len(METRIC_DESCRIPTIONS) == 27
    assert set(METRIC_DESCRIPTIONS) == set(METRIC_DISPLAY_NAMES)
    for metric, text in METRIC_DESCRIPTIONS.items():
        assert text.strip(), metric


def test_key_metric_descriptions_mention_leak_signals():
    assert "内存泄漏" in get_metric_description(MetricType.MEMORY_PRIVATE)
    gdi = get_metric_description(MetricType.NUM_GDI_OBJECTS)
    assert "10000" in gdi and "GDI" in gdi
    # 旧键经归一后同样能取到说明
    assert get_metric_description("memory_vms") == get_metric_description(
        MetricType.MEMORY_PRIVATE)
    assert get_metric_description("no_such_metric") == ""


# ---------- A5：变化量 / 增速格式化 ----------

def test_format_metric_delta_bytes_adaptive_unit_with_sign():
    assert format_metric_delta(MetricType.MEMORY_PRIVATE, 2048.0) == "+2 MB"
    assert format_metric_delta(MetricType.MEMORY_PRIVATE, -512.0) == "-512 KB"
    assert format_metric_delta(MetricType.MEMORY_PRIVATE, 3 * 1024 * 1024.0) == "+3 GB"


def test_format_metric_delta_zero_and_count():
    assert format_metric_delta(MetricType.MEMORY_PRIVATE, 0.0) == "±0"
    assert format_metric_delta(MetricType.NUM_HANDLES, 0) == "±0"
    assert format_metric_delta(MetricType.NUM_GDI_OBJECTS, 3) == "+3 个"
    assert format_metric_delta(MetricType.NUM_GDI_OBJECTS, -1200) == "-1,200 个"
    assert format_metric_delta(MetricType.CPU_PERCENT, 0.04) == "±0"
    assert format_metric_delta(MetricType.CPU_PERCENT, 1.26) == "+1.3%"
    assert format_metric_delta(MetricType.NUM_THREADS, None) == "--"


def test_format_metric_delta_fixed_display_unit():
    assert format_metric_delta(MetricType.MEMORY_PRIVATE, 512.0, "MB") == "+0.5 MB"


def test_format_metric_rate_bytes_and_counts():
    assert format_metric_rate(MetricType.MEMORY_PRIVATE, 12.5 * 1024) == "+12.5 MB/小时"
    assert format_metric_rate(MetricType.MEMORY_PRIVATE, -2048.0) == "-2 MB/小时"
    # 计数类允许小数，100 以上取整
    assert format_metric_rate(MetricType.NUM_GDI_OBJECTS, 12.5) == "+12.5 个/小时"
    assert format_metric_rate(MetricType.NUM_GDI_OBJECTS, 250.4) == "+250 个/小时"
    assert format_metric_rate(MetricType.NUM_HANDLES, 0.0) == "±0/小时"
    assert format_metric_rate(MetricType.NUM_HANDLES, 0.01) == "±0/小时"
    assert format_metric_rate(MetricType.MEMORY_PRIVATE, None) == "--"


def test_app_config_module_exports_are_consistent():
    """配置项的默认值就是预设，且复位后不会共享同一个列表对象"""
    assert cfg.last_metrics.defaultValue == LEAK_CHECK_PRESET
    assert app_config.cfg.last_metrics.defaultValue is not LEAK_CHECK_PRESET
