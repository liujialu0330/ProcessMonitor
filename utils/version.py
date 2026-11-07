"""
版本号比较（SemVer 2.0 §11 的宽容实现）

放在工具层：更新检查（core.update_checker）与数据库元数据的只读判定（data.db.meta）
都要比较版本号，data 层不能依赖 core，所以比较逻辑只在这里定义一份。
core.update_checker.compare_version 从这里导入并原样导出，旧引用不变。
"""
import re
from typing import List, Tuple

_ASCII_DIGITS_RE = re.compile(r'[0-9]+')


def split_version(v) -> Tuple[List[int], List[str]]:
    """
    把版本号拆成（主版本段列表, 预发布标识符列表）；任何输入都不抛异常。

    规则：去首尾空白与 v/V 前缀 → 丢弃第一个 "+" 及其后的构建元数据 → 以第一个 "-"
    分出预发布部分（"-" 后为空视为没有预发布）→ 主版本按 "." 分段，非纯 ASCII 数字
    的段（含空段）按 0 处理；预发布按 "." 分成标识符原样保留。
    """
    text = str(v).strip().lstrip('vV')
    text = text.split('+', 1)[0]
    core, _, pre = text.partition('-')
    nums = [int(seg) if _ASCII_DIGITS_RE.fullmatch(seg) else 0 for seg in core.split('.')]
    return nums, (pre.split('.') if pre else [])


def _compare_prerelease(pa: list, pb: list) -> int:
    """SemVer 2.0 §11.4：逐个标识符比较，全部相同时标识符少者小"""
    for ia, ib in zip(pa, pb):
        a_num = _ASCII_DIGITS_RE.fullmatch(ia) is not None
        b_num = _ASCII_DIGITS_RE.fullmatch(ib) is not None
        if a_num and b_num:
            ka, kb = int(ia), int(ib)
        elif a_num != b_num:
            # 数字标识符的优先级低于字母数字标识符
            return -1 if a_num else 1
        else:
            # 都是字母数字标识符：按 ASCII 字典序（区分大小写）
            ka, kb = ia, ib
        if ka != kb:
            return -1 if ka < kb else 1
    if len(pa) != len(pb):
        return -1 if len(pa) < len(pb) else 1
    return 0


def compare_version(a: str, b: str) -> int:
    """
    按 SemVer 2.0 §11 比较版本号优先级，并保留旧有的宽容度：

    - 容忍 v/V 前缀；"+" 之后的构建元数据忽略；
    - 主版本段数不限，缺段按 0 补齐（"1.0" == "1.0.0"），非数字段按 0 处理；
    - 主版本相同时，带预发布的低于不带的（2.0.0-rc.1 < 2.0.0）；
    - 预发布按 "." 分段逐个比较：数字段按数值，字母数字段按 ASCII，数字段低于字母
      数字段，前缀相同时段少者小（2.0.0-beta < 2.0.0-beta.1 < 2.0.0-beta.2 < 2.0.0-rc.1）；
    - 任何输入（含非字符串、空串）都不抛异常。

    Returns:
        int: a < b 返回 -1，a > b 返回 1，相等返回 0
    """
    na, pa = split_version(a)
    nb, pb = split_version(b)
    width = max(len(na), len(nb))
    na += [0] * (width - len(na))
    nb += [0] * (width - len(nb))
    if na != nb:
        return -1 if na < nb else 1
    if pa and pb:
        return _compare_prerelease(pa, pb)
    if pa or pb:
        # 只有一方带预发布：带预发布的一方更小
        return -1 if pa else 1
    return 0


def release_part(version: str) -> str:
    """去掉预发布与构建元数据：'2.0.0-beta.1+abc' → '2.0.0'"""
    return str(version).strip().lstrip('vV').split('+', 1)[0].split('-', 1)[0]


def is_prerelease(version: str) -> bool:
    """是否预发布版本（带 "-" 后缀，如 2.0.0-beta.1）"""
    return bool(split_version(version)[1])
