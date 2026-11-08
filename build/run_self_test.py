r"""
打包自检外壳：带超时地运行 ``<exe> --self-test --data-dir <临时空目录>``

用法：
    python build\run_self_test.py dist\进程监控助手\进程监控助手.exe [--timeout 240] [--keep]

build.bat 与 installer_smoke.ps1 都经它运行自检：窗口程序出错时可能卡在模态错误框里
（如引导程序加载 Python DLL 失败），直接等进程结束会让构建无限挂起，这里超时后结束
该进程树并按失败处理。退出码：0 自检通过；子进程的非 0 退出码原样返回；124 超时。
自检通过时删除临时数据目录；失败时保留（--keep 则始终保留），其中的
self_test_report.txt 与 logs\app.log 说明失败的步骤。只用标准库。
"""
import argparse
import locale
import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

EXIT_TIMEOUT = 124
DEFAULT_TIMEOUT_S = 240.0
REPORT_FILENAME = "self_test_report.txt"


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), "replace")


def _kill_tree(pid: int) -> None:
    """结束进程及其子进程（pmctl 等）；只按 PID 结束，绝不按映像名"""
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)


def run_self_test(exe: str, timeout_s: float = DEFAULT_TIMEOUT_S, keep: bool = False,
                  data_dir: Optional[str] = None, out=None) -> int:
    out = out or sys.stdout
    if not os.path.isfile(exe):
        print(f"找不到程序：{exe}", file=out)
        return 2
    created = data_dir is None
    data_dir = data_dir or tempfile.mkdtemp(prefix="pm_selftest_")
    os.makedirs(data_dir, exist_ok=True)
    process = subprocess.Popen([exe, "--self-test", "--data-dir", data_dir],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL)
    try:
        raw, _ = process.communicate(timeout=timeout_s)
        code = process.returncode
        text = _decode(raw or b"")
    except subprocess.TimeoutExpired:
        _kill_tree(process.pid)
        raw, _ = process.communicate()
        code = EXIT_TIMEOUT
        text = _decode(raw or b"") + f"\n[失败] 自检超过 {timeout_s:g} 秒没有结束，已结束进程\n"

    report = os.path.join(data_dir, REPORT_FILENAME)
    if not text.strip() and os.path.isfile(report):
        # 拿不到标准输出（没有可写的句柄）时，以报告文件为准
        with open(report, encoding="utf-8") as f:
            text = f.read()
    print(text.replace("\r\n", "\n").rstrip("\n"), file=out)
    if code == 0 and not keep:
        shutil.rmtree(data_dir, ignore_errors=True)
    elif code != 0:
        print(f"自检失败（退出码 {code}），数据目录已保留：{data_dir}", file=out)
    return code


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="带超时地运行打包自检")
    parser.add_argument("exe", help="主程序 exe 的路径")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="超时秒数")
    parser.add_argument("--keep", action="store_true", help="通过后也保留数据目录")
    parser.add_argument("--data-dir", help="自检数据目录（必须是空目录；缺省自动建临时目录）")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    return run_self_test(args.exe, args.timeout, args.keep, args.data_dir)


if __name__ == "__main__":
    sys.exit(main())
