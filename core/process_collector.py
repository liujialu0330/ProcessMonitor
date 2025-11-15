"""
进程信息采集器
负责从系统中采集进程的各项性能指标
"""
import psutil
from typing import Optional, Dict, List, Tuple
from utils.metrics import MetricType


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

    def collect_metric(self, metric_type: str) -> Optional[float]:
        """
        采集指定的性能指标

        Args:
            metric_type: 指标类型（来自MetricType）

        Returns:
            Optional[float]: 指标值，失败返回None
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)

            # 根据不同类型采集数据
            if metric_type == MetricType.MEMORY_RSS:
                # 工作集内存（RSS），转换为MB
                return self._process.memory_info().rss / (1024 * 1024)

            elif metric_type == MetricType.MEMORY_VMS:
                # 虚拟内存大小，转换为MB
                return self._process.memory_info().vms / (1024 * 1024)

            elif metric_type == MetricType.MEMORY_PERCENT:
                # 内存使用百分比
                return self._process.memory_percent()

            elif metric_type == MetricType.CPU_PERCENT:
                # CPU使用率（使用0.1秒间隔，更准确）
                # interval=0在第一次调用时可能返回0，使用0.1更稳定
                return self._process.cpu_percent(interval=0.1)

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
                # IO读取字节数，转换为MB
                io_counters = self._process.io_counters()
                return io_counters.read_bytes / (1024 * 1024)

            elif metric_type == MetricType.IO_WRITE_BYTES:
                # IO写入字节数，转换为MB
                io_counters = self._process.io_counters()
                return io_counters.write_bytes / (1024 * 1024)

            elif metric_type == MetricType.IO_READ_COUNT:
                # IO读取次数
                io_counters = self._process.io_counters()
                return float(io_counters.read_count)

            elif metric_type == MetricType.IO_WRITE_COUNT:
                # IO写入次数
                io_counters = self._process.io_counters()
                return float(io_counters.write_count)

            else:
                return None

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

    def get_process_info(self) -> Optional[Dict[str, any]]:
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
    def get_all_processes() -> List[Tuple[int, str]]:
        """
        获取系统中所有正在运行的进程列表

        Returns:
            List[Tuple[int, str]]: (pid, name)元组的列表
        """
        processes = []
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                processes.append((proc.info['pid'], proc.info['name']))
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

    # 测试各项指标
    print(f"工作集内存: {collector.collect_metric(MetricType.MEMORY_RSS):.2f} MB")
    print(f"CPU使用率: {collector.collect_metric(MetricType.CPU_PERCENT):.1f} %")
    print(f"线程数: {collector.collect_metric(MetricType.NUM_THREADS):.0f}")

    # 测试获取所有进程
    all_processes = ProcessCollector.get_all_processes()
    print(f"\n系统中共有 {len(all_processes)} 个进程")
    print("前5个进程:")
    for pid, name in all_processes[:5]:
        print(f"  PID: {pid}, Name: {name}")

    # 测试查找进程
    print("\n查找名称包含 'python' 的进程:")
    python_processes = ProcessCollector.find_process_by_name("python")
    for pid, name in python_processes:
        print(f"  PID: {pid}, Name: {name}")
