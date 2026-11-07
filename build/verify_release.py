"""
GitHub Release 发布校验（发版前检查草稿、发版后复核）

用法（项目根目录）：
    python build/verify_release.py --tag v2.0.0 --local-setup dist/installer/Windows_v2.0.0_Setup.exe
    python build/verify_release.py --tag v2.0.0-beta.1 --token <PAT> --allow-draft
    python build/verify_release.py --tag v2.0.0 --release-json release.json   # 离线：用保存的 API 返回体

只用标准库。逐项输出中文结果（✓ 通过 / ✗ 失败 / - 跳过），全部通过退出码 0，有失败为 1，
参数错误为 2（argparse 默认）。

规则来自更新通道契约（`tests/test_update_contract_v141.py` 文件头）：已安装的 1.4.1 只取第一个
文件名含 setup 的 .exe、不校验 digest；2.x 的更新器缺 digest 就拒绝自动安装；`releases/latest`
不返回 prerelease 与 draft。规则一览：

  (a) 版本四处一致：config.APP_VERSION、tag（去 v）、CHANGELOG.md 顶部版本段、Release 名称
      （预发布期间 CHANGELOG 顶部可以仍是"未发布"段；正式版必须已改为该版本段）
  (b) 恰好一个文件名含 setup（不区分大小写）的 .exe 资产，文件名为 ASCII 且等于 Windows_v{版本}_Setup.exe
  (c) 该资产带 sha256: 摘要；给了 --local-setup 时摘要与大小都与本地文件一致
  (d) -beta.N / -rc.N 后缀的 tag 必须 prerelease；正式版必须非 prerelease 且非 draft
  (e) Release 正文不超过 60 行；正式版须含"升级须知"
  (f) tag 以 v 开头，且形如 vX.Y.Z / vX.Y.Z-beta.N / vX.Y.Z-rc.N
  (g) releases/latest：正式版发布后应指向本 tag，预发布不得是 latest（在线或给了 --latest-json 时检查）
"""
import argparse
import ast
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_API_BASE = "https://api.github.com"
MAX_BODY_LINES = 60
UPGRADE_NOTES_KEYWORD = "升级须知"
UNRELEASED_TITLE = "未发布"

_GA_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PRERELEASE_RE = re.compile(r"^\d+\.\d+\.\d+-(?:beta|rc)\.\d+$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
# CHANGELOG 版本段标题：## / ### 开头，如 "### v2.0.0 (2026-12-01)" 或 "### 未发布"
_CHANGELOG_HEADING_RE = re.compile(r"^#{2,3}\s+(.+?)\s*$")
_CHANGELOG_VERSION_RE = re.compile(r"^[vV]?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)\b")


@dataclass
class Check:
    rule: str        # 规则编号 a–g
    title: str       # 检查项
    status: str      # "pass" / "fail" / "skip"
    detail: str = ""


def _pass(rule, title, detail=""):
    return Check(rule, title, "pass", detail)


def _fail(rule, title, detail):
    return Check(rule, title, "fail", detail)


def _skip(rule, title, detail):
    return Check(rule, title, "skip", detail)


# ---------------------------------------------------------------- 输入


def read_config_value(config_path: str, name: str) -> Optional[str]:
    """从 config.py 按语法树读取模块级字符串常量（不导入，避免执行任何代码）"""
    with open(config_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=config_path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name \
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def changelog_top_section(changelog_path: str) -> Optional[str]:
    """CHANGELOG 第一个版本段标题的文字（如 "v2.0.0 (2026-12-01)" 或 "未发布"）；没有则 None"""
    with open(changelog_path, encoding="utf-8") as f:
        for line in f:
            match = _CHANGELOG_HEADING_RE.match(line.rstrip("\n"))
            if match:
                return match.group(1)
    return None


def _http_get_json(url: str, token: Optional[str]):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ProcessMonitor-verify-release",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    handlers = []
    if host in ("127.0.0.1", "localhost", "::1"):
        # 本地地址（测试用的假服务端）不走系统代理
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(urllib.request.Request(url, headers=headers), timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_release(api_base: str, repo: str, tag: str, token: Optional[str]) -> dict:
    """按 tag 取 Release；草稿不能按 tag 查到，带令牌时退回列表里找"""
    base = f"{api_base.rstrip('/')}/repos/{repo}/releases"
    try:
        return _http_get_json(f"{base}/tags/{urllib.parse.quote(tag)}", token)
    except urllib.error.HTTPError as e:
        if e.code != 404 or not token:
            raise
    for release in _http_get_json(f"{base}?per_page=100", token):
        if release.get("tag_name") == tag:
            return release
    raise LookupError(f"仓库 {repo} 中找不到 tag 为 {tag} 的 Release（含草稿）")


def fetch_latest(api_base: str, repo: str, token: Optional[str]) -> Optional[dict]:
    try:
        return _http_get_json(f"{api_base.rstrip('/')}/repos/{repo}/releases/latest", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ---------------------------------------------------------------- 规则


def tag_version(tag: str) -> str:
    return tag[1:] if tag[:1] in ("v", "V") else tag


def is_prerelease_version(version: str) -> bool:
    return "-" in version


def setup_assets(release: dict) -> List[dict]:
    """与 1.4.1 更新器同一口径：文件名含 setup（不区分大小写）且以 .exe 结尾"""
    result = []
    for asset in release.get("assets") or []:
        name = (asset.get("name") or "").lower()
        if "setup" in name and name.endswith(".exe"):
            result.append(asset)
    return result


def _version_token_in(text: str, version: str) -> bool:
    """text 中是否出现完整的版本号（可带 v 前缀；2.0.0 不匹配 2.0.0-beta.1 或 12.0.0）"""
    pattern = r"(?<![0-9A-Za-z.\-])[vV]?" + re.escape(version) + r"(?![0-9A-Za-z\-]|\.[0-9A-Za-z])"
    return re.search(pattern, text or "") is not None


def check_tag_format(tag: str) -> List[Check]:
    checks = []
    if tag.startswith("v"):
        checks.append(_pass("f", "tag 以 v 开头", tag))
    else:
        checks.append(_fail("f", "tag 以 v 开头", f"tag 为 {tag!r}，应为 v{tag_version(tag)}"))
    version = tag_version(tag)
    if _GA_RE.match(version) or _PRERELEASE_RE.match(version):
        checks.append(_pass("f", "tag 格式", "正式版" if _GA_RE.match(version) else "预发布"))
    else:
        checks.append(_fail("f", "tag 格式",
                            f"{tag} 不符合 vX.Y.Z / vX.Y.Z-beta.N / vX.Y.Z-rc.N"))
    return checks


def check_versions(tag: str, release: dict, app_version: Optional[str],
                   changelog_section: Optional[str]) -> List[Check]:
    version = tag_version(tag)
    checks = []

    rel_tag = release.get("tag_name") or ""
    if rel_tag == tag:
        checks.append(_pass("a", "Release 的 tag_name 与 --tag 一致", rel_tag))
    else:
        checks.append(_fail("a", "Release 的 tag_name 与 --tag 一致",
                            f"Release 的 tag_name 为 {rel_tag!r}，--tag 为 {tag!r}"))

    if app_version is None:
        checks.append(_fail("a", "config.APP_VERSION 与 tag 一致", "config.py 中找不到 APP_VERSION"))
    elif app_version == version:
        checks.append(_pass("a", "config.APP_VERSION 与 tag 一致", app_version))
    else:
        checks.append(_fail("a", "config.APP_VERSION 与 tag 一致",
                            f"APP_VERSION 为 {app_version}，tag 版本为 {version}"))

    if changelog_section is None:
        checks.append(_fail("a", "CHANGELOG 顶部版本段与 tag 一致", "CHANGELOG.md 中没有版本段标题"))
    elif changelog_section.startswith(UNRELEASED_TITLE) and is_prerelease_version(version):
        # 发布计划：beta / rc 期间变更继续写在"未发布"段，正式版时一次性整理为版本段
        checks.append(_pass("a", "CHANGELOG 顶部版本段与 tag 一致", "预发布期间为“未发布”段"))
    elif changelog_section.startswith(UNRELEASED_TITLE):
        checks.append(_fail("a", "CHANGELOG 顶部版本段与 tag 一致",
                            f"顶部仍为“{UNRELEASED_TITLE}”段，发布前应改为 v{version}"))
    else:
        match = _CHANGELOG_VERSION_RE.match(changelog_section)
        found = match.group(1) if match else None
        if found == version:
            checks.append(_pass("a", "CHANGELOG 顶部版本段与 tag 一致", changelog_section))
        else:
            checks.append(_fail("a", "CHANGELOG 顶部版本段与 tag 一致",
                                f"顶部版本段为“{changelog_section}”，tag 版本为 {version}"))

    name = (release.get("name") or "").strip()
    if not name:
        checks.append(_fail("a", "Release 名称含版本号", "Release 名称为空"))
    elif _version_token_in(name, version):
        checks.append(_pass("a", "Release 名称含版本号", name))
    else:
        checks.append(_fail("a", "Release 名称含版本号", f"名称“{name}”中没有完整的 {version}"))
    return checks


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_assets(tag: str, release: dict, local_setup: Optional[str]) -> List[Check]:
    version = tag_version(tag)
    expected_name = f"Windows_v{version}_Setup.exe"
    checks = []
    candidates = setup_assets(release)
    if len(candidates) != 1:
        names = "、".join(a.get("name") or "?" for a in candidates) or "无"
        checks.append(_fail("b", "恰好一个含 setup 的 .exe 资产",
                            f"找到 {len(candidates)} 个（{names}）；1.4.1 只取第一个"))
        checks.append(_skip("c", "安装包 SHA-256 摘要", "没有唯一的安装包资产"))
        return checks
    asset = candidates[0]
    name = asset.get("name") or ""
    checks.append(_pass("b", "恰好一个含 setup 的 .exe 资产", name))
    if not name.isascii():
        checks.append(_fail("b", "安装包文件名为 ASCII", f"{name} 含非 ASCII 字符"))
    elif name != expected_name:
        checks.append(_fail("b", "安装包文件名", f"为 {name}，应为 {expected_name}"))
    else:
        checks.append(_pass("b", "安装包文件名", name))
    state = asset.get("state")
    if state is not None and state != "uploaded":
        checks.append(_fail("b", "安装包上传完成", f"资产状态为 {state}"))

    digest = asset.get("digest")
    match = _SHA256_DIGEST_RE.match((digest or "").strip())
    if not match:
        detail = "资产没有 digest 字段" if not digest else f"digest 格式无效：{digest}"
        checks.append(_fail("c", "安装包 SHA-256 摘要",
                            f"{detail}；2.x 的更新器会拒绝自动安装"))
        if local_setup:
            checks.append(_skip("c", "摘要与本地安装包一致", "Release 没有可比对的摘要"))
        return checks
    remote = match.group(1).lower()
    checks.append(_pass("c", "安装包 SHA-256 摘要", f"sha256:{remote[:12]}…"))
    if not local_setup:
        checks.append(_skip("c", "摘要与本地安装包一致", "未提供 --local-setup"))
        return checks
    if not os.path.isfile(local_setup):
        checks.append(_fail("c", "摘要与本地安装包一致", f"本地文件不存在：{local_setup}"))
        return checks
    local = sha256_of(local_setup)
    if local == remote:
        checks.append(_pass("c", "摘要与本地安装包一致", os.path.basename(local_setup)))
    else:
        checks.append(_fail("c", "摘要与本地安装包一致",
                            f"本地 {local[:12]}… ≠ Release {remote[:12]}…"))
    size = asset.get("size")
    local_size = os.path.getsize(local_setup)
    if isinstance(size, int) and size != local_size:
        checks.append(_fail("c", "大小与本地安装包一致", f"本地 {local_size} 字节 ≠ Release {size} 字节"))
    return checks


def check_prerelease_flags(tag: str, release: dict, allow_draft: bool) -> List[Check]:
    version = tag_version(tag)
    prerelease = bool(release.get("prerelease"))
    draft = bool(release.get("draft"))
    checks = []
    if _PRERELEASE_RE.match(version):
        if prerelease:
            checks.append(_pass("d", "预发布已勾选 prerelease"))
        else:
            checks.append(_fail("d", "预发布已勾选 prerelease",
                                f"{tag} 带预发布后缀却未标记 prerelease：会成为 latest 并推送给 1.4.1 用户"))
        if draft:
            checks.append(_pass("d", "草稿状态", "草稿（发布前检查）") if allow_draft
                          else _fail("d", "草稿状态", "Release 仍是草稿；发布前检查请加 --allow-draft"))
    elif _GA_RE.match(version):
        if prerelease:
            checks.append(_fail("d", "正式版未勾选 prerelease",
                                "正式版标记了 prerelease：releases/latest 不会返回它，用户收不到更新"))
        else:
            checks.append(_pass("d", "正式版未勾选 prerelease"))
        if not draft:
            checks.append(_pass("d", "正式版已发布（非草稿）"))
        elif allow_draft:
            checks.append(_skip("d", "正式版已发布（非草稿）", "草稿（发布前检查），发布后请不带 --allow-draft 复核"))
        else:
            checks.append(_fail("d", "正式版已发布（非草稿）", "Release 仍是草稿"))
    else:
        checks.append(_fail("d", "prerelease 标记", f"版本 {version} 不符合命名规则，无法判断"))
    return checks


def check_body(tag: str, release: dict) -> List[Check]:
    body = (release.get("body") or "").replace("\r\n", "\n").rstrip("\n")
    lines = body.split("\n") if body else []
    checks = []
    if len(lines) <= MAX_BODY_LINES:
        checks.append(_pass("e", f"正文不超过 {MAX_BODY_LINES} 行", f"{len(lines)} 行"))
    else:
        checks.append(_fail("e", f"正文不超过 {MAX_BODY_LINES} 行",
                            f"{len(lines)} 行；1.4.1 关于页的更新说明只显示前 4000 字"))
    if is_prerelease_version(tag_version(tag)):
        checks.append(_skip("e", f"正文含“{UPGRADE_NOTES_KEYWORD}”", "预发布不要求"))
    elif UPGRADE_NOTES_KEYWORD in body:
        checks.append(_pass("e", f"正文含“{UPGRADE_NOTES_KEYWORD}”"))
    else:
        checks.append(_fail("e", f"正文含“{UPGRADE_NOTES_KEYWORD}”", "正式版正文缺少升级须知"))
    return checks


def check_latest(tag: str, release: dict, latest: Optional[dict], available: bool) -> List[Check]:
    title = "releases/latest 指向"
    if not available:
        return [_skip("g", title, "离线模式且未提供 --latest-json")]
    if release.get("draft"):
        return [_skip("g", title, "草稿尚未发布")]
    latest_tag = (latest or {}).get("tag_name")
    if is_prerelease_version(tag_version(tag)):
        if latest_tag == tag:
            return [_fail("g", title, f"预发布 {tag} 成了 latest，1.4.1 用户会收到它")]
        return [_pass("g", title, f"{latest_tag or '无'}（预发布不应是 latest）")]
    if latest_tag == tag:
        return [_pass("g", title, tag)]
    return [_fail("g", title, f"latest 为 {latest_tag or '无'}，不是 {tag}")]


def run_checks(tag: str, release: dict, *, app_version: Optional[str],
               changelog_section: Optional[str], local_setup: Optional[str] = None,
               allow_draft: bool = False, latest: Optional[dict] = None,
               latest_available: bool = False) -> List[Check]:
    checks: List[Check] = []
    checks += check_versions(tag, release, app_version, changelog_section)
    checks += check_assets(tag, release, local_setup)
    checks += check_prerelease_flags(tag, release, allow_draft)
    checks += check_body(tag, release)
    checks += check_tag_format(tag)
    checks += check_latest(tag, release, latest, latest_available)
    return checks


# ---------------------------------------------------------------- 输出与入口


def _symbols(stream) -> Tuple[str, str, str]:
    """控制台编码（如 GBK）写不出 ✓ ✗ 时退回 √ ×"""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "✓✗".encode(encoding)
        return "✓", "✗", "-"
    except (UnicodeEncodeError, LookupError):
        return "√", "×", "-"


def report(checks: List[Check], tag: str, source: str, stream=None) -> int:
    stream = stream or sys.stdout
    ok, bad, skip = _symbols(stream)
    mark = {"pass": ok, "fail": bad, "skip": skip}
    print(f"发布校验：{tag}（{source}）", file=stream)
    for c in checks:
        line = f"  {mark[c.status]} ({c.rule}) {c.title}"
        if c.detail:
            line += f"：{c.detail}"
        print(line, file=stream)
    failed = sum(1 for c in checks if c.status == "fail")
    passed = sum(1 for c in checks if c.status == "pass")
    skipped = sum(1 for c in checks if c.status == "skip")
    print(f"结果：通过 {passed} 项，失败 {failed} 项，跳过 {skipped} 项", file=stream)
    return 1 if failed else 0


def _default_repo(config_path: str) -> Optional[str]:
    owner = read_config_value(config_path, "GITHUB_OWNER")
    repo = read_config_value(config_path, "GITHUB_REPO")
    return f"{owner}/{repo}" if owner and repo else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_release.py",
        description="校验 GitHub Release 是否符合更新通道契约（版本一致、安装包资产、摘要、prerelease、正文）")
    parser.add_argument("--tag", required=True, help="要校验的 tag，如 v2.0.0 或 v2.0.0-beta.1")
    parser.add_argument("--repo", help="owner/name，缺省取 config.py 的 GITHUB_OWNER/GITHUB_REPO")
    parser.add_argument("--token", default=None,
                        help="GitHub 令牌（缺省读环境变量 GITHUB_TOKEN；查草稿时必需）")
    parser.add_argument("--local-setup", help="本地安装包路径，比对 SHA-256 与大小")
    parser.add_argument("--release-json", help="离线模式：Release API 返回体的 JSON 文件，不访问网络")
    parser.add_argument("--latest-json", help="离线模式下 releases/latest 返回体的 JSON 文件（可选）")
    parser.add_argument("--allow-draft", action="store_true",
                        help="发布前检查草稿：草稿状态不算失败")
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "config.py"),
                        help="读取 APP_VERSION 的 config.py（默认项目根）")
    parser.add_argument("--changelog", default=os.path.join(PROJECT_ROOT, "CHANGELOG.md"),
                        help="CHANGELOG.md 路径（默认项目根）")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE,
                        help="GitHub API 根地址（默认 https://api.github.com；测试用本地假服务端）")
    return parser


def _load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None, stream=None) -> int:
    args = build_parser().parse_args(argv)
    stream = stream or sys.stdout
    bad = _symbols(stream)[1]
    token = args.token or os.environ.get("GITHUB_TOKEN") or None
    try:
        app_version = read_config_value(args.config, "APP_VERSION")
        changelog_section = changelog_top_section(args.changelog)
    except (OSError, SyntaxError, UnicodeDecodeError) as e:
        print(f"{bad} 读取本地文件失败：{e}", file=stream)
        return 1

    latest, latest_available = None, False
    try:
        if args.release_json:
            release = _load_json(args.release_json)
            source = f"离线：{os.path.basename(args.release_json)}"
            if args.latest_json:
                latest, latest_available = _load_json(args.latest_json), True
        else:
            repo = args.repo or _default_repo(args.config)
            if not repo:
                print(f"{bad} 未指定 --repo，且 config.py 中没有 GITHUB_OWNER/GITHUB_REPO", file=stream)
                return 1
            release = fetch_release(args.api_base, repo, args.tag, token)
            source = f"在线：{repo}"
            latest, latest_available = fetch_latest(args.api_base, repo, token), True
    except (OSError, ValueError, LookupError, urllib.error.URLError) as e:
        print(f"{bad} 获取 Release 失败：{e}", file=stream)
        return 1
    if not isinstance(release, dict):
        print(f"{bad} Release 数据不是 JSON 对象", file=stream)
        return 1

    checks = run_checks(args.tag, release, app_version=app_version,
                        changelog_section=changelog_section, local_setup=args.local_setup,
                        allow_draft=args.allow_draft, latest=latest,
                        latest_available=latest_available)
    return report(checks, args.tag, source, stream)


if __name__ == "__main__":
    sys.exit(main())
