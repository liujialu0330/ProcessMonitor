"""参考唯一性、身份保留、星标与批量删除的事务边界。"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.monitor_manager import MonitorManager
from core.reference import compare_analyses, estimate_rounds, load_reference_comparison
from core.history_query import TaskAnalysis
from data.models import DataPoint, MonitorTask
from data.errors import StorageError


def task(db, key, scenario='打开窗口', build='1.0', status='stopped'):
    now = datetime(2026, 11, 12)
    record = MonitorTask(key, 123, 'app.exe', ['memory_private'], 1, now,
                         now + timedelta(seconds=10), status, scenario=scenario, build=build)
    db.tasks.save(record)
    db.points.append([DataPoint(key, now, 1, 'memory_private')])
    return db.tasks.get(key)


def test_reference_is_unique_and_other_scenarios_are_independent(db):
    for key, scene in [('a', '打开窗口'), ('b', '打开窗口'), ('c', '切换页面')]:
        task(db, key, scene)
        db.tasks.set_reference(key)
    assert not db.tasks.get('a').is_reference
    assert db.tasks.reference_for('打开窗口').task_id == 'b'
    assert db.tasks.reference_for('切换页面').task_id == 'c'
    db.tasks.update_identity('b', '切换页面', '2.0')
    assert db.tasks.reference_for('打开窗口') is None
    assert not db.tasks.get('b').is_reference
    assert db.tasks.reference_for('切换页面').task_id == 'c'


def test_retention_cleanup_never_silently_deletes_scenario_reference(db):
    from dataclasses import replace
    for key in ('reference', 'ordinary'):
        record = task(db, key)
        db.tasks.save(replace(record, start_time=datetime(2000, 1, 1),
                              end_time=datetime(2000, 1, 2)))
    db.tasks.set_reference('reference')
    assert db.maintenance.cleanup_older_than(7) == 1
    assert db.tasks.reference_for('打开窗口').task_id == 'reference'
    assert db.tasks.get('ordinary') is None


def test_recovery_keeps_plan_group_and_identity_in_running_model(db, qapp, monkeypatch):
    task(db, 'recover', status='running', scenario='方案场景', build='测试版本')
    db.tasks.set_group(['recover'], 'shared-plan-group')
    manager = MonitorManager(db)
    monkeypatch.setattr(manager, 'start_task', lambda task_id: True)
    try:
        assert manager.recover_task('recover')
        assert manager.task_group('recover') == 'shared-plan-group'
        record = manager._tasks['recover'].task_model
        assert (record.scenario, record.build) == ('方案场景', '测试版本')
    finally:
        manager.remove_task('recover')


@pytest.mark.parametrize('scene,status', [('', 'stopped'), ('窗口', 'running')])
def test_reference_requires_scene_and_finished_task(db, scene, status):
    task(db, 'a', scene, status=status)
    with pytest.raises(ValueError):
        db.tasks.set_reference('a')


def test_identity_survives_stale_worker_save_and_probe(db):
    original = task(db, 'a')
    db.tasks.update_identity('a', '新版场景', '用户版本')
    db.tasks.save(original)
    db.tasks.set_default_build('a', '探针版本')
    db.tasks.set_starred('a', True)
    db.tasks.save_meta('a', '{"file_version":"探针版本"}')
    current = db.tasks.get('a')
    assert (current.scenario, current.build, current.starred) == ('新版场景', '用户版本', True)


def test_star_and_quality_and_stop_metadata_survive_each_other(db):
    from data.models import QualityStats
    task(db, 'a')
    db.tasks.save_stop('a', 'user', None)
    db.tasks.save_quality('a', QualityStats(3, 3, 0), histograms={'version': 1})
    db.tasks.set_starred('a', True)
    db.tasks.save_meta('a', '{"file_version":"2.0"}')
    meta = json.loads(db.tasks.load_meta('a'))
    assert meta['starred'] is True
    assert meta['stop']['reason'] == 'user'
    assert meta['quality_histograms'] == {'version': 1}


def test_batch_deletion_validates_every_target_before_deleting(db):
    task(db, 'a')
    task(db, 'b', status='running')
    with pytest.raises(ValueError):
        db.tasks.delete_many_stopped(['a', 'b'])
    assert db.tasks.get('a') is not None
    db.tasks.update_status('b', 'stopped')
    db.tasks.set_reference('b')
    with pytest.raises(ValueError):
        db.tasks.delete_many_stopped(['a', 'b'])
    assert db.tasks.get('a') is not None
    assert db.tasks.delete_many_stopped(['a', 'b'], allow_references=True) == 2
    assert db.tasks.list_all() == []


def test_single_delete_cannot_bypass_reference_confirmation(db, qapp):
    task(db, 'a')
    db.tasks.set_reference('a')
    manager = MonitorManager(db)
    assert not manager.delete_task_data('a')
    assert db.tasks.get('a') is not None
    assert manager.delete_task_data('a', allow_reference=True)


def test_cancelled_batch_deletion_rolls_back(db):
    task(db, 'a')
    task(db, 'b')
    calls = []
    def cancelled():
        calls.append(1)
        return len(calls) > 1
    with pytest.raises((ValueError, StorageError)):
        db.tasks.delete_many_stopped(['a', 'b'], cancelled=cancelled)
    assert db.tasks.get('a') and db.tasks.get('b')


def snap(values, *, baseline=True):
    return SimpleNamespace(rounds=[SimpleNamespace(delta=value) for value in values],
                           baseline=object() if baseline else None, baseline_comparable=True)


def analysis(key, values):
    return TaskAnalysis(key, 'all', {'memory_private': snap(values)})


def test_reference_regression_requires_nonoverlapping_intervals():
    result = compare_analyses(analysis('new', [10, 10, 10]), analysis('ref', [1, 1, 1]))
    assert result.status == 'regression' and result.rows[0].delta == 9
    overlap = compare_analyses(analysis('new', [0, 10, 20]), analysis('ref', [1, 2, 3]))
    assert overlap.status == 'comparable'
    lower = compare_analyses(analysis('new', [-10, -10, -10]), analysis('ref', [1, 1, 1]))
    assert lower.status == 'comparable'


@pytest.mark.parametrize('snapshot,reason', [(snap([1, 2]), '完整轮次'),
    (snap([1, None, 3]), '缺失'), (snap([1, 2, 3], baseline=False), '基线')])
def test_reference_missing_data_is_not_misreported_as_stable(snapshot, reason):
    estimate, problem = estimate_rounds(snapshot)
    assert estimate is None and reason in problem


def test_reference_lookup_handles_unconfigured_scene_and_self(db):
    task(db, 'a', scenario='')
    assert '场景' in load_reference_comparison(db, 'a').reason
    db.tasks.update_identity('a', '窗口', '')
    assert '没有参考' in load_reference_comparison(db, 'a').reason
    db.tasks.set_reference('a')
    assert '就是' in load_reference_comparison(db, 'a').reason


def test_reference_comparison_loads_real_round_samples(db):
    start = datetime(2026, 11, 12)
    for key, step in [('new', 10), ('ref', 1)]:
        task(db, key)
        db.points.append([DataPoint(key, start + timedelta(seconds=i), float(step * i),
                                    'memory_private') for i in range(40)])
        for i, second in enumerate((0, 10, 20, 30)):
            db.markers.add(key, start + timedelta(seconds=second), f'轮次{i}',
                           kind='baseline' if i == 0 else 'user')
    db.tasks.set_reference('ref')
    result = load_reference_comparison(db, 'new')
    assert result.status == 'regression'
    assert result.reference_id == 'ref'
    assert result.rows[0].current.count == result.rows[0].reference.count == 3
    assert result.rows[0].current.mean == 100
    assert result.rows[0].reference.mean == 10


def test_pending_identity_and_group_are_saved_on_first_sample(db, qapp):
    manager = MonitorManager(db, target_probe=lambda *_a, **_kw: None)
    key = manager.create_task(123, 'app.exe', ['memory_private'])
    try:
        assert manager.update_task_identity(key, '窗口', '2.0')
        manager.set_task_group([key], 'plan-1')
        worker = manager.get_task(key)
        worker.task_model.start_time = datetime(2026, 11, 12)
        db.tasks.save(worker.task_model)
        manager._on_task_data_updated(key, {})
        record = db.tasks.get(key)
        assert (record.scenario, record.build, record.group_id) == ('窗口', '2.0', 'plan-1')
        assert manager.task_group(key) == 'plan-1'
    finally:
        manager.remove_task(key)
