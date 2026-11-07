#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BiliLearn 下载站 · 纯 Python 服务端
====================================
一个文件同时做两件事：

  1. HTTP 服务（ThreadingHTTPServer）
     - /manifest.json     短缓存 60s，前端据此渲染版本列表
     - /files/<版本>/<平台>/<文件名>   强缓存，支持 Range 断点续传
     - /healthz           健康检查
     - .part 下载中的临时文件一律拒绝外泄

  2. 后台同步线程
     - 按间隔调用 mirror.py 拉取 GitHub Releases
     - 首次启动立即同步一次，之后循环

用法：
    python3 serve.py --config /opt/dl-mirror/mirror.json --port 40034

systemd 会以服务方式常驻，崩溃自动重启。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 允许断点续传的响应块
CHUNK = 1024 * 256

LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print("[%s] %s" % (now(), msg), flush=True)


# ------------------------------------------------------------------ 同步线程

def sync_loop(config_path: Path, interval: int, once: bool) -> None:
    """首次立即同步，之后按间隔循环。单次失败不影响下一轮。"""
    sys.path.insert(0, str(config_path.parent))
    try:
        import mirror as M
    except Exception as e:
        log("无法加载 mirror.py（%s），同步线程退出" % e)
        return

    class Args:
        dry_run = False
        verify = False
        verbose = False
        no_lock = True

    while True:
        cfg = M.load_config(config_path)
        log("开始同步 %s" % cfg.get("repo"))
        try:
            code = M.Mirror(cfg, Args()).execute()
            log("同步结束，退出码 %s" % code)
        except Exception as e:
            log("同步异常（下轮重试）：%s" % e)

        if once:
            return
        time.sleep(max(60, int(interval)))


# ------------------------------------------------------------------ HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "dlmirror/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 工具
    def _root(self) -> Path:
        return self.server.root

    def _safe_path(self, url_path: str) -> Path | None:
        """把 URL 路径解析到 root 内，阻断 ../ 穿越。"""
        clean = urllib.parse.unquote(url_path).lstrip("/")
        target = (self._root() / clean).resolve()
        root = self._root().resolve()
        if target != root and root not in target.parents:
            return None
        return target

    def _send(self, code: int, body: bytes = b"", ctype="text/plain; charset=utf-8",
              headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Server", self.server_version)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默默认访问日志，只留错误
        pass

    # ---- 方法
    def do_GET(self):
        try:
            self._serve()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端主动断开，正常
        except Exception as e:
            # 兜底：任何未预期错误都返回 500，不能断开连接留 Empty reply
            log("处理 %s 出错：%s" % (self.path, e))
            try:
                self._send(500, ("Internal error: %s" % e).encode(),
                           "text/plain; charset=utf-8")
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def _serve(self):
        path = urllib.parse.urlsplit(self.path).path

        if path == "/healthz":
            return self._send(200, b'{"ok":true}', "application/json")

        if path == "/manifest.json":
            return self._manifest()

        if path.startswith("/files/"):
            return self._file(path)

        # 根路径给个简单的导航页，方便人工确认服务活着
        if path in ("/", "/index.html"):
            return self._send(200, self._index().encode("utf-8"), "text/html; charset=utf-8")

        return self._send(404, b"Not Found")

    def _manifest(self):
        mp = self._root() / "manifest.json"
        if not mp.exists():
            return self._send(503, b'{"error":"manifest not ready"}', "application/json")
        try:
            body = mp.read_bytes()
        except Exception as e:
            return self._send(500, str(e).encode(), "application/json")
        return self._send(200, body, "application/json; charset=utf-8", {
            # 必须短缓存：前端要尽快看到新版本
            "Cache-Control": "public, max-age=60"
        })

    def _index(self):
        root = self._root()
        rows = []
        files_dir = root / "files"
        if files_dir.exists():
            for ver in sorted(files_dir.iterdir(), reverse=True):
                if not ver.is_dir():
                    continue
                for plat in sorted(ver.iterdir()):
                    if not plat.is_dir():
                        continue
                    for f in sorted(plat.iterdir()):
                        if f.is_file() and not f.name.endswith(".part"):
                            size = f.stat().st_size
                            href = "/files/%s/%s/%s" % (ver.name, plat.name, f.name)
                            rows.append(
                                '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                                '<td><a href="%s">下载</a></td></tr>'
                                % (ver.name, plat.name, f.name, _human(size), href))
        body = "".join(rows) or '<tr><td colspan="4">暂无文件，等首次同步完成</td></tr>'
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>dlmirror</title><style>"
            "body{font-family:system-ui,'Microsoft YaHei',sans-serif;background:#f5f6fa;"
            "color:#333;padding:32px;margin:0}"
            ".box{max-width:900px;margin:0 auto;background:#fff;border-radius:12px;"
            "padding:28px;box-shadow:0 6px 24px rgba(0,0,0,.08)}"
            "h1{font-size:20px;margin:0 0 4px}"
            "p{color:#888;font-size:13px;margin:0 0 20px}"
            "table{width:100%;border-collapse:collapse;font-size:13px}"
            "th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #eee}"
            "th{color:#888;font-weight:500;font-size:12px}"
            "a{color:#667eea;text-decoration:none}"
            "</style></head><body><div class='box'>"
            "<h1>dlmirror</h1><p>下载站后端已就绪 · "
            "<a href='/manifest.json'>manifest.json</a> · "
            "<a href='/healthz'>healthz</a></p>"
            "<table><tr><th>版本</th><th>平台</th><th>文件</th><th>大小</th><th></th></tr>"
            + body +
            "</table></div></body></html>"
        )

    def _file(self, path: str):
        target = self._safe_path(path)
        if target is None:
            return self._send(403, b"Forbidden")
        if not target.exists() or not target.is_file():
            return self._send(404, b"Not Found")
        if target.name.endswith(".part"):
            # 下载中的临时文件绝不外泄
            return self._send(404, b"Not Found")

        size = target.stat().st_size
        ctype = _guess_ctype(target.name)

        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                start_s, end_s = m.group(1), m.group(2)
                if not start_s and not end_s:
                    return self._send(416, b"Invalid Range")
                if not start_s:  # bytes=-N 取末尾 N 字节
                    length = min(int(end_s), size)
                    start = size - length
                    end = size - 1
                else:
                    start = int(start_s)
                    end = int(end_s) if end_s else size - 1
                if start >= size or start > end:
                    return self._send(416, b"Range Not Satisfiable", headers={
                        "Content-Range": "bytes */%d" % size
                    })
                end = min(end, size - 1)
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                if self.command == "HEAD":
                    return
                try:
                    with open(target, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            block = f.read(min(CHUNK, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # 客户端中断下载，正常
                return

        # 完整文件
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(target, "rb") as f:
                while True:
                    block = f.read(CHUNK)
                    if not block:
                        break
                    self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _human(n) -> str:
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


def _guess_ctype(name: str) -> str:
    low = name.lower()
    if low.endswith(".json"):
        return "application/json; charset=utf-8"
    if low.endswith((".html", ".htm")):
        return "text/html; charset=utf-8"
    return "application/octet-stream"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, root: Path):
        super().__init__(addr, handler)
        self.root = root


# ------------------------------------------------------------------ 入口

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="下载站服务端（HTTP + 定时同步）")
    ap.add_argument("--config", default="/opt/dl-mirror/mirror.json")
    ap.add_argument("--port", type=int, default=40034)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--interval", type=int, default=1800, help="同步间隔秒，默认 1800")
    ap.add_argument("--once", action="store_true", help="只同步一次，不起 HTTP 服务")
    ap.add_argument("--no-sync", action="store_true", help="不起同步线程，只跑 HTTP 服务")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print("找不到配置文件：%s" % cfg_path, file=sys.stderr)
        return 2

    sys.path.insert(0, str(cfg_path.parent))
    try:
        import mirror as M
    except Exception as e:
        print("无法加载同目录的 mirror.py：%s" % e, file=sys.stderr)
        return 2

    cfg = M.load_config(cfg_path)
    root = Path(cfg.get("filesDir", "/var/lib/dl-mirror/files")).parent
    root.mkdir(parents=True, exist_ok=True)

    if args.once:
        class A:
            dry_run = False; verify = False; verbose = False; no_lock = True
        return M.Mirror(cfg, A()).execute()

    if not args.no_sync:
        t = threading.Thread(
            target=sync_loop,
            args=(cfg_path, args.interval, False),
            daemon=True, name="mirror-sync")
        t.start()

    srv = Server((args.host, args.port), Handler, root)
    log("HTTP 服务监听 %s:%d，根目录 %s" % (args.host, args.port, root))
    log("同步间隔 %d 秒" % args.interval)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
