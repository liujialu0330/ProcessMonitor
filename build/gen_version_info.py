"""
生成 PyInstaller 用的 Windows 版本信息文件（version_info.txt）

version_info.txt 是 PyInstaller 打包产物（build\\version_info.txt），不入库；
本脚本从 config.APP_VERSION 单源动态生成 4 元组版本号，供 build.bat 在
PyInstaller 打包前调用，杜绝版本号写死导致与 config.py 脱节。

用法：
    python build\\gen_version_info.py
"""
import os
import re
import sys

# 保证无论从哪个工作目录调用都能找到项目根目录下的 config.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config  # noqa: E402

# 第 4 段的取值：正式版取最大值，保证同一 X.Y.Z 下 beta < rc < 正式版，
# 与 core/update_checker.compare_version 的 SemVer 优先级同序
GA_BUILD = 100
RC_BUILD_BASE = 50
# beta.N / rc.N 的 N 必须小于此值，否则会越过下一档（beta.50 撞上 rc.0，rc.50 撞上正式版）
PRERELEASE_N_LIMIT = 50
# Windows 版本资源（VS_FIXEDFILEINFO）每段是 16 位无符号数
VERSION_FIELD_MAX = 65535

# 严格的 X.Y.Z[-预发布][+构建元数据]；不匹配时走旧的宽容解析
_SEMVER_RE = re.compile(
    r'^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$')
_PRERELEASE_RE = re.compile(r'^(beta|rc)\.(\d+)$')

MSG_FIELD_OUT_OF_RANGE = "版本号 {version} 映射为 {nums}，超出 Windows 版本资源每段 0~65535 的范围"
MSG_PRERELEASE_TOO_LARGE = "版本号 {version} 的预发布序号 {n} 过大（须小于 {limit}），会打乱 beta < rc < 正式版的顺序"


def _legacy_tuple(version_str: str) -> list:
    """旧规则：按 "." 取前 4 段，非数字段按 0，不足 4 段右侧补 0"""
    nums = []
    for p in version_str.split('.')[:4]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 4:
        nums.append(0)
    return nums


def _to_version_tuple(version_str: str) -> tuple:
    """
    将版本号转为 PyInstaller version_file 所需的 4 元组（Windows 文件版本号）

    映射规则（第 4 段表示发布阶段，使同一 X.Y.Z 下的文件版本号按发布顺序递增）：
    - "X.Y.Z"        → (X, Y, Z, 100)
    - "X.Y.Z-beta.N" → (X, Y, Z, N)
    - "X.Y.Z-rc.N"   → (X, Y, Z, 50 + N)
    - 其他预发布标签（如 "-alpha"、"-beta"）→ (X, Y, Z, 0)
    - "+构建元数据" 忽略
    - 不符合 "X.Y.Z[-预发布]" 的输入沿用旧规则：取前 4 段，非数字段按 0，不足补 0

    Raises:
        ValueError: beta.N / rc.N 的 N ≥ 50，或任一段超出 0~65535
    """
    match = _SEMVER_RE.match(version_str)
    if match is None:
        nums = _legacy_tuple(version_str)
    else:
        nums = [int(match.group(1)), int(match.group(2)), int(match.group(3))]
        pre = match.group(4)
        if pre is None:
            nums.append(GA_BUILD)
        else:
            pre_match = _PRERELEASE_RE.match(pre)
            if pre_match is None:
                nums.append(0)
            else:
                n = int(pre_match.group(2))
                if n >= PRERELEASE_N_LIMIT:
                    raise ValueError(MSG_PRERELEASE_TOO_LARGE.format(
                        version=version_str, n=n, limit=PRERELEASE_N_LIMIT))
                nums.append(n if pre_match.group(1) == 'beta' else RC_BUILD_BASE + n)
    if not all(0 <= x <= VERSION_FIELD_MAX for x in nums):
        raise ValueError(MSG_FIELD_OUT_OF_RANGE.format(version=version_str, nums=tuple(nums)))
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


def generate(output_path: str = None, version: str = None) -> str:
    """
    生成 version_info.txt

    Args:
        output_path: 输出文件路径，默认写到 build\\version_info.txt
        version: 版本号，默认 config.APP_VERSION（测试可显式传入）。
            4 元组按 _to_version_tuple 映射；FileVersion/ProductVersion 字符串
            保留原始文本（含 "-beta.1" 等预发布后缀），资源管理器里能看出是预发布

    Returns:
        str: 实际写入的文件路径
    """
    if output_path is None:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version_info.txt')
    if version is None:
        version = config.APP_VERSION

    version_tuple = _to_version_tuple(version)
    content = VERSION_INFO_TEMPLATE.format(
        version_tuple=version_tuple,
        company=config.APP_NAME,
        app_name=config.APP_NAME,
        version_str=version,
        exe_name=f'{config.APP_NAME}.exe',
    )

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return output_path


if __name__ == '__main__':
    path = generate()
    print(f'已生成: {path}')
    print(f'版本号: {config.APP_VERSION} -> {_to_version_tuple(config.APP_VERSION)}')
