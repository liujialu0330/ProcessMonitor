"""
端到端：pmctl 子进程自动拉起真实的无窗口实例（--background --data-dir <临时目录>），
用真实的 Windows 平台插件（去掉 QT_QPA_PLATFORM），对本 pytest 进程做监控：

ping（拉起）→ start → 经管道连发 100 次 marker（不丢不乱序）→ pmctl marker → wait →
EnumWindows 无可见顶层窗口 → stop → wait stopped → export（CSV / 报告图片 / 摘要）→
导出期间仍无可见窗口 → verdict → run（串联 + JUnit）→ quit（退出码 0）→ --no-launch 连不上。

整条链路要求 30 秒内完成；任何情况下 finally 都结束拉起的实例。
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes

import psutil
import pytest

from core.ipc import client as ipc
from core.ipc import protocol as p
from core.monitor_manager import BASELINE_LABEL, round_label
from utils.instance_name import pipe_path, single_instance_name

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason="命名管道与无窗口模式只在 Windows")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PMCTL = os.path.join(ROOT, 'pmctl.py')
BUDGET_S = 30.0


def visible_top_level_windows(pid: int):
    """pid 名下可见的顶层窗口（类名, 标题）。"""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    found = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            title = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, title, 256)
            found.append((cls.value, title.value))
        return True

    user32.EnumWindows(proc_type(callback), 0)
    return found


class Pmctl:
    def __init__(self, data_dir: str, env):
        self.data_dir = data_dir
        self.env = env

    def __call__(self, *args, timeout: float = 25.0):
        result = subprocess.run(
            [sys.executable, PMCTL, '--data-dir', self.data_dir, *args],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            env=self.env, cwd=ROOT, timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.returncode, result.stdout, result.stderr

    def json(self, *args, **kwargs):
        code, out, err = self('--json', *args, **kwargs)
        assert code == 0, (args, code, out, err)
        return json.loads(out.strip().splitlines()[-1])['result']


def _kill_instance(pid):
    if not pid:
        return
    try:
        proc = psutil.Process(pid)
        proc.kill()
        proc.wait(5)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass


def test_pmctl_drives_background_instance(tmp_path):
    began = time.monotonic()
    data_dir = str(tmp_path / 'data')
    out_dir = tmp_path / 'out'
    env = os.environ.copy()
    env.pop('QT_QPA_PLATFORM', None)       # 子进程用真实的 Windows 平台插件
    env.pop('PROCESS_MONITOR_DATA_DIR', None)
    env['PYTHONIOENCODING'] = 'utf-8'
    pm = Pmctl(data_dir, env)
    me = os.getpid()
    server_pid = None
    try:
        # ---- 拉起并就绪 ----
        info = pm.json('ping')
        server_pid = info['pid']
        assert info['ready'] is True and info['headless'] is True
        assert info['automation_enabled'] is True
        assert psutil.Process(server_pid).name().lower().startswith('python')
        # 再次 ping 不会拉起第二个
        assert pm.json('ping')['pid'] == server_pid

        # ---- 开始监控本 pytest 进程 ----
        started = pm.json('start', '--pid', str(me), '--metrics', 'memory_rss,num_handles',
                          '--interval', '1')
        task_id = started['task_id']
        assert started['pid'] == me

        # ---- 连发 100 次 marker：一次写入，按序回复 ----
        name = single_instance_name(os.path.abspath(data_dir))
        with ipc.IpcClient.connect(pipe_path(name)) as client:
            client.transport.write(b''.join(
                p.encode_request(i, 'marker', {'task_id': task_id}) for i in range(1, 101)))
            replies = [p.parse_response(p.decode_line(client.transport.read_line(10)))
                       for _ in range(100)]
        assert [r.id for r in replies] == list(range(1, 101))
        assert all(r.ok for r in replies), [r.error_message for r in replies if not r.ok][:3]
        labels = [r.result['markers'][0]['label'] for r in replies]
        assert labels == [BASELINE_LABEL] + [round_label(n) for n in range(1, 100)]

        code, out, err = pm('marker', '--task', task_id, '--label', '手动')
        assert code == 0, err
        status = pm.json('status')
        task = next(t for t in status['tasks'] if t['task_id'] == task_id)
        assert task['rounds'] == 100

        code, out, err = pm('wait', '--task', task_id, '--until', 'elapsed>=2',
                            '--timeout-s', '15')
        assert code == 0, (out, err)
        assert visible_top_level_windows(server_pid) == []

        # ---- 停止、等待、导出、判定 ----
        assert pm('stop', '--task', task_id)[0] == 0
        assert pm('wait', '--task', task_id, '--until', 'stopped', '--timeout-s', '15')[0] == 0
        exported = pm.json('export', '--task', task_id, '--dir', str(out_dir))
        assert visible_top_level_windows(server_pid) == []
        paths = exported['paths']
        assert len(paths) == 3 and all(os.path.isfile(path) for path in paths)
        assert any(path.endswith('.png') for path in paths)
        code, out, err = pm('verdict', '--task', task_id)
        assert code in (0, 10, 20), (code, out, err)
        # 已停止的任务不能再打点：invalid_state → 1
        assert pm('marker', '--task', task_id)[0] == ipc.EXIT_ERROR
        # 参数错误 → 2
        assert pm('export', '--task', task_id, '--dir', str(out_dir), '--kinds', 'pdf')[0] == 2

        # ---- run 串联 + JUnit ----
        junit = tmp_path / 'junit.xml'
        code, out, err = pm('run', '--pid', str(me), '--metrics', 'memory_rss', '--interval', '1',
                            '--rounds-every', '1', '--stop-after', '3',
                            '--out', str(tmp_path / 'run_out'), '--junit', str(junit))
        assert code in (0, 10, 20), (code, out, err)
        root = ET.parse(str(junit)).getroot()
        assert root.get('name') == '进程监控助手' and root.get('tests') == '1'
        assert len(os.listdir(tmp_path / 'run_out')) == 3

        # ---- 退出 ----
        assert pm('quit')[0] == 0
        assert psutil.Process(server_pid).wait(15) == 0
        server_pid = None
        code, out, err = pm('--no-launch', 'ping')
        assert code == ipc.EXIT_CONNECTION
    finally:
        _kill_instance(server_pid)
    elapsed = time.monotonic() - began
    print(f"端到端耗时 {elapsed:.1f} 秒")
    assert elapsed < BUDGET_S
