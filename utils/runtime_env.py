"""只对监控工具自身生效的运行环境；启动被测程序时不传递工具的默认值。"""
import os
from typing import Dict

_OPENBLAS_THREADS = 'OPENBLAS_NUM_THREADS'
_default_applied = False


def configure_math_threads() -> None:
    """在图表依赖导入前限制 BLAS 线程；保留用户显式设置的值。"""
    global _default_applied
    if _OPENBLAS_THREADS not in os.environ:
        os.environ[_OPENBLAS_THREADS] = '1'
        _default_applied = True


def target_environment() -> Dict[str, str]:
    """复制当前环境，仅去掉本工具补入的默认值，避免改变被测程序的性能。"""
    env = dict(os.environ)
    if _default_applied and env.get(_OPENBLAS_THREADS) == '1':
        env.pop(_OPENBLAS_THREADS)
    return env
