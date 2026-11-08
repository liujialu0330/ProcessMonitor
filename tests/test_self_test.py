"""
打包自检（app/self_test.py）与它的外壳（build/run_self_test.py）

离线部分：参数解析、报告与步骤失败处理、总超时、各步骤的失败路径、golden 库与清单一致、
外壳的退出码与超时。真正走完整条链的用例（slow）在子进程里跑 main.py --self-test。
"""
import gzip
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import config
from app import self_test
from app.paths import AppPaths
from app.self_test import (
    EXIT_FAILED, EXIT_OK, EXIT_USAGE, SELF_TEST_ARG, SelfTestError, SelfTestReport, parse_args,
    run_self_test,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHELL_SCRIPT = PROJECT_ROOT / "build" / "run_self_test.py"
MANIFEST = PROJECT_ROOT / "tests" / "fixtures" / "legacy_db" / "manifest.json"


def _argv(data_dir):
    return ["main.py", SELF_TEST_ARG, "--data-dir", str(data_dir)]


class _Exit:
    """记录 exit_process 的调用（不真的结束测试进程）"""

    def __init__(self):
        self.codes = []

    def __call__(self, code):
        self.codes.append(code)


# ---------------------------------------------------------------- 参数

def test_parse_args_requires_explicit_data_dir():
    with pytest.raises(SelfTestError, match="必须显式指定数据目录"):
        parse_args(["main.py", SELF_TEST_ARG])


def test_parse_args_accepts_both_spellings(tmp_path):
    assert parse_args(["main.py", SELF_TEST_ARG, "--data-dir", str(tmp_path)]).data_dir == str(tmp_path)
    assert parse_args(["main.py", SELF_TEST_ARG, f"--data-dir={tmp_path}"]).data_dir == str(tmp_path)


def test_parse_args_rejects_missing_value():
    with pytest.raises(SelfTestError, match="缺少数据目录路径"):
        parse_args(["main.py", SELF_TEST_ARG, "--data-dir"])


def test_environment_variable_is_not_enough(monkeypatch, tmp_path):
    """自检不认环境变量与便携标志：只有命令行显式给出的目录才允许写入"""
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    with pytest.raises(SelfTestError):
        parse_args(["main.py", SELF_TEST_ARG])


def test_no_data_dir_exits_with_usage_code_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out, exit_process = io.StringIO(), _Exit()
    code = run_self_test(["main.py", SELF_TEST_ARG], exit_process=exit_process, stream=out)
    assert code == EXIT_USAGE and exit_process.codes == [EXIT_USAGE]
    assert "[参数错误]" in out.getvalue() and "--data-dir" in out.getvalue()
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- 报告与步骤执行

def test_report_records_pass_and_fail_and_stops_at_first_failure():
    out = io.StringIO()
    report = SelfTestReport(out)
    assert report.run_step("甲", lambda: "详情") is True
    assert report.run_step("乙", lambda: (_ for _ in ()).throw(SelfTestError("没有 X"))) is False
    assert report.failed_step == "乙" and report.failure_reason == "没有 X"
    assert report.steps_done == 1 and report.ok is False
    text = out.getvalue()
    assert "[通过] 1. 甲" in text and "详情" in text
    assert "[失败] 自检失败：第 2 步“乙”：没有 X" in text
    assert report.finish() == EXIT_FAILED


def test_report_unexpected_exception_names_the_type():
    report = SelfTestReport(io.StringIO())
    report.run_step("丙", lambda: 1 / 0)
    assert report.failure_reason.startswith("ZeroDivisionError")


def test_report_survives_narrow_console_encoding():
    class Narrow(io.TextIOBase):
        encoding = "ascii"

        def __init__(self):
            self.parts = []

        def write(self, text):
            text.encode("ascii")
            self.parts.append(text)
            return len(text)

    stream = Narrow()
    report = SelfTestReport(stream)
    report.emit("中文输出")
    assert "".join(stream.parts).strip() == "????"


def test_report_with_no_stream_still_collects_lines():
    report = SelfTestReport(None)
    report.run_step("甲", lambda: None)
    assert any("甲" in line for line in report.lines)
    assert report.finish() == EXIT_OK


def _steps(*items):
    return tuple(items)


def test_run_self_test_passes_and_writes_report_file(tmp_path):
    ran = []
    steps = _steps(("一", lambda state: ran.append(1) or "ok1"), ("二", lambda state: ran.append(2)))
    out, exit_process = io.StringIO(), _Exit()
    code = run_self_test(_argv(tmp_path), exit_process=exit_process, stream=out, steps=steps)
    assert code == EXIT_OK and exit_process.codes == [EXIT_OK] and ran == [1, 2]
    assert "自检通过（2 步" in out.getvalue()
    report_file = tmp_path / self_test.REPORT_FILENAME
    assert "自检通过" in report_file.read_text(encoding="utf-8")


def test_run_self_test_stops_at_failing_step_and_reports_it(tmp_path):
    ran = []

    def failing(state):
        raise SelfTestError("故意失败")

    steps = _steps(("一", lambda state: ran.append(1)), ("坏步骤", failing),
                   ("三", lambda state: ran.append(3)))
    out, exit_process = io.StringIO(), _Exit()
    code = run_self_test(_argv(tmp_path), exit_process=exit_process, stream=out, steps=steps)
    assert code == EXIT_FAILED and exit_process.codes == [EXIT_FAILED]
    assert ran == [1]                                   # 失败后的步骤不再执行
    assert "[失败] 自检失败：第 2 步“坏步骤”：故意失败" in out.getvalue()
    assert "自检通过" not in out.getvalue()
    assert "坏步骤" in (tmp_path / self_test.REPORT_FILENAME).read_text(encoding="utf-8")


def test_run_self_test_times_out_with_failure_code(tmp_path):
    steps = _steps(("慢步骤", lambda state: time.sleep(1.0)))
    out, exit_process = io.StringIO(), _Exit()
    run_self_test(_argv(tmp_path), exit_process=exit_process, stream=out, steps=steps,
                  timeout_s=0.2)
    assert exit_process.codes[0] == EXIT_FAILED          # 看门狗线程先于步骤结束触发
    assert "自检超时" in out.getvalue() and "慢步骤" in out.getvalue()


# ---------------------------------------------------------------- 各步骤的失败路径

def test_paths_step_refuses_existing_database(tmp_path):
    (tmp_path / "monitor.db").write_bytes(b"real data")
    state = self_test._State(argv=_argv(tmp_path), data_dir=str(tmp_path))
    with pytest.raises(SelfTestError, match="已有数据库"):
        self_test._step_paths(state)
    assert (tmp_path / "monitor.db").read_bytes() == b"real data"
    assert not (tmp_path / "logs").exists()              # 校验不过之前不建任何目录


def test_paths_step_refuses_non_cli_source():
    state = self_test._State(argv=["main.py", SELF_TEST_ARG], data_dir="")
    with pytest.raises(SelfTestError, match="必须显式指定数据目录"):
        self_test._step_paths(state)


def test_golden_step_fails_when_data_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(self_test, "golden_db_path", lambda: str(tmp_path / "gone.db.gz"))
    state = self_test._State(argv=[], data_dir=str(tmp_path), paths=AppPaths.under(str(tmp_path / "d")))
    state.paths.ensure_dirs()
    with pytest.raises(SelfTestError, match="包里没有 golden 旧库"):
        self_test._step_golden_and_migrate(state)


def test_golden_step_fails_when_fixture_is_not_v1(tmp_path, monkeypatch):
    wrong = tmp_path / "v5.db"
    conn = sqlite3.connect(wrong)
    conn.execute("PRAGMA user_version = 5")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    gz = tmp_path / "wrong.db.gz"
    gz.write_bytes(gzip.compress(wrong.read_bytes()))
    monkeypatch.setattr(self_test, "golden_db_path", lambda: str(gz))
    paths = AppPaths.under(str(tmp_path / "d"))
    paths.ensure_dirs()
    state = self_test._State(argv=[], data_dir=paths.data_dir, paths=paths)
    with pytest.raises(SelfTestError, match="应为 v1"):
        self_test._step_golden_and_migrate(state)


def test_pmctl_step_fails_when_executable_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(self_test, "pmctl_command", lambda: [str(tmp_path / "no_pmctl.exe")])
    state = self_test._State(argv=[], data_dir=str(tmp_path), paths=AppPaths.under(str(tmp_path)))
    with pytest.raises(SelfTestError, match="找不到 pmctl"):
        self_test._step_pmctl(state)


def test_process_list_step_sees_this_process(qapp):
    detail = self_test._step_process_list(self_test._State(argv=[], data_dir=""))
    assert "个进程" in detail


def test_golden_migration_and_context_steps_pass_in_process(qapp, app_paths):
    """迁移到 v5、三个 golden 任务的采样数核对（成功路径，不涉及窗口）"""
    app_paths.ensure_dirs()
    state = self_test._State(argv=[], data_dir=app_paths.data_dir, paths=app_paths)
    try:
        detail = self_test._step_golden_and_migrate(state)
        assert "v1 → v5" in detail
        assert os.path.isfile(app_paths.db_path + ".bak_v1")
        self_test._step_context(state)
    finally:
        if state.ctx is not None:
            state.ctx.db.close()


def test_context_step_detects_wrong_sample_count(qapp, app_paths, monkeypatch):
    app_paths.ensure_dirs()
    state = self_test._State(argv=[], data_dir=app_paths.data_dir, paths=app_paths)
    monkeypatch.setitem(self_test.GOLDEN_TASK_SAMPLES, "golden-a", 599)
    try:
        self_test._step_golden_and_migrate(state)
        with pytest.raises(SelfTestError, match="golden-a 的采样数是 600，应为 599"):
            self_test._step_context(state)
    finally:
        if state.ctx is not None:
            state.ctx.db.close()


def test_step_table_covers_the_documented_chain():
    names = [name for name, _ in self_test.STEPS]
    for expected in ("迁移", "主窗口", "进程列表", "监控自身", "导出", "pmctl", "真退出"):
        assert any(expected in name for name in names), expected
    assert len(set(names)) == len(names)


# ---------------------------------------------------------------- golden 夹具

def test_golden_file_exists_and_is_a_v1_database(tmp_path):
    source = Path(self_test.golden_db_path())
    assert source.is_file()
    target = tmp_path / "golden.db"
    target.write_bytes(gzip.decompress(source.read_bytes()))
    assert self_test._read_user_version(str(target)) == self_test.GOLDEN_SOURCE_VERSION


def test_golden_expectations_match_the_fixture_manifest():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sample = next(s for s in manifest["samples"] if s["file"] == self_test.GOLDEN_FILENAME)
    expected = {task_id: info["sample_count_after"] for task_id, info in sample["tasks"].items()}
    assert expected == self_test.GOLDEN_TASK_SAMPLES


def test_spec_packages_the_golden_file_next_to_the_expected_directory():
    spec = (PROJECT_ROOT / "build" / "ProcessMonitor.spec").read_text(encoding="utf-8")
    assert f"'../tests/fixtures/legacy_db/{self_test.GOLDEN_FILENAME}', '{self_test.BUNDLED_DIRNAME}'" in spec


# ---------------------------------------------------------------- 子进程：入口分流

def _run_main(args, tmp_path, timeout=120):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
               PROCESS_MONITOR_DATA_DIR=str(tmp_path / "must_stay_unused"))
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "main.py"), *args], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def test_main_without_data_dir_returns_usage_code_and_touches_no_data_dir(tmp_path):
    result = _run_main([SELF_TEST_ARG], tmp_path)
    assert result.returncode == EXIT_USAGE
    assert "--data-dir" in result.stdout
    assert not (tmp_path / "must_stay_unused").exists()


def test_main_refuses_a_directory_that_already_holds_a_database(tmp_path):
    target = tmp_path / "data"
    target.mkdir()
    (target / "monitor.db").write_bytes(b"precious")
    result = _run_main([SELF_TEST_ARG, "--data-dir", str(target)], tmp_path)
    assert result.returncode == EXIT_FAILED
    assert "已有数据库" in result.stdout
    assert (target / "monitor.db").read_bytes() == b"precious"
    assert sorted(p.name for p in target.iterdir()) == ["monitor.db", self_test.REPORT_FILENAME]


@pytest.mark.slow
def test_main_runs_the_whole_chain_offscreen(tmp_path):
    target = tmp_path / "data"
    target.mkdir()
    result = _run_main([SELF_TEST_ARG, "--data-dir", str(target)], tmp_path, timeout=170)
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert result.stdout.count("[通过]") == len(self_test.STEPS)
    assert "自检通过" in result.stdout
    exported = sorted(p.name for p in (target / "self_test_export").iterdir())
    assert len(exported) == 3
    assert (target / "monitor.db.bak_v1").is_file()


# ---------------------------------------------------------------- 外壳 build/run_self_test.py

def _shell():
    spec = importlib.util.spec_from_file_location("run_self_test_under_test", SHELL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_exe(tmp_path, body):
    path = tmp_path / "fake_app.cmd"
    path.write_text("@echo off\r\n" + body + "\r\n", encoding="ascii")
    return str(path)


def test_shell_returns_zero_and_removes_data_dir_on_success(tmp_path):
    exe = _fake_exe(tmp_path, "echo all good\r\nexit /b 0")
    out = io.StringIO()
    created = []
    shell = _shell()
    original = shell.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = original(*args, dir=str(tmp_path), **kwargs)
        created.append(path)
        return path

    shell.tempfile.mkdtemp = tracking_mkdtemp
    try:
        assert shell.run_self_test(exe, timeout_s=30, out=out) == 0
    finally:
        shell.tempfile.mkdtemp = original
    assert "all good" in out.getvalue()
    assert created and not os.path.exists(created[0])


def test_shell_passes_through_failure_code_and_keeps_data_dir(tmp_path):
    exe = _fake_exe(tmp_path, "echo step failed\r\nexit /b 7")
    data = tmp_path / "keep"
    out = io.StringIO()
    assert _shell().run_self_test(exe, timeout_s=30, data_dir=str(data), out=out) == 7
    assert "step failed" in out.getvalue() and "退出码 7" in out.getvalue()
    assert data.is_dir()


def test_shell_kills_a_hung_process_and_returns_timeout_code(tmp_path):
    exe = _fake_exe(tmp_path, "ping -n 30 127.0.0.1 >nul")
    out = io.StringIO()
    started = time.monotonic()
    code = _shell().run_self_test(exe, timeout_s=1.5, data_dir=str(tmp_path / "d"), out=out)
    assert code == 124
    assert time.monotonic() - started < 20
    assert "已结束进程" in out.getvalue()


def test_shell_reports_missing_executable(tmp_path):
    out = io.StringIO()
    assert _shell().run_self_test(str(tmp_path / "none.exe"), out=out) == 2
    assert "找不到程序" in out.getvalue()
