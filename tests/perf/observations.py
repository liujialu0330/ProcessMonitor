"""仅在性能阶段边界采集现场，不在被计时函数里取系统快照或写日志。"""
import ctypes
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import platform
import sys
import time


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _windows_state() -> dict:
    if sys.platform != 'win32':
        return {'supported': False}
    from ctypes import wintypes

    class PowerStatus(ctypes.Structure):
        _fields_ = [('ac', wintypes.BYTE), ('battery_flag', wintypes.BYTE),
                    ('battery_percent', wintypes.BYTE), ('saver', wintypes.BYTE),
                    ('remaining_s', wintypes.DWORD), ('full_s', wintypes.DWORD)]

    class Throttle(ctypes.Structure):
        _fields_ = [('version', wintypes.ULONG), ('control_mask', wintypes.ULONG),
                    ('state_mask', wintypes.ULONG)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcessorNumber.restype = wintypes.DWORD
    kernel.GetSystemPowerStatus.argtypes = [ctypes.POINTER(PowerStatus)]
    kernel.GetSystemPowerStatus.restype = wintypes.BOOL
    kernel.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
    kernel.GetProcessInformation.restype = wintypes.BOOL
    power, throttle = PowerStatus(), Throttle(1, 0, 0)
    state = {'supported': True, 'processor_sample': int(kernel.GetCurrentProcessorNumber())}
    if kernel.GetSystemPowerStatus(ctypes.byref(power)):
        state['power'] = {name: getattr(power, name) for name, _ in power._fields_}
    else:
        state['power_error'] = ctypes.get_last_error()
    # ProcessPowerThrottling=4，版本1；mask为0仅表示系统自动管理，不表示有效QoS为High。
    if kernel.GetProcessInformation(kernel.GetCurrentProcess(), 4,
                                    ctypes.byref(throttle), ctypes.sizeof(throttle)):
        state['explicit_power_throttling'] = {
            name: getattr(throttle, name) for name, _ in throttle._fields_}
    else:
        state['throttling_error'] = ctypes.get_last_error()
    return state


def snapshot() -> dict:
    """累计CPU/IO计数可作阶段差值；采样点落核不是全阶段驻留证明。"""
    import psutil
    result = {'utc': utc_now(), 'wall_s': time.perf_counter(),
              'process_cpu_s': time.process_time(), 'pid': os.getpid()}
    try:
        process = psutil.Process()
        with process.oneshot():
            result['process'] = {
                'cpu': process.cpu_times()._asdict(), 'io': process.io_counters()._asdict(),
                'threads': process.num_threads(), 'priority': int(process.nice()),
                'memory': process.memory_info()._asdict(), 'affinity': process.cpu_affinity(),
            }
        disk = psutil.disk_io_counters()
        result['system'] = {'cpu': psutil.cpu_times()._asdict(),
                            'disk_io': disk._asdict() if disk is not None else None,
                            'memory_available': psutil.virtual_memory().available,
                            'process_count': len(psutil.pids())}
        result['windows'] = _windows_state()
    except (OSError, psutil.Error, AttributeError, ValueError) as exc:
        result['observation_error'] = f'{type(exc).__name__}: {exc}'
    return result


def source_identity(root: Path) -> dict:
    """只读源码指纹，不扫描大数据文件，避免为了记录而预热整个合成库。"""
    sources, errors = {}, {}

    def fingerprint(path):
        name = path.relative_to(root).as_posix()
        try:
            sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            errors[name] = f'{type(exc).__name__}: {exc}'

    for folder in ('app', 'core', 'data', 'utils', 'ui', 'tests/perf'):
        try:
            for path in (root / folder).rglob('*.py'):
                if '.cache' not in path.parts:
                    fingerprint(path)
        except OSError as exc:
            errors[folder] = f'{type(exc).__name__}: {exc}'
    for name in ('main.py', 'pmctl.py', 'config.py', 'app_config.py',
                 'settings_schema.py', 'settings_store.py', 'tests/perf/budgets.json'):
        path = root / name
        if path.exists():
            fingerprint(path)
    clock = time.get_clock_info('perf_counter')
    return {'python': sys.version, 'executable': sys.executable,
            'platform': platform.platform(), 'machine': platform.processor(),
            'source_sha256': sources, 'source_read_errors': errors,
            'clock': {'implementation': clock.implementation, 'resolution': clock.resolution,
                      'monotonic': clock.monotonic, 'adjustable': clock.adjustable},
            'environment': {key: os.environ.get(key) for key in (
                'QT_QPA_PLATFORM', 'OPENBLAS_NUM_THREADS', 'PERF_MACHINE_FACTOR')},
            'trace_active': sys.gettrace() is not None, 'profile_active': sys.getprofile() is not None}
