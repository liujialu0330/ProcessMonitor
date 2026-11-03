"""测试辅助：轮询等待，替代固定 time.sleep 的时序断言"""
import time


def poll_until(condition, timeout: float = 10.0, interval: float = 0.01) -> bool:
    """轮询等待 condition() 为真，最长 timeout 秒；超时返回最后一次判定结果"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


def wait_stable(getter, quiet: float = 0.4, timeout: float = 10.0) -> bool:
    """等待 getter() 的值连续 quiet 秒不变（用于确认"已不再产生新数据"）"""
    deadline = time.monotonic() + timeout
    last = getter()
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.02)
        current = getter()
        if current != last:
            last = current
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= quiet:
            return True
    return False
