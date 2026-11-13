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

INSTALLER_LIMIT_MB = 100
DIRECTORY_LIMIT_MB = 350
MB = 1_000_000


def check_budgets(directory: str, installer: str) -> list:
    """按完整字节数比较，不因整数显示向下取整放过略超限的产物。MB 按十进制。"""
    if not os.path.isdir(directory) or not os.path.isfile(installer):
        raise FileNotFoundError('程序目录或安装包不存在')
    size, _ = collect_stats(directory)
    installer_size = os.path.getsize(installer)
    problems = []
    for label, actual, limit in (('安装包', installer_size, INSTALLER_LIMIT_MB),
                                  ('程序目录', size, DIRECTORY_LIMIT_MB)):
        if actual > limit * MB:
            problems.append(f'{label} {actual / MB:.2f} MB，超过 {limit} MB')
    return problems


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
    if len(sys.argv) == 4 and sys.argv[1] == '--check':
        try:
            problems = check_budgets(sys.argv[2], sys.argv[3])
        except OSError as exc:
            print(str(exc))
            sys.exit(1)
        print('\n'.join(problems) if problems else '体积检查通过：安装包 ≤ 100 MB、程序目录 ≤ 350 MB')
        sys.exit(1 if problems else 0)
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
    size_mb = size_bytes // MB
    print(size_mb)
    print(count)
