"""
存储预估纯函数：样本行按 18 字节、每指标每分钟一行汇总按 120 字节；间隔超过 1 分钟时
汇总行随有样本的分钟数减少；文案为"每小时约 N 行 · 12 小时约 M"。
"""
import pytest

from core.storage_estimate import (
    BYTES_PER_ROLLUP_ROW, BYTES_PER_SAMPLE, StorageEstimate, estimate, estimate_tooltip,
    format_estimate, format_size,
)


def test_constants_match_v5_measurement():
    assert (BYTES_PER_SAMPLE, BYTES_PER_ROLLUP_ROW) == (18, 120)


@pytest.mark.parametrize("metrics, interval, expected", [
    # 5 指标 1 秒：18,000 行 × 18 + 5 × 60 × 120
    (5, 1, StorageEstimate(18_000, 360_000, 4_320_000, 8_640_000)),
    (26, 1, StorageEstimate(93_600, 1_872_000, 22_464_000, 44_928_000)),
    # 1 指标 60 秒：每分钟恰好一个样本、一行汇总
    (1, 60, StorageEstimate(60, 8_280, 99_360, 198_720)),
    # 2 指标 120 秒：只有一半的分钟有样本，汇总行同样减半
    (2, 120, StorageEstimate(60, 8_280, 99_360, 198_720)),
    # 不整除的间隔按小数算、最后取整
    (3, 7, StorageEstimate(1_543, 49_371, 592_457, 1_184_914)),
    (3, 0.5, StorageEstimate(21_600, 410_400, 4_924_800, 9_849_600)),
    (0, 1, StorageEstimate(0, 0, 0, 0)),
    (5, 0, StorageEstimate(0, 0, 0, 0)),
    (5, -1, StorageEstimate(0, 0, 0, 0)),
])
def test_estimate_table(metrics, interval, expected):
    assert estimate(metrics, interval) == expected


@pytest.mark.parametrize("size, expected", [
    (500, "1 KB"),
    (99_360, "97 KB"),
    (1024 * 1024, "1 MB"),
    (4_320_000, "4 MB"),
    (22_464_000, "21 MB"),
    (900 * 1024 * 1024, "900 MB"),
    (int(1.5 * 1024 ** 3), "1.5 GB"),
])
def test_format_size_table(size, expected):
    assert format_size(size) == expected


@pytest.mark.parametrize("metrics, interval, expected", [
    (5, 1, "每小时约 18,000 行 · 12 小时约 4 MB"),
    (1, 60, "每小时约 60 行 · 12 小时约 97 KB"),
    (26, 1, "每小时约 93,600 行 · 12 小时约 21 MB"),
    (0, 1, ""),
])
def test_format_estimate_table(metrics, interval, expected):
    assert format_estimate(estimate(metrics, interval)) == expected


def test_tooltip_explains_basis_and_24h():
    text = estimate_tooltip(5, 1)
    assert "5 项指标" in text and "每 1 秒" in text and "24 小时约 8 MB" in text
    assert "18 字节" in text and "120 字节" in text
    assert estimate_tooltip(0, 1) == ""
