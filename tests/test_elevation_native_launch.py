"""Win32 ShellExecuteEx 参数与 UAC 取消码的替身核验。"""
from types import SimpleNamespace

import pytest

import utils.elevation as elevation


class Function:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


@pytest.mark.parametrize('cancelled', [False, True])
def test_shell_launch_returns_handle_or_explicit_cancel(monkeypatch, cancelled):
    seen = {}

    def execute(pointer):
        info = pointer._obj
        seen.update(verb=info.lpVerb, command=info.lpFile, parameters=info.lpParameters,
                    flags=info.fMask, owner=info.hwnd, cwd=info.lpDirectory)
        info.hProcess = 123
        return not cancelled

    shell = SimpleNamespace(ShellExecuteExW=Function(execute))
    ole = SimpleNamespace(CoInitializeEx=Function(lambda *_a: 0),
                          CoUninitialize=Function(lambda: seen.update(uninitialized=True)))
    monkeypatch.setattr(elevation.ctypes, 'WinDLL',
                        lambda name, **_kw: shell if name == 'shell32' else ole, raising=False)
    monkeypatch.setattr(elevation.ctypes, 'get_last_error', lambda: 1223, raising=False)
    monkeypatch.setattr(elevation, 'WindowsProcess', lambda handle: ('process', handle))
    command = ['C:/Program Files/Python/pythonw.exe', 'D:/中文 路径/main.py',
               '--data-dir', 'D:/数据 path']
    if cancelled:
        with pytest.raises(elevation.ElevationCancelled):
            elevation.launch_elevated(command, cwd='D:/中文 路径', owner=456)
    else:
        assert elevation.launch_elevated(command, cwd='D:/中文 路径', owner=456) == ('process', 123)
    assert seen['verb'] == 'runas'
    assert seen['owner'] == 456 and seen['cwd'] == 'D:/中文 路径'
    assert '"D:/中文 路径/main.py"' in seen['parameters']
    assert '"D:/数据 path"' in seen['parameters']
    assert seen['flags'] & 0x40 and seen['flags'] & 0x100
    assert seen['uninitialized']
