"""打包体积以精确字节数验收，两个阈值分别覆盖边界。"""
import pytest

from build.dist_stats import MB, check_budgets


@pytest.mark.parametrize('directory_size,installer_size,expected', [
    (350 * MB, 100 * MB, []),
    (350 * MB + 1, 100 * MB, ['程序目录']),
    (350 * MB, 100 * MB + 1, ['安装包']),
])
def test_budget_uses_exact_bytes(tmp_path, monkeypatch, directory_size, installer_size, expected):
    root = tmp_path / 'app'
    root.mkdir()
    installer = tmp_path / 'setup.exe'
    installer.write_bytes(b'placeholder')
    monkeypatch.setattr('build.dist_stats.collect_stats', lambda _: (directory_size, 1))
    monkeypatch.setattr('build.dist_stats.os.path.getsize', lambda _: installer_size)
    result = check_budgets(str(root), str(installer))
    assert [s.split(' ')[0] for s in result] == expected
