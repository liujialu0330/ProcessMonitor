"""
进程信息采集器
负责从系统中采集进程的各项性能指标
"""
import ctypes
import logging
import sys
from ctypes import wintypes
import psutil
from typing import Any, Optional, Dict, List, Tuple
from core.contracts import UnavailableReason
from utils.metrics import MetricType

logger = logging.getLogger(__name__)

# ---------- GDI/USER 对象数（user32.GetGuiResources）----------
GR_GDIOBJECTS = 0
GR_USEROBJECTS = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87

# 模块级惰性初始化的 Win32 函数，仅 Windows 平台使用
_win32_funcs = None


class MetricNotSupported(Exception):
    """该指标对目标进程不适用（不是权限问题，以管理员运行也无法采集）。

    reason 区分具体原因，界面文案由 ui/formatting 按枚举映射。
    """

    def __init__(self, reason: UnavailableReason = UnavailableReason.NOT_SUPPORTED):
        super().__init__(reason.value)
        self.reason = reason


def _init_win32():
    """惰性加载并声明 Win32 函数原型（显式 argtypes/restype，避免64位句柄截断）。"""
    global _win32_funcs
    if _win32_funcs is not None:
        return _win32_funcs
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.GetGuiResources.restype = wintypes.DWORD

    _win32_funcs = (kernel32, user32)
    return _win32_funcs


class ProcessCollector:
    """进程信息采集器"""

    def __init__(self, pid: int):
        """
        初始化采集器

        Args:
            pid: 进程ID
        """
        self.pid = pid
        self._process: Optional[psutil.Process] = None
        self._last_io_counters = None  # 用于计算IO增量
        # 最近一次 collect_metrics 中无法采集的指标：{指标类型: 原因}
        self.last_unavailable: Dict[str, UnavailableReason] = {}
        # 已记录过异常日志的指标，避免持续失败时每个周期都刷日志
        self._logged_failures = set()

    def is_process_running(self) -> bool:
        """
        检查进程是否正在运行

        Returns:
            bool: 进程是否存在且运行中
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            return self._process.is_running()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def get_create_time(self) -> Optional[float]:
        """
        读取采集器所绑定进程的创建时间（与 psutil.Process.create_time() 同口径）

        Returns:
            Optional[float]: 创建时间（epoch 秒）；进程不存在或无权限读取时返回None
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            return self._process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def get_process_name(self) -> Optional[str]:
        """
        获取进程名称

        Returns:
            Optional[str]: 进程名称，失败返回None
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            return self._process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def _get_gui_resource(self, flag: int) -> float:
        """
        读取进程的 GDI 或 USER 对象数（user32.GetGuiResources）

        Args:
            flag: GR_GDIOBJECTS(0) 或 GR_USEROBJECTS(1)

        Returns:
            float: 对象数

        Raises:
            psutil.NoSuchProcess: 进程已不存在
            psutil.AccessDenied: 无权限打开进程，或调用失败
            MetricNotSupported: 非 Windows，或进程不在当前会话（如 session 0 的系统服务）
        """
        if sys.platform != "win32":
            raise MetricNotSupported(UnavailableReason.NOT_SUPPORTED)

        kernel32, user32 = _init_win32()
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)
        if not handle:
            if not psutil.pid_exists(self.pid):
                raise psutil.NoSuchProcess(self.pid)
            raise psutil.AccessDenied(self.pid)
        try:
            ctypes.set_last_error(0)
            count = user32.GetGuiResources(handle, flag)
            if count == 0:
                error = ctypes.get_last_error()
                if error == _ERROR_INVALID_PARAMETER:
                    # 其他会话的进程（如 session 0 的系统服务）返回 0 且 LastError=87；
                    # 这类进程不在当前桌面会话，提权也无法采集，不能提示"权限不足"
                    raise MetricNotSupported(UnavailableReason.NOT_IN_SESSION)
                if error != 0:
                    raise psutil.AccessDenied(self.pid)
            return float(count)
        finally:
            kernel32.CloseHandle(handle)

    def _collect_one(self, metric_type: str) -> Optional[float]:
        """
        采集单个性能指标（内部方法，不捕获psutil异常，由调用方统一处理）

        Args:
            metric_type: 指标类型（来自MetricType）

        Returns:
            Optional[float]: 指标值，未知指标返回None
        """
        if self._process is None:
            self._process = psutil.Process(self.pid)

        # 根据不同类型采集数据
        if metric_type == MetricType.MEMORY_RSS:
            # 工作集内存（RSS），转换为KB
            return self._process.memory_info().rss / 1024

        elif metric_type == MetricType.MEMORY_PERCENT:
            # 内存使用百分比
            return self._process.memory_percent()

        elif metric_type == MetricType.CPU_PERCENT:
            # CPU使用率（interval=None非阻塞，基于上次调用以来的增量计算）
            # 首次调用返回0，需先调用prime_cpu()预热
            return self._process.cpu_percent(interval=None)

        elif metric_type == MetricType.NUM_THREADS:
            # 线程数
            return float(self._process.num_threads())

        elif metric_type == MetricType.NUM_HANDLES:
            # 句柄数（仅Windows）
            try:
                return float(self._process.num_handles())
            except AttributeError:
                # 非Windows系统返回文件描述符数量
                return float(self._process.num_fds())

        elif metric_type == MetricType.IO_READ_BYTES:
            # IO读取字节数，转换为KB
            io_counters = self._process.io_counters()
            return io_counters.read_bytes / 1024

        elif metric_type == MetricType.IO_WRITE_BYTES:
            # IO写入字节数，转换为KB
            io_counters = self._process.io_counters()
            return io_counters.write_bytes / 1024

        elif metric_type == MetricType.IO_READ_COUNT:
            # IO读取次数
            io_counters = self._process.io_counters()
            return float(io_counters.read_count)

        elif metric_type == MetricType.IO_WRITE_COUNT:
            # IO写入次数
            io_counters = self._process.io_counters()
            return float(io_counters.write_count)

        elif metric_type == MetricType.IO_OTHER_COUNT:
            # IO其他次数
            io_counters = self._process.io_counters()
            return float(io_counters.other_count)

        elif metric_type == MetricType.IO_OTHER_BYTES:
            # IO其他字节数，转换为KB
            io_counters = self._process.io_counters()
            return io_counters.other_bytes / 1024

        # ========== 扩展内存指标 ==========
        elif metric_type == MetricType.MEMORY_PEAK_WSET:
            # 工作集峰值，转换为KB
            return self._process.memory_info().peak_wset / 1024

        elif metric_type == MetricType.MEMORY_PRIVATE:
            # 私有字节（Private Bytes，= PrivateUsage = 任务管理器"提交大小"），
            # 转换为KB。psutil 的 vms/pagefile/private 数值相同，只采这一个
            return self._process.memory_info().private / 1024

        elif metric_type == MetricType.MEMORY_PEAK_PAGEFILE:
            # 私有字节峰值（PeakPagefileUsage），转换为KB
            return self._process.memory_info().peak_pagefile / 1024

        elif metric_type == MetricType.MEMORY_PAGED_POOL:
            # 分页池，转换为KB
            return self._process.memory_info().paged_pool / 1024

        elif metric_type == MetricType.MEMORY_PEAK_PAGED_POOL:
            # 分页池峰值，转换为KB
            return self._process.memory_info().peak_paged_pool / 1024

        elif metric_type == MetricType.MEMORY_NONPAGED_POOL:
            # 非分页池，转换为KB
            return self._process.memory_info().nonpaged_pool / 1024

        elif metric_type == MetricType.MEMORY_PEAK_NONPAGED_POOL:
            # 非分页池峰值，转换为KB
            return self._process.memory_info().peak_nonpaged_pool / 1024

        elif metric_type == MetricType.MEMORY_NUM_PAGE_FAULTS:
            # 页面错误次数
            return float(self._process.memory_info().num_page_faults)

        elif metric_type == MetricType.MEMORY_USS:
            # 唯一集大小，转换为KB
            return self._process.memory_full_info().uss / 1024

        # ========== GDI/USER 对象数 ==========
        elif metric_type == MetricType.NUM_GDI_OBJECTS:
            return self._get_gui_resource(GR_GDIOBJECTS)

        elif metric_type == MetricType.NUM_USER_OBJECTS:
            return self._get_gui_resource(GR_USEROBJECTS)

        # ========== CPU扩展指标 ==========
        elif metric_type == MetricType.CPU_USER_TIME:
            # CPU用户时间（秒）
            return self._process.cpu_times().user

        elif metric_type == MetricType.CPU_SYSTEM_TIME:
            # CPU系统时间（秒）
            return self._process.cpu_times().system

        elif metric_type == MetricType.CPU_PRIORITY:
            # 进程优先级
            return float(self._process.nice())

        # ========== 上下文切换 ==========
        elif metric_type == MetricType.NUM_CTX_SWITCHES_VOL:
            # 自愿上下文切换
            return float(self._process.num_ctx_switches().voluntary)

        elif metric_type == MetricType.NUM_CTX_SWITCHES_INVOL:
            # 非自愿上下文切换
            return float(self._process.num_ctx_switches().involuntary)

        else:
            # 未知指标类型
            return None

    def prime_cpu(self):
        """
        预热CPU采集
        cpu_percent(interval=None)首次调用固定返回0，
        监控含CPU指标的任务前先调用一次，丢弃首个无效值
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            self._process.cpu_percent(interval=None)
        except psutil.Error:
            # 预热失败不影响主流程，采集时会再次处理异常
            pass

    def collect_metrics(self, metric_types: List[str]) -> Optional[Dict[str, float]]:
        """
        批量采集多个性能指标（同一周期共用oneshot缓存）

        Args:
            metric_types: 指标类型列表（来自MetricType）

        Returns:
            Optional[Dict[str, float]]: {指标类型: 指标值}字典；
                进程不存在（含 PID 已被另一个进程复用）时返回None（调用方据此停止任务）。
                采集失败的指标不放入字典（不会记为0），原因记录在
                self.last_unavailable（每次调用开头清空）。
        """
        self.last_unavailable = {}
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)

            # PID 复用检测：is_running() 会比对进程创建时间，PID 被新进程占用时为 False，
            # 否则会把另一个进程的数据记到本任务名下
            try:
                if not self._process.is_running():
                    return None
            except psutil.AccessDenied:
                pass  # 无法确认时按"仍在运行"处理，不误停任务

            values: Dict[str, float] = {}
            with self._process.oneshot():
                for metric_type in metric_types:
                    try:
                        value = self._collect_one(metric_type)
                    except psutil.AccessDenied:
                        # 权限不足：跳过该指标（不记0），进程可能仍存在，不中断
                        self.last_unavailable[metric_type] = UnavailableReason.ACCESS_DENIED
                        continue
                    except psutil.ZombieProcess:
                        # 注意：ZombieProcess是NoSuchProcess的子类，必须先于父类捕获
                        self.last_unavailable[metric_type] = UnavailableReason.PROCESS_EXITING
                        continue
                    except psutil.NoSuchProcess:
                        # 进程确实不存在，整体返回None停止任务
                        return None
                    except MetricNotSupported as e:
                        # 指标对该进程不适用（非权限问题），跳过且不提示提权
                        self.last_unavailable[metric_type] = e.reason
                        continue
                    except Exception:
                        # 其他异常，进程可能仍存在，跳过该指标继续
                        if metric_type not in self._logged_failures:
                            self._logged_failures.add(metric_type)
                            logger.exception(
                                "采集指标 %s 失败（PID %s）", metric_type, self.pid)
                        self.last_unavailable[metric_type] = UnavailableReason.ERROR
                        continue

                    if value is not None:
                        values[metric_type] = value
                    # 未知指标（value为None）跳过，不写入结果

            return values

        except psutil.NoSuchProcess:
            # 创建Process对象时进程已不存在
            return None

    def collect_metric(self, metric_type: str) -> Optional[float]:
        """
        采集指定的性能指标（单指标兼容接口，语义与旧版一致）

        Args:
            metric_type: 指标类型（来自MetricType）

        Returns:
            Optional[float]: 指标值，失败返回None
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)

            if metric_type == MetricType.CPU_PERCENT:
                # 单指标采集保持旧语义：使用0.1秒阻塞间隔，更准确
                return self._process.cpu_percent(interval=0.1)

            return self._collect_one(metric_type)

        except psutil.NoSuchProcess:
            # 进程确实不存在
            return None
        except (psutil.AccessDenied, psutil.ZombieProcess):
            # 访问被拒绝或僵尸进程，进程可能仍存在但暂时无法访问
            # 返回0而不是None，避免误判为进程终止
            return 0.0
        except Exception:
            # 其他异常，进程可能仍存在
            return 0.0

    def get_process_info(self) -> Optional[Dict[str, Any]]:
        """
        获取进程的基本信息

        Returns:
            Optional[Dict]: 进程信息字典，包含name, pid, status等
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)

            return {
                'pid': self.pid,
                'name': self._process.name(),
                'status': self._process.status(),
                'create_time': self._process.create_time(),
                'username': self._process.username() if hasattr(self._process, 'username') else None,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    @staticmethod
    def get_all_processes() -> List[Tuple[int, str, Optional[float]]]:
        """
        获取系统中所有正在运行的进程列表

        带上创建时间：列表可能已过期，从选中到真正开始采集之间 PID 可能被别的进程
        复用，监控任务据此确认采到的仍是所选的那个进程。

        Returns:
            List[Tuple[int, str, Optional[float]]]: (pid, name, create_time)元组的列表；
                create_time 读取失败时为None（调用方据此跳过身份比对）
        """
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                pid = proc.info['pid']
                name = proc.info['name']
                # 过滤掉进程名为空或None的进程
                if name and name.strip():
                    processes.append((pid, name, proc.info.get('create_time')))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # 按进程名排序
        processes.sort(key=lambda x: x[1].lower())
        return processes

    @staticmethod
    def find_process_by_name(name: str) -> List[Tuple[int, str]]:
        """
        根据进程名查找进程

        Args:
            name: 进程名（支持部分匹配）

        Returns:
            List[Tuple[int, str]]: 匹配的(pid, name)元组列表
        """
        processes = []
        name_lower = name.lower()

        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if name_lower in proc.info['name'].lower():
                    processes.append((proc.info['pid'], proc.info['name']))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        processes.sort(key=lambda x: x[1].lower())
        return processes


# 单元测试代码（可选）
if __name__ == "__main__":
    import os

    # 测试当前进程
    current_pid = os.getpid()
    print(f"当前进程PID: {current_pid}")

    collector = ProcessCollector(current_pid)

    # 测试进程名
    print(f"进程名: {collector.get_process_name()}")

    # 测试单指标接口（旧语义）
    print(f"工作集内存: {collector.collect_metric(MetricType.MEMORY_RSS):.2f} KB")
    print(f"CPU使用率: {collector.collect_metric(MetricType.CPU_PERCENT):.1f} %")
    print(f"线程数: {collector.collect_metric(MetricType.NUM_THREADS):.0f}")

    # 测试多指标批量采集接口
    collector.prime_cpu()
    values = collector.collect_metrics([
        MetricType.MEMORY_RSS,
        MetricType.CPU_PERCENT,
        MetricType.NUM_THREADS,
    ])
    print("\n批量采集结果:")
    for metric_type, value in values.items():
        print(f"  {metric_type}: {value:.2f}")

    # 测试不存在的进程（应返回None）
    dead_collector = ProcessCollector(999999)
    print(f"\n不存在进程的批量采集结果: {dead_collector.collect_metrics([MetricType.MEMORY_RSS])}")

    # 测试获取所有进程
    all_processes = ProcessCollector.get_all_processes()
    print(f"\n系统中共有 {len(all_processes)} 个进程")
    print("前5个进程:")
    for pid, name, _create_time in all_processes[:5]:
        print(f"  PID: {pid}, Name: {name}")

    # 测试查找进程
    print("\n查找名称包含 'python' 的进程:")
    python_processes = ProcessCollector.find_process_by_name("python")
    for pid, name in python_processes:
        print(f"  PID: {pid}, Name: {name}")
