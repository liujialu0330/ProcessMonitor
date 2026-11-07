"""
单实例并发验收：10 个进程同时启动，恰好 1 个主实例，其余 9 个的激活消息全部送达。

旧方案"先连管道、连不上再 listen"在这种场景下会出现多个主实例；本用例守护
"命名互斥量为唯一判据 + 从实例退避重试连接"这一契约。
"""
import os
import subprocess
import sys
import time
import uuid

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESS_COUNT = 10
PRIMARY_LISTEN_MS = 6000

# 子进程：启动完成后报 READY，阻塞等父进程在 stdin 上发令，再同时开始单实例判定。
# 主实例监听 PRIMARY_LISTEN_MS 毫秒，统计收到的激活消息后退出
_CHILD_CODE = r'''
import sys
from PyQt5.QtCore import QCoreApplication, QTimer
app = QCoreApplication([])
from app.single_instance import SingleInstanceGuard
guard = SingleInstanceGuard(sys.argv[1], installer_mutex_name=None)
print("READY", flush=True)
sys.stdin.readline()
if guard.notify_existing_instance():
    print("ROLE=secondary", flush=True)
    sys.exit(0)
guard.listen()
count = [0]
guard.activation_requested.connect(lambda: count.__setitem__(0, count[0] + 1))
QTimer.singleShot(int(sys.argv[2]), app.quit)
app.exec_()
print("ROLE=primary", flush=True)
print("ACTIVATIONS=%d" % count[0], flush=True)
'''


@pytest.mark.skipif(sys.platform != 'win32', reason="命名互斥量判据仅 Windows 可用")
def test_ten_concurrent_launches_elect_exactly_one_primary():
    name = f"ProcessMonitorTest_{uuid.uuid4().hex}"
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
    started = time.monotonic()
    procs = [
        subprocess.Popen([sys.executable, '-c', _CHILD_CODE, name, str(PRIMARY_LISTEN_MS)],
                         cwd=PROJECT_ROOT, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
        for _ in range(PROCESS_COUNT)
    ]
    try:
        for proc in procs:
            line = proc.stdout.readline().strip()
            assert line == "READY", proc.stderr.read()
        # 所有进程都已就绪后几乎同时发令，制造真正的并发判定
        for proc in procs:
            proc.stdin.write("go\n")
            proc.stdin.flush()
        outputs = [proc.communicate(timeout=20) for proc in procs]
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    elapsed = time.monotonic() - started

    roles = [out.splitlines() for out, _ in outputs]
    primaries = [lines for lines in roles if "ROLE=primary" in lines]
    secondaries = [lines for lines in roles if "ROLE=secondary" in lines]
    detail = [(p.returncode, out, err[-500:]) for p, (out, err) in zip(procs, outputs)]
    assert len(primaries) == 1, detail
    assert len(secondaries) == PROCESS_COUNT - 1, detail
    assert all(p.returncode == 0 for p in procs), detail
    assert f"ACTIVATIONS={PROCESS_COUNT - 1}" in primaries[0], detail
    assert elapsed < 15, f"并发验收耗时 {elapsed:.1f} s 超过 15 s"
