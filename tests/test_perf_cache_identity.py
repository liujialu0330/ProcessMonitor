"""性能缓存必须绑定本地日期的 UTC 映射，不能把顺序追加错测为乱序重建。"""
import sqlite3
from datetime import timedelta

from data.mappers import to_ms
from tests.perf import synth


def test_current_and_legacy_cache_keys_change_with_timezone_mapping(monkeypatch):
    specs = [synth.TaskSpec('clock', 20000), synth.LegacySpec(rows=100, tasks=2)]
    original = [spec.digest() for spec in specs]
    original_to_ms = synth.to_ms
    monkeypatch.setattr(synth, 'to_ms', lambda value: original_to_ms(value) + 8 * 3600 * 1000)
    assert all(spec.digest() != before for spec, before in zip(specs, original))


def test_clock_identity_covers_interior_day_mapping(monkeypatch):
    spec = synth.days(3, 'daylight')
    original = spec.digest()
    original_to_ms = synth.to_ms
    # 起止偏移相同也不能掩盖中间日期的规则变化；仅替换纯函数，不改机器时区。
    def shifted(value):
        shift = 3600 * 1000 if (value - synth.T0).days == 1 else 0
        return original_to_ms(value) + shift
    monkeypatch.setattr(synth, 'to_ms', shifted)
    assert spec.digest() != original


def test_cache_key_is_stable_for_same_clock_and_parameters():
    spec = synth.TaskSpec('stable', 20000)
    assert spec.digest() == synth.TaskSpec('stable', 20000).digest()
    assert spec.params()['time_identity']['utc_start_ms'] == to_ms(synth.T0)


def test_synth_samples_end_before_next_append_and_record_clock_identity(tmp_path):
    spec = synth.TaskSpec('append', 8, hole_every_s=0, rounds=0)
    path = tmp_path / 'synthetic.db'
    info = synth.build_task_db(str(path), spec)
    with sqlite3.connect(path) as conn:
        first, last = conn.execute('SELECT MIN(ts_ms), MAX(ts_ms) FROM samples').fetchone()
    assert first == to_ms(synth.T0)
    assert last < to_ms(synth.T0 + timedelta(seconds=spec.duration_s))
    assert info['time_identity'] == spec.params()['time_identity']
