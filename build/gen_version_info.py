"""
生成 PyInstaller 用的 Windows 版本信息文件（version_info.txt）

version_info.txt 是 PyInstaller 打包产物（build\\version_info.txt），不入库；
本脚本从 config.APP_VERSION 单源动态生成 4 元组版本号，供 build.bat 在
PyInstaller 打包前调用，杜绝版本号写死导致与 config.py 脱节。

用法：
    python build\\gen_version_info.py
"""
import os
import sys

# 保证无论从哪个工作目录调用都能找到项目根目录下的 config.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config  # noqa: E402


def _to_version_tuple(version_str: str) -> tuple:
    """
    将 "1.2.0" 形式的版本号转为 PyInstaller version_file 所需的 4 元组

    Args:
        version_str: 形如 "1.2.0" 的版本号字符串，段数不足4段时右侧补0，
            非数字段按0处理（与 core/update_checker.py 的 compare_version
            兼容口径一致）

    Returns:
        tuple: 4个int组成的元组，如 (1, 2, 0, 0)
    """
    parts = version_str.split('.')
    nums = []
    for p in parts[:4]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)


VERSION_INFO_TEMPLATE = """# UTF-8
#
# 本文件由 build\\gen_version_info.py 自动生成，请勿手动编辑。
# 数据来源：config.APP_VERSION（单源）
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'080404b0',
        [StringStruct(u'CompanyName', u'{company}'),
        StringStruct(u'FileDescription', u'{app_name}'),
        StringStruct(u'FileVersion', u'{version_str}'),
        StringStruct(u'InternalName', u'{app_name}'),
        StringStruct(u'LegalCopyright', u'Copyright (C) {app_name}'),
        StringStruct(u'OriginalFilename', u'{exe_name}'),
        StringStruct(u'ProductName', u'{app_name}'),
        StringStruct(u'ProductVersion', u'{version_str}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"""


def generate(output_path: str = None) -> str:
    """
    生成 version_info.txt

    Args:
        output_path: 输出文件路径，默认写到 build\\version_info.txt

    Returns:
        str: 实际写入的文件路径
    """
    if output_path is None:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version_info.txt')

    version_tuple = _to_version_tuple(config.APP_VERSION)
    content = VERSION_INFO_TEMPLATE.format(
        version_tuple=version_tuple,
        company=config.APP_NAME,
        app_name=config.APP_NAME,
        version_str=config.APP_VERSION,
        exe_name=f'{config.APP_NAME}.exe',
    )

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return output_path


if __name__ == '__main__':
    path = generate()
    print(f'已生成: {path}')
    print(f'版本号: {config.APP_VERSION} -> {_to_version_tuple(config.APP_VERSION)}')
