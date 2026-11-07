#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gh-proxy 代理逻辑测试
=====================
覆盖：
  1. proxy_url 的地址改写（github / objects / 非 github / 已套前缀 / 空代理）
  2. _fetch_with_proxy 的代理优先与失败回退
  3. manifest 里的 source 链接带上前缀
  4. 配置文件读取该字段

Run with:  python test-proxy.py
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "server-mirror"))

import mirror as M  # noqa: E402

PASS, FAIL = [], []
PROXY = "https://gh-proxy.org"


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""), flush=True)


def main():
    print("=== 1. proxy_url 地址改写 ===")

    raw = "https://github.com/xiaoyaya191/bilibili_learning_bot/releases/download/v3.1.6/BiliLearn-Web-3.1.6-Windows-x64.zip"
    want = "https://gh-proxy.org/" + raw
    got = M.proxy_url(raw, PROXY)
    check("github release 地址加前缀", got == want, got)

    obj = "https://objects.githubusercontent.com/gh/1/2/3"
    check("objects.githubusercontent.com 也加前缀",
          M.proxy_url(obj, PROXY) == "https://gh-proxy.org/" + obj)

    arch = "https://github.com/xiaoyaya191/bilibili_learning_bot/archive/refs/tags/v3.1.6.zip"
    check("archive 地址加前缀",
          M.proxy_url(arch, PROXY) == "https://gh-proxy.org/" + arch)

    check("非 github 地址不动",
          M.proxy_url("http://8.152.0.186:40034/files/a.zip", PROXY) ==
          "http://8.152.0.186:40034/files/a.zip")
    check("objects 之外的 CDN 不动",
          M.proxy_url("https://cdn.example.com/a.zip", PROXY) == "https://cdn.example.com/a.zip")

    check("空代理原样返回", M.proxy_url(raw, "") == raw)
    check("None 代理原样返回", M.proxy_url(raw, None) == raw)
    check("空 URL 不炸", M.proxy_url("", PROXY) == "")

    check("已套前缀不重复套",
          M.proxy_url(want, PROXY) == want, M.proxy_url(want, PROXY))

    check("代理带尾斜杠也正确",
          M.proxy_url(raw, "https://gh-proxy.org/") == want)

    other = "https://ghproxy.net"
    check("可换其他代理", M.proxy_url(raw, other) == "https://ghproxy.net/" + raw)

    print("\n=== 2. 默认配置 ===")
    check("默认启用代理", M.DEFAULT_CONFIG.get("githubProxy") == "https://gh-proxy.org")
    check("默认允许回退直连", M.DEFAULT_CONFIG.get("proxyFallback") is True)

    print("\n=== 3. _fetch_with_proxy 行为 ===")

    class Args:
        dry_run = False; verify = False; verbose = True; no_lock = True

    def make_mirror(over=None):
        tmp = Path(tempfile.mkdtemp(prefix="proxy-"))
        cfg = json.loads(json.dumps(M.DEFAULT_CONFIG))
        cfg["filesDir"] = str(tmp / "files")
        cfg["manifestPath"] = str(tmp / "manifest.json")
        cfg["requestTimeout"] = 3
        if over:
            cfg.update(over)
        return M.Mirror(cfg, Args())

    m = make_mirror()
    calls = []

    def fake_download_ok(url, dest):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return {"size": 1, "sha256": "y"}

    m.download = fake_download_ok
    dest = Path(m.cfg["filesDir"]) / "a.zip"
    used, info = m._fetch_with_proxy(raw, dest)
    check("优先使用代理地址", used == want, used)
    check("只调用一次 download", len(calls) == 1, calls)

    # 代理失败 -> 回退直连
    m2 = make_mirror()
    calls2 = []

    def fake_download_fail_first(url, dest):
        calls2.append(url)
        if url.startswith("https://gh-proxy.org/"):
            raise OSError("proxy down")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return {"size": 1, "sha256": "y"}

    m2.download = fake_download_fail_first
    used2, info2 = m2._fetch_with_proxy(raw, dest)
    check("代理失败后回退直连", used2 == raw, used2)
    check("两次尝试都发生了", len(calls2) == 2, calls2)
    check("直连成功返回结果", info2.get("size") == 1)

    # 关闭回退 -> 直接抛错
    m3 = make_mirror({"proxyFallback": False})

    def always_fail(url, dest):
        raise OSError("proxy down")

    m3.download = always_fail
    try:
        m3._fetch_with_proxy(raw, dest)
        check("关闭回退时抛出异常", False)
    except OSError:
        check("关闭回退时抛出异常", True)

    # 代理为空 -> 直连
    m4 = make_mirror({"githubProxy": ""})
    calls4 = []
    m4.download = lambda url, d: (calls4.append(url),
                                  d.parent.mkdir(parents=True, exist_ok=True),
                                  d.write_bytes(b"x"),
                                  {"size": 1, "sha256": "y"})[-1]
    used4, _ = m4._fetch_with_proxy(raw, dest)
    check("未配置代理时直连", used4 == raw and calls4 == [raw])

    print("\n=== 4. manifest 的 source 链接 ===")
    rel = [{"tag_name": "v3.1.6", "prerelease": False, "draft": False,
            "published_at": "2026-10-10T00:00:00Z",
            "assets": [{"name": "BiliLearn-Web-3.1.6-Windows-x64.zip",
                        "browser_download_url": raw, "size": 1}]}]

    # mock 掉真实下载，专注验证 manifest 内容
    def stub_download(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return {"size": 1, "sha256": "abc"}

    orig_download = M.Mirror.download
    M.Mirror.download = stub_download
    try:
        m5 = make_mirror()
        versions = m5.run(m5.plan(rel))
        src = versions["3.1.6"]["assets"].get("source", {}).get("url", "")
        check("source 链接带代理前缀", src.startswith("https://gh-proxy.org/https://github.com/"), src)
        check("source 指向正确 tag", "v3.1.6.zip" in src, src)

        m6 = make_mirror({"githubProxy": ""})
        versions6 = m6.run(m6.plan(rel))
        src6 = versions6["3.1.6"]["assets"].get("source", {}).get("url", "")
        check("关掉代理后 source 直连 GitHub", src6.startswith("https://github.com/"), src6)
    finally:
        M.Mirror.download = orig_download

    print("\n=== 5. 安装包下载地址仍指向本机 ===")
    win = versions["3.1.6"]["assets"]["windows"]
    check("windows 下载地址是本机", win["url"].startswith(m5.cfg["publicBaseUrl"]), win["url"])
    check("windows 不带代理前缀", "gh-proxy.org" not in win["url"], win["url"])

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
