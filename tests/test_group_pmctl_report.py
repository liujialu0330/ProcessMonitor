"""
pmctl 的进程组参数（--tree / --group-name）与进程组任务的明细 CSV（只含合计、末尾附成员汇总段）、
JUnit 里的成员用例。
"""
import csv
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import pytest

import pmctl
from core.ipc import client as ipc
from core.report import member_summary_table
from core.report_worker import ReportJob, ReportWorker
from core.workers.base import CancelToken
from data.models import DataPoint, MemberRecord, MonitorTask
from data.mappers import to_ms
from data.repos.member_repo import MemberSummary
from tests.test_pmctl import FakeSession, ok, run_cli

BASE = datetime(2026, 3, 1, 9, 0, 0)


# ========== pmctl 参数 ==========

def test_start_tree_payload():
    session = FakeSession({'start': ok({'task_id': 'T'})})
    code, out, *_ = run_cli(['start', '--tree', '--pid', '123'], session)
    assert code == 0
    assert session.calls[0][1] == {'target_kind': 'tree', 'pid': 123}


def test_start_group_name_payload():
    session = FakeSession({'start': ok({'task_id': 'T'})})
    code, *_ = run_cli(['start', '--group-name', 'chrome.exe', '--metrics', 'memory_private'],
                       session)
    assert code == 0
    assert session.calls[0][1] == {'target_kind': 'name', 'name': 'chrome.exe',
                                   'metrics': ['memory_private']}


def test_start_group_name_with_wait_sends_wait():
    session = FakeSession({'start': ok({'task_id': 'T'})})
    run_cli(['start', '--group-name', 'later.exe', '--wait'], session)
    assert session.calls[0][1]['wait'] is True and session.calls[0][2] is None


def test_start_launch_with_tree():
    session = FakeSession({'start': ok({'task_id': 'T'})})
    code, *_ = run_cli(['start', '--tree', '--launch', 'C:\\x\\app.exe'], session)
    assert code == 0
    payload = session.calls[0][1]
    assert payload['target_kind'] == 'tree' and payload['launch']['exe'].endswith('app.exe')


@pytest.mark.parametrize('argv', [
    ['start', '--tree', '--name', 'a.exe'],        # tree 的根必须是 pid / launch
    ['start', '--tree', '--group-name', 'a.exe'],
    ['start', '--pid', '1', '--group-name', 'a.exe'],   # 互斥
    ['run', '--tree', '--name', 'a.exe', '--stop-after', '5'],
    ['run', '--tree', '--group-name', 'a.exe', '--stop-after', '5'],
    ['run', '--tree', '--stop-after', '5'],
])
def test_bad_group_option_combinations_exit_2(argv):
    code, out, errs, session = run_cli(argv)
    assert code == ipc.EXIT_USAGE
    assert session.calls == []


def test_run_applies_tree_to_pid_targets_and_group_name_to_names():
    session = FakeSession({
        'start': lambda payload: ok({'task_id': 'T', 'name': 'x.exe', 'pid': payload.get('pid', 0)}),
        'wait': ok({'reached': True}),
        'verdict': ok({'verdict': 'stable', 'metrics': []}),
    })
    code, *_ = run_cli(['run', '--tree', '--pid', '11', '--stop-after', '5'], session)
    assert code == 0
    starts = [c[1] for c in session.calls if c[0] == 'start']
    assert starts[0]['target_kind'] == 'tree' and starts[0]['pid'] == 11

    session = FakeSession({
        'start': ok({'task_id': 'T', 'name': 'x.exe', 'pid': 0}),
        'wait': ok({'reached': True}),
        'verdict': ok({'verdict': 'stable', 'metrics': []}),
    })
    run_cli(['run', '--group-name', 'a.exe', '--group-name', 'b.exe', '--stop-after', '5'],
            session)
    starts = [c[1] for c in session.calls if c[0] == 'start']
    assert [(s['target_kind'], s['name']) for s in starts] == [('name', 'a.exe'),
                                                               ('name', 'b.exe')]


def test_status_shows_group_kind():
    tasks = [{'task_id': 'T1', 'name': 'app.exe', 'pid': 5, 'status': 'running',
              'paused': False, 'rounds': 0, 'target_kind': 'tree'},
             {'task_id': 'T2', 'name': 'chrome.exe', 'pid': 0, 'status': 'running',
              'paused': False, 'rounds': 0, 'target_kind': 'name'},
             {'task_id': 'T3', 'name': 'x.exe', 'pid': 9, 'status': 'running',
              'paused': False, 'rounds': 0}]
    code, out, *_ = run_cli(['status'], FakeSession({'status': ok({'tasks': tasks})}))
    lines = out.splitlines()
    assert '进程树，PID 5' in lines[0]
    assert 'chrome.exe（同名进程组）' in lines[1]
    assert 'x.exe（PID 9）' in lines[2]


def test_verdict_output_lists_members():
    result = {'verdict': 'rising', 'metrics': [
        {'key': 'process_count', 'name': '进程总数', 'verdict': 'stable',
         'rate_per_hour': 0, 'rate_per_round': None}],
        'members': [{'key': 'leak.exe', 'verdict': 'rising', 'metrics': [
            {'key': 'process_count@leak.exe', 'name': '进程总数（leak.exe）',
             'verdict': 'rising', 'rate_per_hour': 12.0, 'rate_per_round': None}]}]}
    code, out, *_ = run_cli(['verdict', '--task', 'T'], FakeSession({'verdict': ok(result)}))
    assert code == 10
    assert '成员 leak.exe：持续上涨' in out
    assert '进程总数（leak.exe）：rising' in out


def test_junit_has_a_case_per_member_metric_and_rising_member_fails():
    result = {'target': 'app.exe', 'verdict': 'rising', 'metrics': [
        {'key': 'process_count', 'name': '进程总数', 'verdict': 'stable'}],
        'members': [{'key': 'leak.exe', 'verdict': 'rising', 'metrics': [
            {'key': 'process_count@leak.exe', 'name': '进程总数（leak.exe）',
             'verdict': 'rising'}]}]}
    suite = pmctl.build_junit([result]).getroot()
    cases = {c.get('name'): c for c in suite.findall('testcase')}
    assert set(cases) == {'进程总数', '进程总数（leak.exe）'}
    assert cases['进程总数（leak.exe）'].find('failure') is not None
    assert cases['进程总数'].find('failure') is None
    assert suite.get('tests') == '2' and suite.get('failures') == '1'


# ========== 明细 CSV ==========

def seed_group_task(db, kind='tree'):
    task = MonitorTask(task_id='g-csv', pid=100, process_name='app.exe',
                       metric_types=['memory_private', 'process_count'], interval=1.0,
                       start_time=BASE, end_time=BASE + timedelta(seconds=4), status='stopped',
                       target_kind=kind, target_spec='{}')
    db.save_task(task)
    points = []
    for i in range(5):
        stamp = BASE + timedelta(seconds=i)
        points += [DataPoint(task.task_id, stamp, 300.0 + i, 'memory_private'),
                   DataPoint(task.task_id, stamp, 3.0, 'process_count'),
                   DataPoint(task.task_id, stamp, 200.0 + i, 'memory_private@worker.exe'),
                   DataPoint(task.task_id, stamp, 2.0, 'process_count@worker.exe'),
                   DataPoint(task.task_id, stamp, 100.0, 'memory_private@app.exe'),
                   DataPoint(task.task_id, stamp, 1.0, 'process_count@app.exe')]
    assert db.save_data_points(points)
    ms = lambda s: to_ms(BASE + timedelta(seconds=s))  # noqa: E731
    db.members.apply([MemberRecord(task.task_id, 100, 1.0, 'app.exe', ms(0), ms(4)),
                      MemberRecord(task.task_id, 201, 2.0, 'Worker.exe', ms(0), ms(2)),
                      MemberRecord(task.task_id, 202, 3.0, 'worker.exe', ms(2), ms(4))])
    return db.tasks.get(task.task_id)


def read_csv(path):
    with open(path, encoding='utf-8-sig') as f:
        return list(csv.reader(f))


def test_group_detail_csv_has_totals_only_then_member_summary(qapp, db, tmp_path):
    task = seed_group_task(db)
    path = str(tmp_path / 'g.csv')
    result = ReportWorker(db, ReportJob(task=task, csv_path=path, gap_seconds=None)).execute(
        CancelToken())
    rows = read_csv(path)
    header = rows[0]
    assert header[:2] == ['时间', '已运行(秒)']
    assert 'memory_private@worker.exe' not in ' '.join(header)
    detail = rows[1:6]
    # 明细 5 行，都是合计值（成员序列的 200 / 100 不出现在明细里）
    assert [float(r[4]) for r in detail] == [300.0, 301.0, 302.0, 303.0, 304.0]
    assert [r[5] for r in detail] == ['3'] * 5
    assert result.row_count == 5 and result.point_count == 10
    assert rows[6] == []
    assert rows[7] == ['成员汇总']
    summary_header = rows[8]
    assert summary_header[:4] == ['成员', '实例数', '首次出现', '最后出现']
    members = {r[0]: r for r in rows[9:]}
    assert set(members) == {'app.exe', 'Worker.exe'}
    worker = members['Worker.exe']
    assert worker[1] == '2'
    assert worker[2] == '2026-03-01 09:00:00' and worker[3] == '2026-03-01 09:00:04'
    # 各指标末值：成员序列的最后一个采样
    assert float(worker[4]) == 204.0 and worker[5] == '2'
    assert worker[2] == '2026-03-01 09:00:00' and members['app.exe'][1] == '1'


def test_pid_task_csv_has_no_member_segment(qapp, db, tmp_path):
    task = MonitorTask(task_id='p-csv', pid=1, process_name='a.exe',
                       metric_types=['memory_private'], interval=1.0, start_time=BASE,
                       end_time=BASE + timedelta(seconds=2), status='stopped')
    db.save_task(task)
    db.save_data_points([DataPoint('p-csv', BASE + timedelta(seconds=i), 1.0 + i,
                                   'memory_private') for i in range(3)])
    path = str(tmp_path / 'p.csv')
    ReportWorker(db, ReportJob(task=db.tasks.get('p-csv'), csv_path=path)).execute(CancelToken())
    rows = read_csv(path)
    assert not any(r == ['成员汇总'] for r in rows)
    assert len(rows) == 4


def test_group_csv_without_member_trail_has_no_summary_segment(qapp, db, tmp_path):
    task = seed_group_task(db)
    with db._factory.transaction() as cursor:
        cursor.execute("DELETE FROM task_members WHERE task_id = ?", (task.task_id,))
    path = str(tmp_path / 'g.csv')
    ReportWorker(db, ReportJob(task=task, csv_path=path)).execute(CancelToken())
    assert not any(r == ['成员汇总'] for r in read_csv(path))


def test_member_summary_table_formats_last_values():
    task = MonitorTask(task_id='t', pid=0, process_name='x', metric_types=['memory_private',
                                                                           'process_count'],
                       interval=1.0, start_time=BASE, end_time=None, status='stopped')
    members = [MemberSummary('a.exe', 'A.exe', 3, BASE, BASE + timedelta(seconds=60))]
    header, rows = member_summary_table(
        task, members, {('a.exe', 'process_count'): 2.0})
    assert header[0:4] == ['成员', '实例数', '首次出现', '最后出现']
    assert header[-1].startswith('进程总数') and header[-1].endswith('末值')
    assert rows == [['A.exe', 3, '2026-03-01 09:00:00', '2026-03-01 09:01:00', '', '2']]
