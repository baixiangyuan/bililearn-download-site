#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Release 镜像同步器
=========================

定时从 GitHub Releases 拉取最新安装包到本机，并生成 manifest.json。
前端（Cloudflare Worker）只读 manifest.json，所以发布新版本后无需改任何代码。

工作流程：
    1. 调用 GitHub API 列出 Releases（公开仓库无需 token）
    2. 按文件名规则把附件归类到 windows / mac / linux / android
    3. 新附件下载到 filesDir，已存在且大小一致的跳过（支持断点续传）
    4. 计算 SHA-256，写 manifest.json（原子替换，前端随时读到完整文件）
    5. 按 keepVersions 清理旧版本，回收磁盘

用法：
    python3 mirror.py                      # 正常跑一次
    python3 mirror.py --dry-run            # 只打印计划，不下载不写文件
    python3 mirror.py --verify             # 重新校验已有文件哈希
    python3 mirror.py --config /etc/mirror.json
    python3 mirror.py --strict             # 有附件失败就以非零退出（供监控用）

退出码：0 成功（含降级） / 1 有附件失败且 --strict / 2 配置或写入等致命错误
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "release-mirror/1.0"
SCHEMA = 1

# 平台匹配规则
# ---------------
# include：文件名（转小写后）命中任意一条正则即算该平台
# exclude：命中 exclude 的一律跳过，即使它同时命中 include。
#           用于把 "BiliLearn-Android-v3.1.5-beta-source.zip" 这种
#           名字里带 android 的源码包挡掉。
# defaultPlatform：include 全不命中时，落到这个平台（可为 null 表示丢弃）。
#           用来兜住 "BiliLearn-Web-3.1.4.zip" 这种没写平台名的安装包。
DEFAULT_CONFIG = {
    "repo": "xiaoyaya191/bilibili_learning_bot",
    "token": "",                       # 公开仓库留空；私有仓库填 GitHub PAT
    "filesDir": "/var/lib/dl-mirror/files",
    "manifestPath": "/var/lib/dl-mirror/manifest.json",
    "publicBaseUrl": "https://dl.example.com/files",
    # 可用占位符：{base} {version} {tag} {platform} {file}
    "urlTemplate": "",
    "platforms": {
        "windows": {
            "include": [r"windows", r"win64", r"win32", r"\.exe$", r"\.exe\.", r"setup", r"-pc-"],
            "exclude": [r"source", r"-src\.", r"sha256", r"checksum"],
            "defaultPlatform": "windows"
        },
        "android": {
            "include": [r"android", r"\.apk$"],
            "exclude": [r"source", r"-src\.", r"sha256", r"checksum"],
            "defaultPlatform": None
        },
        "mac": {
            "include": [r"mac", r"darwin", r"osx", r"\.dmg$"],
            "exclude": [r"source", r"-src\.", r"sha256", r"checksum"],
            "defaultPlatform": None
        },
        "linux": {
            "include": [r"linux", r"\.appimage$", r"\.deb$", r"\.rpm$"],
            "exclude": [r"source", r"-src\.", r"sha256", r"checksum"],
            "defaultPlatform": None
        }
    },
    # 没写平台名的安装包（如 BiliLearn-Web-3.1.4.zip）归到这个平台。
    # 只对下列归档后缀生效，避免把 README.pdf 之类也当安装包。
    "fallbackPlatform": "windows",
    "fallbackExtensions": [".zip", ".7z", ".rar", ".tar.gz", ".tgz", ".gz"],
    # GitHub 下载加速前缀。国内服务器直连 github.com 下载大文件很慢，
    # 走代理可显著提速。留空字符串 "" 则直连。
    # 示例：https://gh-proxy.org  或  https://ghproxy.net  或  https://mirror.ghproxy.com
    "githubProxy": "https://gh-proxy.org",
    # 代理失效时是否回退直连重试。True 时先试代理，失败自动改直连。
    "proxyFallback": True,
    "includePrerelease": False,
    "keepVersions": 5,                 # 只保留最近 N 个版本的安装包
    "downloadWorkers": 3,
    "requestTimeout": 30,
    "chunkSize": 1048576,
    "resume": True
}


def proxy_url(url: str, proxy: str) -> str:
    """给 GitHub 地址套代理前缀。

    https://github.com/a/b/releases/download/v1/c.zip
      -> https://gh-proxy.org/https://github.com/a/b/releases/download/v1/c.zip

    非 github.com / objects.githubusercontent.com 地址原样返回，
    避免把已有 CDN 的地址也绕进代理。
    """
    if not proxy or not url:
        return url
    proxy = proxy.rstrip("/")
    # 已经套过前缀就不重复套
    if url.startswith(proxy + "/"):
        return url
    for host in ("https://github.com/", "https://objects.githubusercontent.com/"):
        if url.startswith(host):
            return proxy + "/" + url
    return url


# ---------------------------------------------------------------- 工具函数

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def version_key(v: str):
    parts = re.findall(r"\d+|[A-Za-z]+", str(v))
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def human_size(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return "%d B" % int(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
    return ""


def file_sha256(path: Path, chunk: int = 1048576) -> str | None:
    """流式计算文件 SHA-256；失败返回 None（不阻塞主流程）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return None


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_atomic(path: Path, data: dict):
    """先写临时文件再 os.replace，保证前端不会读到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- 主逻辑

class Mirror:
    def __init__(self, cfg: dict, args: argparse.Namespace):
        self.cfg = cfg
        self.args = args
        self.files_dir = Path(cfg["filesDir"])
        self.manifest_path = Path(cfg["manifestPath"])
        # 平台规则编译。支持三种配置写法，统一转成同一结构：
        #   "windows": ["regex", ...]                          # 旧式，等价于 include
        #   "windows": {"include": [...], "exclude": [...]}     # 带排除
        #   "windows": {"include": [...], "defaultPlatform": "windows"}
        self.platforms = []
        for name, rule in (cfg.get("platforms") or {}).items():
            if isinstance(rule, list):
                inc, exc, default = rule, [], None
            elif isinstance(rule, dict):
                inc = rule.get("include") or []
                exc = rule.get("exclude") or []
                default = rule.get("defaultPlatform")
            else:
                continue
            if not inc and not default:
                continue
            self.platforms.append({
                "name": name,
                "include": [re.compile(p) for p in inc],
                "exclude": [re.compile(p) for p in exc],
                "defaultPlatform": default
            })
        # 所有平台规则都不命中时的兜底平台（仅对归档后缀生效）
        self.fallback_platform = cfg.get("fallbackPlatform")
        self.fallback_exts = tuple(
            (e if e.startswith(".") else "." + e).lower()
            for e in (cfg.get("fallbackExtensions") or [])
        )
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
        self.failures = []
        self.old_manifest = load_json(self.manifest_path) or {}
        self._prev_sha = {}
        for ver, info in (self.old_manifest.get("versions") or {}).items():
            for plat, a in (info.get("assets") or {}).items():
                # 兼容单文件（对象）和多文件（数组）两种形态
                items = a if isinstance(a, list) else [a]
                for item in items:
                    if isinstance(item, dict) and item.get("sha256"):
                        fname = item.get("file") or item.get("url", "").rsplit("/", 1)[-1]
                        if fname:
                            self._prev_sha[(ver, plat, fname)] = item["sha256"]

    # ---- 日志
    def log(self, msg: str):
        print("[%s] %s" % (now_iso(), msg), flush=True)

    def vlog(self, msg: str):
        if self.args.verbose:
            self.log(msg)

    # ---- GitHub API
    def gh_api(self, path: str):
        url = path if path.startswith("http") else "https://api.github.com" + path
        headers = {
            "User-Agent": UA,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        if self.cfg.get("token"):
            headers["Authorization"] = "Bearer " + self.cfg["token"]

        # api.github.com 在国内也可能不稳，同样先试代理再回退直连
        proxy = (self.cfg.get("githubProxy") or "").strip()
        if proxy:
            proxied = proxy + "/" + url
            try:
                self.vlog("经代理请求 API %s" % proxied)
                req = urllib.request.Request(proxied, headers=headers)
                with urllib.request.urlopen(req, timeout=self.cfg["requestTimeout"]) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                if not self.cfg.get("proxyFallback", True):
                    raise
                self.log("API 代理 %s 失败（%s），回退直连" % (proxy, e))

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.cfg["requestTimeout"]) as r:
            return json.loads(r.read().decode("utf-8"))

    def _fetch_with_proxy(self, url: str, dest: Path):
        """先试代理，失败且允许回退时改直连。

        返回 (实际使用的 url, download() 的结果)。代理不可用时日志说明，
        不影响最终结果——只是慢一点。
        """
        proxy = (self.cfg.get("githubProxy") or "").strip()
        if not proxy:
            return url, self.download(url, dest)

        proxied = proxy_url(url, proxy)
        if proxied != url:
            try:
                self.vlog("经代理下载 %s" % proxied)
                return proxied, self.download(proxied, dest)
            except Exception as e:
                if not self.cfg.get("proxyFallback", True):
                    raise
                self.log("代理 %s 失败（%s），回退直连" % (proxy, e))
        return url, self.download(url, dest)

    def list_releases(self) -> list:
        out, page = [], 1
        while page <= 10:
            batch = self.gh_api("/repos/%s/releases?per_page=100&page=%d"
                                % (self.cfg["repo"], page))
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    # ---- 归类
    def match_platform(self, filename: str):
        """按文件名判定平台；无法判定返回 None（该附件会被跳过）。

        判定顺序：
          1. 命中任一平台的 exclude  -> None（源码包、校验文件走这条）
          2. 命中任一平台的 include  -> 该平台
          3. 都没命中                -> fallbackPlatform（可为 None）
        """
        low = filename.lower()

        for rule in self.platforms:
            if any(p.search(low) for p in rule["exclude"]):
                self.vlog("排除附件 %s（命中 %s 的 exclude）" % (filename, rule["name"]))
                return None

        for rule in self.platforms:
            if any(p.search(low) for p in rule["include"]):
                return rule["name"]

        if self.fallback_platform and low.endswith(self.fallback_exts):
            self.vlog("附件 %s 无平台关键词，归入 %s" % (filename, self.fallback_platform))
            return self.fallback_platform
        return None
        return None

    def public_url(self, version: str, platform: str, filename: str, tag: str) -> str:
        tpl = self.cfg.get("urlTemplate") or "{base}/{version}/{platform}/{file}"
        return (tpl.replace("{base}", self.cfg["publicBaseUrl"].rstrip("/"))
                   .replace("{version}", version)
                   .replace("{tag}", tag)
                   .replace("{platform}", platform)
                   .replace("{file}", filename))

    # ---- 下载
    def download(self, url: str, dest: Path) -> dict:
        """下载到 .part，完成后原子改名。失败时保留 .part 以便下次续传。

        完整性校验基于响应头的 Content-Range / Content-Length 推算出的
        服务端总大小，而不是本次响应体长度——否则续传必然误报"下载不完整"。
        SHA-256 在续传时需覆盖整个文件（含已存在的部分），所以统一在
        落盘后重新计算，避免只哈希到本次写入的片段。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        last_err = None

        for attempt in (0, 1):
            resume = (attempt == 0 and self.cfg.get("resume", True)
                      and part.exists() and part.stat().st_size > 0)
            have = part.stat().st_size if resume else 0
            headers = {"User-Agent": UA}
            if resume:
                headers["Range"] = "bytes=%d-" % have

            req = urllib.request.Request(url, headers=headers)
            try:
                r = urllib.request.urlopen(req, timeout=self.cfg["requestTimeout"])
            except urllib.error.HTTPError as e:
                # 服务端不接受 Range（或文件已变）时，丢掉残文件重来一次
                if resume and e.code in (404, 416, 412):
                    self.vlog("续传被拒 (HTTP %s)，重新下载 %s" % (e.code, dest.name))
                    part.unlink(missing_ok=True)
                    continue
                last_err = e
                break
            except Exception as e:  # 网络中断等
                last_err = e
                break

            with r:
                status = getattr(r, "status", 200)
                partial = (status == 206)
                mode = "ab" if partial else "wb"
                if not partial:
                    have = 0  # 服务端返回了完整文件，从头写

                # 推算服务端文件总大小
                content_range = r.headers.get("Content-Range")  # "bytes 0-99/12345"
                content_length = int(r.headers.get("Content-Length") or 0)
                server_total = 0
                if content_range and "/" in content_range:
                    try:
                        server_total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        server_total = 0
                if not server_total:
                    server_total = content_length + (have if partial else 0)

                chunk = int(self.cfg["chunkSize"])
                try:
                    with open(part, mode) as f:
                        while True:
                            block = r.read(chunk)
                            if not block:
                                break
                            f.write(block)
                except Exception as e:
                    last_err = e
                    break

                got = part.stat().st_size
                if server_total and got < server_total:
                    # 保留 .part 供下次续传
                    last_err = IOError("下载不完整：%d/%d 字节" % (got, server_total))
                    break

            os.replace(part, dest)
            # 落盘后统一计算全文件哈希，续传场景才不会漏掉已有片段
            sha = file_sha256(dest)
            return {"size": dest.stat().st_size, "sha256": sha}

        raise RuntimeError("下载失败 %s：%s" % (dest.name, last_err))

    # ---- 规划
    def plan(self, releases: list) -> list:
        entries = []
        for rel in releases:
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not self.cfg.get("includePrerelease"):
                continue
            tag = rel.get("tag_name") or ""
            version = (tag.lstrip("vV") or tag)
            if not version:
                continue

            assets, seen_names = [], set()
            for a in rel.get("assets") or []:
                name = a.get("name") or ""
                if name in seen_names:
                    continue
                seen_names.add(name)
                plat = self.match_platform(name)
                # 匹配到平台就下载；没匹配到的一律跳过（源码包、校验文件等）
                if not plat:
                    self.vlog("跳过未匹配附件 %s" % name)
                    continue
                assets.append({"platform": plat, "name": name,
                               "url": a.get("browser_download_url"),
                               "size": int(a.get("size") or 0)})
            entries.append({"version": version, "tag": tag,
                            "publishedAt": (rel.get("published_at") or "")[:10],
                            "assets": assets})

        entries.sort(key=lambda e: version_key(e["version"]), reverse=True)
        keep = int(self.cfg.get("keepVersions") or 0)
        if keep > 0:
            entries = entries[:keep]
        return entries

    # ---- 执行
    def run(self, entries: list) -> dict:
        jobs = []
        for e in entries:
            for a in e["assets"]:
                jobs.append((e, a))

        # 收集 (version, platform, filename) -> 下载/校验结果
        results = {}

        def work(item):
            e, a = item
            key = (e["version"], a["platform"], a["name"])
            dest = self.files_dir / e["version"] / a["platform"] / a["name"]
            expected = a["size"]
            already = (dest.exists() and dest.stat().st_size == expected
                       and (not self.args.verify or self._prev_sha.get(key)))
            if already:
                self.stats["skipped"] += 1
                # 缓存命中时优先沿用上次算好的哈希，避免每次重算大文件
                sha = self._prev_sha.get(key) or file_sha256(dest)
                self.log("跳过 %s / %s（已存在）" % (e["version"], a["platform"]))
                return key, {"size": dest.stat().st_size, "sha256": sha,
                             "file": a["name"], "url": dest, "cached": True}
            if self.args.dry_run:
                self.log("[dry-run] 将下载 %s / %s -> %s" % (e["version"], a["platform"], dest))
                return key, {"size": expected, "sha256": None, "file": a["name"],
                             "url": dest, "cached": False, "dry": True}
            self.log("下载 %s / %s（%s）" % (e["version"], a["platform"], a["name"]))
            _, info = self._fetch_with_proxy(a["url"], dest)
            self.stats["downloaded"] += 1
            self.stats["bytes"] += info["size"]
            self.log("完成 %s / %s（%s）" % (e["version"], a["platform"], human_size(info["size"])))
            return key, {"size": info["size"], "sha256": info["sha256"],
                         "file": a["name"], "url": dest, "cached": False}

        if jobs:
            workers = max(1, int(self.cfg.get("downloadWorkers") or 1))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(work, j) for j in jobs]
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        key, info = fut.result()
                        results[key] = info
                    except Exception as ex:
                        self.stats["failed"] += 1
                        self.failures.append(str(ex))
                        self.log("错误：%s" % ex)

        # ---- 组装 manifest
        # 同一平台可能有多个附件（如 x64 + arm64），按平台分组存成数组
        versions = {}
        for e in entries:
            grouped = {}
            for a in e["assets"]:
                key = (e["version"], a["platform"], a["name"])
                info = results.get(key)
                if not info:
                    continue
                if self.args.dry_run and info.get("dry"):
                    entry = {
                        "url": self.public_url(e["version"], a["platform"], a["name"], e["tag"]),
                        "size": a["size"],
                        "sizeText": human_size(a["size"]),
                        "sha256": None,
                        "file": a["name"]
                    }
                else:
                    if not info.get("url").exists():
                        continue
                    entry = {
                        "url": self.public_url(e["version"], a["platform"], a["name"], e["tag"]),
                        "size": info["size"],
                        "sizeText": human_size(info["size"]),
                        "sha256": info["sha256"],
                        "file": a["name"]
                    }
                grouped.setdefault(a["platform"], []).append(entry)

            assets = {}
            for plat, items in grouped.items():
                # 单文件时保持对象形态（前端兼容），多文件时用数组
                assets[plat] = items[0] if len(items) == 1 else items
            # 源码包：直接指向 GitHub（走代理加速），不落盘
            if e["tag"]:
                assets["source"] = {
                    "url": proxy_url(
                        "https://github.com/%s/archive/refs/tags/%s.zip"
                        % (self.cfg["repo"], e["tag"]),
                        self.cfg.get("githubProxy") or ""),
                    "sizeText": "ZIP",
                    "note": "GitHub 源码"
                }
            if assets:
                versions[e["version"]] = {"tag": e["tag"],
                                          "publishedAt": e["publishedAt"],
                                          "assets": assets}

        return versions

    # ---- 清理旧版本与孤儿文件
    def prune(self, versions: dict):
        if self.args.dry_run:
            return
        if not self.files_dir.exists():
            return

        # 1. 删除整个旧版本目录（含残留的 .part 等）
        keep = set(versions.keys())
        for child in sorted(self.files_dir.iterdir()):
            if child.is_dir() and child.name not in keep:
                self.log("清理旧版本目录 %s" % child.name)
                shutil.rmtree(child, ignore_errors=True)

        # 2. 删除保留版本内的孤儿文件（附件被改名或从 Release 中移除）
        for ver, info in versions.items():
            want = set()
            for plat, a in (info.get("assets") or {}).items():
                items = a if isinstance(a, list) else [a]
                for item in items:
                    if isinstance(item, dict) and item.get("file"):
                        want.add((plat, item["file"]))
            vdir = self.files_dir / ver
            if not vdir.exists():
                continue
            for plat_dir in sorted(vdir.iterdir()):
                if not plat_dir.is_dir():
                    continue
                for f in sorted(plat_dir.iterdir()):
                    if f.is_file() and f.suffix != ".part" and (plat_dir.name, f.name) not in want:
                        self.log("清理孤儿文件 %s/%s/%s" % (ver, plat_dir.name, f.name))
                        try:
                            f.unlink()
                        except Exception:
                            pass
                # 平台目录空了就一并删掉
                try:
                    if not any(plat_dir.iterdir()):
                        plat_dir.rmdir()
                except Exception:
                    pass

    # ---- 入口
    def execute(self) -> int:
        self.log("仓库 %s 开始同步" % self.cfg["repo"])
        try:
            releases = self.list_releases()
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self.log("警告：GitHub API 限流或无权访问，保留现有 manifest 不变")
            else:
                self.log("警告：GitHub API 返回 HTTP %s，保留现有 manifest 不变" % e.code)
            return 0 if not self.cfg.get("strict") else 1
        except Exception as e:
            self.log("警告：无法连接 GitHub API（%s），保留现有 manifest 不变" % e)
            return 0 if not self.cfg.get("strict") else 1

        self.log("获取到 %d 个 release" % len(releases))
        entries = self.plan(releases)
        if not entries:
            self.log("没有可同步的 release（可能全是预发布或没有附件）")
            return 0
        self.log("纳入同步的版本：%s" % ", ".join(e["version"] for e in entries))

        versions = self.run(entries)
        self.prune(versions)

        if self.args.dry_run:
            self.log("[dry-run] 未写入 manifest")
        else:
            manifest = {
                "schema": SCHEMA,
                "generatedAt": now_iso(),
                "repo": self.cfg["repo"],
                "latest": entries[0]["version"] if entries else None,
                "versions": versions
            }
            try:
                write_json_atomic(self.manifest_path, manifest)
                self.log("已写入 %s" % self.manifest_path)
            except Exception as e:
                self.log("致命错误：写入 manifest 失败：%s" % e)
                return 2

        self.log("汇总：新下载 %d，跳过 %d，失败 %d，流量 %s"
                 % (self.stats["downloaded"], self.stats["skipped"],
                    self.stats["failed"], human_size(self.stats["bytes"])))
        if self.failures:
            self.log("失败明细：%s" % "; ".join(self.failures[:5]))
            return 1 if self.cfg.get("strict") else 0
        return 0


# ---------------------------------------------------------------- 配置与锁

def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and Path(path).exists():
        # utf-8-sig 顺手吃掉 Windows 编辑器常加的 BOM
        user = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        deep_merge(cfg, user)
    return cfg


class SingleInstance:
    """防止定时器在上一次还没跑完时重复启动。"""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self  # 非 Linux 环境不锁
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("已有实例在运行，本次退出", flush=True)
            self.fh.close()
            self.fh = None
            raise SystemExit(0)
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        if self.fh:
            try:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            except Exception:
                pass
            self.fh.close()
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GitHub Release 镜像同步器")
    ap.add_argument("--config", default="/etc/dl-mirror/mirror.json", help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不下载不写文件")
    ap.add_argument("--verify", action="store_true", help="忽略缓存判断，重新下载校验")
    ap.add_argument("--verbose", action="store_true", help="输出调试信息")
    ap.add_argument("--no-lock", action="store_true", help="跳过单实例锁")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    try:
        cfg = load_config(cfg_path)
    except Exception as e:
        print("致命错误：读取配置 %s 失败：%s" % (cfg_path, e), file=sys.stderr)
        return 2

    if not cfg.get("repo") or "/" not in cfg["repo"]:
        print("致命错误：repo 需写成 用户名/仓库名", file=sys.stderr)
        return 2

    if args.no_lock:
        return Mirror(cfg, args).execute()

    lock = Path(cfg["manifestPath"]).parent / "mirror.lock"
    with SingleInstance(lock):
        return Mirror(cfg, args).execute()


if __name__ == "__main__":
    sys.exit(main())
