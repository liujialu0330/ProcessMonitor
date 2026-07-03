"""
统计 PyInstaller onedir 打包产物目录的总大小与文件数

用途：build.bat 打包完成后调用，输出两行纯数字（MB取整、文件数），
供批处理用 for /f 逐行捕获，避免在 .bat 里内联复杂的 PowerShell/嵌套引号
命令（曾因编码与括号嵌套问题导致批处理解析失败）。

用法：
    python build\\dist_stats.py <目录路径>
输出（stdout，两行）：
    <总大小MB整数>
    <文件数>
"""
import os
import sys


def collect_stats(dir_path: str) -> tuple:
    """
    遍历目录统计总字节数与文件数

    Args:
        dir_path: 目标目录路径

    Returns:
        tuple: (总字节数, 文件数)
    """
    total_size = 0
    file_count = 0
    for root, _dirs, files in os.walk(dir_path):
        for name in files:
            full_path = os.path.join(root, name)
            try:
                total_size += os.path.getsize(full_path)
            except OSError:
                continue
            file_count += 1
    return total_size, file_count


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(0)
        print(0)
        sys.exit(1)

    target_dir = sys.argv[1]
    if not os.path.isdir(target_dir):
        print(0)
        print(0)
        sys.exit(1)

    size_bytes, count = collect_stats(target_dir)
    size_mb = size_bytes // (1024 * 1024)
    print(size_mb)
    print(count)
