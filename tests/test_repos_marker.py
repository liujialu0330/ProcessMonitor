"""
MarkerRepo：add 返回 id、kind 校验、since/until 过滤、改名/删除/改类型、
v3 库（无 kind 列）读取兼容、datetime 强校验与 StorageError 包装
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from data.database import Database
from data.errors import StorageError
from data.models import Marker
from data.repos.marker_repo import MarkerRepo
from tests.helpers import ConnectionFactoryStub, make_broken_factories, make_v4_db

BASE = datetime(2026, 3, 1, 9, 0, 0)


@pytest.fixture
def factory(tmp_path):
    return ConnectionFactoryStub(make_v4_db(str(tmp_path / 'repo.db')))


@pytest.fixture
def repo(factory):
    return MarkerRepo(factory)


def test_add_returns_id_and_list_roundtrip(repo):
    first = repo.add('t', BASE, '开始扫描')
    second = repo.add('t', BASE + timedelta(seconds=5), '基线', kind='baseline')
    repo.add('other', BASE, '别的任务')
    assert isinstance(first, int) and second > first

    assert repo.list('t') == [
        Marker('t', BASE, '开始扫描', first, 'user'),
        Marker('t', BASE + timedelta(seconds=5), '基线', second, 'baseline'),
    ]
    assert repo.list('no-such-task') == []


def test_list_same_timestamp_ordered_by_id(repo):
    ids = [repo.add('t', BASE, f'm{i}') for i in range(3)]
    assert [m.id for m in repo.list('t')] == ids


def test_add_validates_kind_and_timestamp(repo):
    with pytest.raises(ValueError):
        repo.add('t', BASE, 'x', kind='bogus')
    with pytest.raises(TypeError):
        repo.add('t', BASE.timestamp(), 'x')
    with pytest.raises(TypeError):
        repo.add('t', BASE.isoformat(), 'x')
    assert repo.list('t') == []


def test_list_since_until_inclusive(repo):
    for i in range(5):
        repo.add('t', BASE + timedelta(minutes=i), f'm{i}')
    labels = lambda **kw: [m.label for m in repo.list('t', **kw)]  # noqa: E731
    assert labels(since=BASE + timedelta(minutes=2)) == ['m2', 'm3', 'm4']
    assert labels(until=BASE + timedelta(minutes=1)) == ['m0', 'm1']
    assert labels(since=BASE + timedelta(minutes=1), until=BASE + timedelta(minutes=3)) == [
        'm1', 'm2', 'm3']
    with pytest.raises(TypeError):
        repo.list('t', since=BASE.timestamp())
    with pytest.raises(TypeError):
        repo.list('t', until=0)


def test_update_label(repo):
    marker_id = repo.add('t', BASE, '旧名')
    assert repo.update_label(marker_id, '新名') is True
    assert repo.list('t')[0].label == '新名'
    assert repo.update_label(marker_id + 100, 'x') is False


def test_delete(repo):
    keep = repo.add('t', BASE, 'keep')
    drop = repo.add('t', BASE, 'drop')
    assert repo.delete(drop) is True
    assert [m.id for m in repo.list('t')] == [keep]
    assert repo.delete(drop) is False


def test_set_kind(repo):
    marker_id = repo.add('t', BASE, 'x')
    for kind in ('pause', 'resume', 'baseline', 'user'):
        assert repo.set_kind(marker_id, kind) is True
        assert repo.list('t')[0].kind == kind
    assert repo.set_kind(marker_id + 100, 'pause') is False
    with pytest.raises(ValueError):
        repo.set_kind(marker_id, 'bogus')
    assert repo.list('t')[0].kind == 'user'


def test_list_on_v3_db_without_kind_column(tmp_path):
    db_path = str(tmp_path / 'v3.db')
    db = Database(db_path)
    db.add_marker('t', BASE, '旧打点')
    # Database 建出 v4 后这里仍要模拟 v3：去掉 kind 列
    conn = sqlite3.connect(db_path)
    try:
        if 'kind' in {row[1] for row in conn.execute('PRAGMA table_info(task_markers)')}:
            conn.execute('ALTER TABLE task_markers DROP COLUMN kind')
            conn.commit()
    finally:
        conn.close()
    markers = MarkerRepo(ConnectionFactoryStub(db_path)).list('t')
    assert [(m.label, m.kind) for m in markers] == [('旧打点', 'user')]
    assert markers[0].id is not None


def test_all_methods_wrap_sqlite_errors(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        repo = MarkerRepo(factory)
        calls = [
            lambda: repo.add('t', BASE, 'x'),
            lambda: repo.list('t', since=BASE, until=BASE),
            lambda: repo.update_label(1, 'x'),
            lambda: repo.delete(1),
            lambda: repo.set_kind(1, 'pause'),
        ]
        for call in calls:
            with pytest.raises(StorageError) as info:
                call()
            assert isinstance(info.value.__cause__, sqlite3.Error)
