#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serve.py 端到端测试
===================
不起真实同步线程，用一个假的 mirror 模块 + 预置文件，验证：
  1. /manifest.json 返回正确内容和缓存头
  2. /files/... 完整下载
  3. Range 请求（断点续传）的 206 响应和 Content-Range
  4. .part 临时文件被拒绝
  5. 路径穿越被拦截
  6. /healthz
  7. 根索引页

Run with:  python test-serve.py
"""

import hashlib
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "server-mirror"))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""), flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(port, path, method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, body


def main():
    tmp = Path(tempfile.mkdtemp(prefix="serve-test-"))
    root = tmp / "data"
    files = root / "files" / "3.1.6" / "windows"
    files.mkdir(parents=True)

    # 预置一个 1MB 假安装包 + 一个 .part
    payload = hashlib.sha256(b"installer").digest() * 32768  # 1 MiB
    exe = files / "BiliLearn-Web-3.1.6-Windows-x64.zip"
    exe.write_bytes(payload)
    (files / "partial.zip.part").write_bytes(b"half-done")

    manifest = {
        "schema": 1,
        "generatedAt": "2026-11-07T00:00:00Z",
        "repo": "xiaoyaya191/bilibili_learning_bot",
        "latest": "3.1.6",
        "versions": {
            "3.1.6": {
                "tag": "v3.1.6",
                "publishedAt": "2026-10-10",
                "assets": {
                    "windows": {
                        "url": "http://127.0.0.1/files/3.1.6/windows/BiliLearn-Web-3.1.6-Windows-x64.zip",
                        "size": len(payload),
                        "sizeText": "1.0 MB",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "file": "BiliLearn-Web-3.1.6-Windows-x64.zip"
                    }
                }
            }
        }
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    import serve as S

    port = free_port()
    srv = S.Server(("127.0.0.1", port), S.Handler, root)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    try:
        print("=== 1. manifest ===")
        st, h, b = req(port, "/manifest.json")
        check("manifest 200", st == 200, st)
        check("Content-Type 是 json", "application/json" in h.get("content-type", ""), h.get("content-type"))
        check("max-age=60（短缓存）", "max-age=60" in h.get("cache-control", ""), h.get("cache-control"))
        check("CORS 头存在", h.get("access-control-allow-origin") == "*")
        got = json.loads(b)
        check("latest 正确", got.get("latest") == "3.1.6")
        check("sha256 保留", got["versions"]["3.1.6"]["assets"]["windows"]["sha256"] == manifest["versions"]["3.1.6"]["assets"]["windows"]["sha256"])

        print("\n=== 2. 文件完整下载 ===")
        st, h, b = req(port, "/files/3.1.6/windows/BiliLearn-Web-3.1.6-Windows-x64.zip")
        check("文件 200", st == 200, st)
        check("大小正确", len(b) == len(payload), "%d vs %d" % (len(b), len(payload)))
        check("内容一致", b == payload)
        check("Accept-Ranges 存在", h.get("accept-ranges") == "bytes")
        check("强缓存", "max-age=31536000" in h.get("cache-control", ""))
        check("octet-stream", "octet-stream" in h.get("content-type", ""))

        print("\n=== 3. Range 断点续传 ===")
        st, h, b = req(port, "/files/3.1.6/windows/BiliLearn-Web-3.1.6-Windows-x64.zip",
                      headers={"Range": "bytes=0-1023"})
        check("Range 返回 206", st == 206, st)
        check("Content-Range 正确", h.get("content-range") == "bytes 0-1023/%d" % len(payload), h.get("content-range"))
        check("返回前 1024 字节", b == payload[:1024], len(b))
        check("Content-Length 匹配", h.get("content-length") == "1024", h.get("content-length"))

        st, h, b = req(port, "/files/3.1.6/windows/BiliLearn-Web-3.1.6-Windows-x64.zip",
                      headers={"Range": "bytes=524288-"})
        check("开放式 Range 到末尾", st == 206 and len(b) == len(payload) - 524288, "%s %d" % (st, len(b)))
        check("尾部内容正确", b == payload[524288:])

        st, h, b = req(port, "/files/3.1.6/windows/BiliLearn-Web-3.1.6-Windows-x64.zip",
                      headers={"Range": "bytes=-100"})
        check("bytes=-100 取末尾", st == 206 and b == payload[-100:], st)

        st, h, b = req(port, "/files/3.1.6/windows/BiliLearn-Web-3.1.6-Windows-x64.zip",
                      headers={"Range": "bytes=99999999-"})
        check("越界 Range 返回 416", st == 416, st)

        print("\n=== 4. .part 不外泄 ===")
        st, h, b = req(port, "/files/3.1.6/windows/partial.zip.part")
        check(".part 返回 404", st == 404, st)
        check(".part 内容不泄漏", b"half-done" not in b)

        print("\n=== 5. 路径穿越拦截 ===")
        for p in ["/files/../../etc/passwd",
                  "/files/..%2f..%2fetc%2fpasswd",
                  "/files/3.1.6/../../mirifest.json"]:
            st, h, b = req(port, p)
            ok = st in (403, 404) and b"root:" not in b
            check("拦截 %s" % p, ok, "status=%s" % st)

        print("\n=== 6. healthz ===")
        st, h, b = req(port, "/healthz")
        check("healthz 200", st == 200, st)
        check("healthz 返回 ok", json.loads(b).get("ok") is True)

        print("\n=== 7. 根索引页 ===")
        st, h, b = req(port, "/")
        check("根路径 200", st == 200, st)
        check("是 HTML", b"<html" in b.lower())
        check("列出了文件", b"BiliLearn-Web-3.1.6-Windows-x64.zip" in b)
        check("有 manifest 链接", b"/manifest.json" in b)

        print("\n=== 8. HEAD 请求 ===")
        st, h, b = req(port, "/manifest.json", method="HEAD")
        check("HEAD 200 且无 body", st == 200 and b == b"", "body len=%d" % len(b))

        print("\n=== 9. 404 ===")
        st, h, b = req(port, "/files/9.9.9/nope.zip")
        check("不存在文件 404", st == 404, st)
        st, h, b = req(port, "/whatever")
        check("未知路径 404", st == 404, st)

        print("\n=== 10. manifest 缺失时降级 ===")
        (root / "manifest.json").unlink()
        st, h, b = req(port, "/manifest.json")
        check("manifest 缺失返回 503", st == 503, st)
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

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
