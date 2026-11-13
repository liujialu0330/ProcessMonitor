"""
进程组（tree / name）测试替身：假的 ProcessTreeSource 与实例读数的构造函数

假来源的内容由"模拟时刻"决定：frames(t) 给出模拟时刻 t（秒，自假时钟起点算起）时组里有
哪些实例，因此启动时的存在性检查、CPU 预热与各周期的观察读到的是同一时间线上的状态，
不必数"第几次调用"。
"""
from typing import Callable, Optional, Sequence, Union

from core.process_tree import GroupSpec, InstanceReading, Observation

ROOT_PID = 5000
ROOT_CREATE_TIME = 1000.0


def inst(name: str, pid: int, create_time: Optional[float] = None, *, ppid: int = 0,
         cpu: Optional[float] = None, unavailable: Optional[dict] = None,
         **values: float) -> InstanceReading:
    """一个实例的读数：values 是各指标原值（存储单位）；cpu 为 CPU 时间秒；
    创建时间缺省按 PID 派生（互不相同、且不早于根进程）。"""
    return InstanceReading(
        pid=pid, create_time=(create_time if create_time is not None
                              else ROOT_CREATE_TIME + pid / 100.0),
        name=name, ppid=ppid, values=dict(values), unavailable=dict(unavailable or {}),
        cpu_seconds=cpu)


class Frame:
    """一个时刻的组状态：实例列表与根进程是否仍在"""

    def __init__(self, instances: Sequence[InstanceReading], root_alive: bool = True):
        self.instances = tuple(instances)
        self.root_alive = root_alive


FramesFn = Callable[[float], Union[Frame, Sequence[InstanceReading]]]


class FakeTreeSource:
    """
    假 ProcessTreeSource。clock 提供 monotonic()（模拟时刻与 CPU 差分的时间基准）；
    frames(t) 返回 Frame 或实例列表（根进程视为存活）。

    on_observe(t) 在返回观察结果前调用，供用例在采集线程内请求停止之类的脚本使用；
    calls 记录每次 observe 的 (模拟时刻, 请求的指标, root_create_time)。
    """

    def __init__(self, frames: FramesFn, clock, *, origin: Optional[float] = None,
                 root_create_time: float = ROOT_CREATE_TIME,
                 on_observe: Optional[Callable[[float], None]] = None):
        self.frames = frames
        self.clock = clock
        self.origin = clock.monotonic() if origin is None else origin
        self.root_create_time = root_create_time
        self.on_observe = on_observe
        self.calls = []

    def now(self) -> float:
        return self.clock.monotonic() - self.origin

    def observe(self, spec: GroupSpec, metrics: Sequence[str], *,
                root_create_time: Optional[float] = None,
                not_before: Optional[float] = None) -> Observation:
        t = self.now()
        self.calls.append((t, tuple(metrics), root_create_time))
        frame = self.frames(t)
        if not isinstance(frame, Frame):
            frame = Frame(frame)
        if self.on_observe is not None:
            self.on_observe(t)
        return Observation(
            taken_monotonic=self.clock.monotonic(), instances=frame.instances,
            root_alive=frame.root_alive,
            root_create_time=self.root_create_time if frame.root_alive else None,
            source='fake')
