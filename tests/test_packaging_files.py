"""
打包与发布链的静态契约：安装脚本、构建脚本、安装包冒烟脚本与 release 工作流

这些文件改坏时没有任何运行时错误，要到发版那天才暴露，所以把关键约定写成断言：
覆盖安装前清掉 _internal、安装包文件名固定为 ASCII 的 Windows_v{版本}_Setup.exe、构建脚本用
自检取代“启动后 taskkill”、冒烟脚本能被 PowerShell 解析、release 工作流只构建不发布。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD = PROJECT_ROOT / "build"


def _text(path: Path, encoding="utf-8") -> str:
    return path.read_text(encoding=encoding)


def _setup_iss() -> str:
    return _text(BUILD / "setup.iss")


# ---------------------------------------------------------------- setup.iss

def test_install_delete_clears_internal_before_files_are_copied():
    text = _setup_iss()
    section = text[text.index("[InstallDelete]"):text.index("\n[Files]")]
    entries = [line for line in section.splitlines() if line.strip().startswith("Type:")]
    assert entries == ['Type: filesandordirs; Name: "{app}\\_internal"']


def test_installer_file_name_is_the_ascii_release_asset_name():
    lines = [line.strip() for line in _setup_iss().splitlines()
             if line.strip().startswith("OutputBaseFilename=")]
    assert lines == ["OutputBaseFilename=Windows_v{#MyAppVersion}_Setup"]
    assert lines[0].isascii()


def test_setup_iss_has_no_chinese_named_installer_left():
    assert "进程监控助手_v" not in _setup_iss()


# ---------------------------------------------------------------- build.bat（GBK）

def _build_bat() -> str:
    return (BUILD / "build.bat").read_bytes().decode("gbk")


def test_build_bat_runs_self_test_instead_of_taskkill_smoke():
    text = _build_bat()
    assert "run_self_test.py" in text
    assert "taskkill" not in text.lower()
    assert "ping.exe -n 6" not in text


def test_build_bat_clears_dist_before_packaging():
    text = _build_bat()
    clear = text.index('rmdir /s /q "dist"')
    assert clear < text.index("PyInstaller --clean")


def test_build_bat_uses_the_ascii_installer_name_only():
    text = _build_bat()
    assert "dist\\installer\\Windows_v%VER%_Setup.exe" in text
    assert "_v%VER%_Setup.exe" in text and "进程监控助手_v%VER%" not in text
    assert "copy /y" not in text


def test_build_bat_has_no_stray_control_characters():
    text = _build_bat()
    stray = {hex(ord(c)) for c in text if ord(c) < 32 and c not in "\r\n\t"}
    assert not stray


# ---------------------------------------------------------------- installer_smoke.ps1

def _smoke_bytes() -> bytes:
    return (BUILD / "installer_smoke.ps1").read_bytes()


def test_smoke_script_has_utf8_bom_for_windows_powershell():
    assert _smoke_bytes().startswith(b"\xef\xbb\xbf")


def test_smoke_script_has_no_typographic_quotes():
    """PowerShell 把 “ ” ‘ ’ 当成引号：混进字符串会让整个脚本解析失败"""
    text = _smoke_bytes().decode("utf-8-sig")
    assert not re.search("[“”‘’]", text)


def test_smoke_script_keeps_to_temp_directories_and_graceful_exit():
    text = _smoke_bytes().decode("utf-8-sig")
    assert '/DIR=`"$appDir`"' in text            # 安装到临时目录
    assert "--data-dir" in text                  # 数据目录一律显式指定
    assert "taskkill.exe /PID" in text and "/F" not in re.findall(r"taskkill\.exe[^\r\n]*", text)[0]
    assert "OldInstaller" in text and "sha256" in text.lower()
    assert "Test-SetupRegistered" in text        # 本机已安装时拒绝，不动它


@pytest.mark.skipif(shutil.which("powershell") is None, reason="需要 Windows PowerShell")
def test_smoke_script_parses_in_windows_powershell():
    script = str(BUILD / "installer_smoke.ps1")
    command = ("$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{script}',[ref]$null,[ref]$e);if($e){{$e|%{{$_.Message}};exit 1}}")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------- release 工作流

def _release_yml() -> str:
    return _text(PROJECT_ROOT / ".github" / "workflows" / "release.yml")


def test_release_workflow_triggers_only_on_tags_and_manual_dispatch():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_release_yml())
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"]}


def test_release_workflow_builds_and_smokes_but_never_publishes():
    text = _release_yml()
    assert "build\\build.bat" in text and "installer_smoke.ps1" in text
    for forbidden in ("gh release", "action-gh-release", "create-release", "upload-release-asset",
                      "git push", "git tag"):
        assert forbidden not in text, forbidden
    assert re.search(r"permissions:\s*\n\s*contents:\s*write", text) is None


def test_asset_name_is_consistent_between_installer_script_and_verify_release():
    verify = _text(BUILD / "verify_release.py")
    assert "Windows_v{version}_Setup.exe" in verify
    assert "Windows_v{#MyAppVersion}_Setup" in _setup_iss()
