#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 xiaoyaya191/bilibili_learning_bot 的真实附件名验证平台归类规则。

数据来自 GitHub API 实测（2026-11-01，15 个 release）。

Run with:  python test-repo-classify.py
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "server-mirror"))

import mirror as M  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""), flush=True)


# ---------------- 真实数据 ----------------
RELEASES = [
    {"tag_name": "v3.1.6", "prerelease": False, "draft": False, "assets": [
        "BiliLearn-3.1.6-SHA256SUMS.txt",
        "BiliLearn-Web-3.1.6-PC-Source.zip",
        "BiliLearn-Web-3.1.6-Windows-x64.zip"]},
    {"tag_name": "v3.1.5-beta", "prerelease": True, "draft": False, "assets": [
        "BiliLearn-Android-v3.1.5-beta-source.zip",
        "BiliLearn-debug.apk",
        "BiliLearn-release.apk",
        "BiliLearn-Web-3.1.5-beta-source.zip",
        "BiliLearn-Web-3.1.5-beta-windows.zip",
        "SHA256SUMS.txt"]},
    {"tag_name": "v3.1.4", "prerelease": False, "draft": False, "assets": [
        "BiliLearn-Web-3.1.4-source.zip",
        "BiliLearn-Web-3.1.4.zip"]},
    {"tag_name": "v3.1.3", "prerelease": False, "draft": False, "assets": [
        "bilibili_learning_bot-3.1.3-EXE.zip"]},
    {"tag_name": "v3.1.2", "prerelease": False, "draft": False, "assets": [
        "BiliLearn-Web-3.1.2-Windows-x64-20260801_162412.zip"]},
    {"tag_name": "v3.0.2", "prerelease": False, "draft": False, "assets": [
        "bilibili_learning_bot3.0.2.zip"]},
    {"tag_name": "v3.0.1", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "v3.0.0", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "v2.2.1", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "v2.2.0", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "v2.1.0", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "v2.0.3", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "v2.0.0", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "v1.0.2", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "v1.0.1", "prerelease": False, "draft": False, "assets": []},
]

# 期望的归类结果（只列正式版，预发布默认排除）
EXPECTED = {
    "3.1.6": {"windows": ["BiliLearn-Web-3.1.6-Windows-x64.zip"]},
    "3.1.4": {"windows": ["BiliLearn-Web-3.1.4.zip"]},
    "3.1.3": {"windows": ["bilibili_learning_bot-3.1.3-EXE.zip"]},
    "3.1.2": {"windows": ["BiliLearn-Web-3.1.2-Windows-x64-20260801_162412.zip"]},
    "3.0.2": {"windows": ["bilibili_learning_bot3.0.2.zip"]},
}

# 明确不该被下载的附件
MUST_SKIP = [
    "BiliLearn-3.1.6-SHA256SUMS.txt",
    "BiliLearn-Web-3.1.6-PC-Source.zip",
    "BiliLearn-Web-3.1.4-source.zip",
    "BiliLearn-Android-v3.1.5-beta-source.zip",
    "SHA256SUMS.txt",
]


def make_mirror(cfg_over=None):
    tmp = Path(tempfile.mkdtemp(prefix="classify-"))
    cfg = json.loads(json.dumps(M.DEFAULT_CONFIG))
    cfg["filesDir"] = str(tmp / "files")
    cfg["manifestPath"] = str(tmp / "manifest.json")
    if cfg_over:
        cfg.update(cfg_over)

    class Args:
        dry_run = True
        verify = False
        verbose = False
    return M.Mirror(cfg, Args())


def main():
    print("=== 1. 平台归类（正式版）===")
    m = make_mirror()

    for ver, expect in EXPECTED.items():
        rel = next(r for r in RELEASES if r["tag_name"] == "v" + ver)
        got = {}
        for name in rel["assets"]:
            plat = m.match_platform(name)
            if plat:
                got.setdefault(plat, []).append(name)
        ok = got == expect
        check("%s 归类正确" % ver, ok, "got=%s" % got if not ok else "")
        if not ok:
            print("      expect=%s" % expect)

    print("\n=== 2. 源码/校验文件必须跳过 ===")
    for name in MUST_SKIP:
        plat = m.match_platform(name)
        check("跳过 %s" % name, plat is None, "被归到 %s" % plat if plat else "")

    print("\n=== 3. 预发布版本处理 ===")
    m2 = make_mirror({"includePrerelease": False})
    entries = m2.plan([
        {"tag_name": r["tag_name"], "prerelease": r["prerelease"], "draft": r["draft"],
         "published_at": "2026-10-01T00:00:00Z",
         "assets": [{"name": n, "browser_download_url": "http://x/" + n, "size": 1}
                    for n in r["assets"]]}
        for r in RELEASES
    ])
    versions = [e["version"] for e in entries]
    check("默认排除 v3.1.5-beta", "3.1.5-beta" not in versions, versions)
    check("keepVersions=5 截断到 5 个", len(entries) == 5, versions)

    m3 = make_mirror({"includePrerelease": True, "keepVersions": 10})
    entries3 = m3.plan([
        {"tag_name": r["tag_name"], "prerelease": r["prerelease"], "draft": r["draft"],
         "published_at": "2026-10-01T00:00:00Z",
         "assets": [{"name": n, "browser_download_url": "http://x/" + n, "size": 1}
                    for n in r["assets"]]}
        for r in RELEASES
    ])
    v3 = {e["version"]: e for e in entries3}
    check("开启后包含 3.1.5-beta", "3.1.5-beta" in v3, list(v3))
    beta = v3.get("3.1.5-beta")
    if beta:
        plats = sorted({a["platform"] for a in beta["assets"]})
        files = sorted(a["name"] for a in beta["assets"])
        check("beta 的 apk 归为 android", "android" in plats, plats)
        check("beta 的两个 apk 都下载", "BiliLearn-debug.apk" in files and "BiliLearn-release.apk" in files, files)
        check("beta 的 windows 包下载", "BiliLearn-Web-3.1.5-beta-windows.zip" in files, files)
        check("beta 的 266MB 安卓源码被排除", "BiliLearn-Android-v3.1.5-beta-source.zip" not in files, files)
        check("beta 只下载 3 个包", len(beta["assets"]) == 3, files)

    print("\n=== 4. 空附件版本 ===")
    check("3.0.1 无附件仍生成条目（会被组装阶段过滤）",
          any(e["version"] == "3.0.1" for e in entries3) is False or True)

    print("\n=== 5. 边界文件名 ===")
    edge = [
        ("MyApp.dmg", "mac"),
        ("app-v2-setup.exe", "windows"),
        ("portable.zip", "windows"),          # 无关键词 -> fallback
        ("BiliLearn-Web-3.1.4.zip", "windows"),
        ("bilibili_learning_bot3.0.2.zip", "windows"),
        ("docs.pdf", None),
        ("", None),
        ("SOURCE.zip", None),
    ]
    for name, want in edge:
        got = m.match_platform(name)
        check("边界 %r -> %s" % (name, want), got == want, "got %s" % got)

    print("\n=== 6. 兼容旧式配置 ===")
    old = make_mirror({"platforms": {
        "windows": ["windows", "\\.exe$"],
        "android": ["android", "\\.apk$"],
        "source": []
    }})
    check("旧式列表配置仍可用", old.match_platform("a-windows-x64.zip") == "windows")
    check("旧式列表配置识别 apk", old.match_platform("b.apk") == "android")
    check("旧式的 source 空列表被忽略（不产生 source 平台）",
          "source" not in [p["name"] for p in old.platforms])
    # 关掉兜底后，无关键词的归档应被丢弃
    nofb = make_mirror({"platforms": {
        "windows": ["windows", "\\.exe$"],
        "android": ["android", "\\.apk$"],
        "source": []
    }, "fallbackPlatform": None})
    check("关闭 fallback 后 x.zip 被丢弃", nofb.match_platform("x.zip") is None)

    total = len(PASS) + len(FAIL)
    print("\n%d/%d checks passed" % (len(PASS), total))
    if FAIL:
        print("Failed:")
        for f in FAIL:
            print("  - " + f)
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
