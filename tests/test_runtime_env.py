"""工具的数学库线程默认值不能污染被测程序的环境。"""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize('original', [None, '1', '8', ''])
def test_thread_default_is_local_to_monitor(original):
    env = dict(os.environ)
    if original is None:
        env.pop('OPENBLAS_NUM_THREADS', None)
    else:
        env['OPENBLAS_NUM_THREADS'] = original
    code = '''
import json, os, sys
from utils.runtime_env import configure_math_threads, target_environment
configure_math_threads()
configure_math_threads()
child = target_environment()
print(json.dumps([os.environ.get('OPENBLAS_NUM_THREADS'),
                  child.get('OPENBLAS_NUM_THREADS'), 'numpy' in sys.modules]))
'''
    result = subprocess.run([sys.executable, '-c', code], env=env,
                            capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(result.stdout) == [original if original is not None else '1',
                                       original, False]


def test_launcher_receives_restored_target_environment(monkeypatch):
    from core import process_launcher
    from utils import runtime_env
    monkeypatch.delenv('OPENBLAS_NUM_THREADS', raising=False)
    monkeypatch.setattr(runtime_env, '_default_applied', False)
    runtime_env.configure_math_threads()
    captured = {}

    def popen(_command, **kwargs):
        captured.update(kwargs)
        raise OSError('测试：不创建真实进程')

    result = process_launcher.ProcessLauncher(popen=popen).launch(
        process_launcher.LaunchSpec(exe=sys.executable))
    assert not result.ok
    assert 'OPENBLAS_NUM_THREADS' not in captured['env']
    assert os.environ['OPENBLAS_NUM_THREADS'] == '1'
