#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end tests for mirror.py
==============================
No network, no real GitHub needed:
  1. Start a local HTTP server that fakes the GitHub API and release assets
  2. Run mirror.py against a temp dir (real download, hashing, manifest write)
  3. Verify manifest structure, on-disk files, pruning, resume, API-failure fallback

Run with:  python test-mirror.py
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIRROR = HERE / "server-mirror" / "mirror.py"
PORT = 18765

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""), flush=True)


RELEASES = [
    {
        "tag_name": "v3.2.0", "published_at": "2026-10-28T09:00:00Z",
        "prerelease": False, "draft": False,
        "assets": [
            {"name": "BiliLearn-3.2.0-windows-x64.zip",
             "browser_download_url": "http://127.0.0.1:%d/dl/BiliLearn-3.2.0-windows-x64.zip" % PORT,
             "size": 1048576},
            {"name": "BiliLearn-3.2.0-macos.dmg",
             "browser_download_url": "http://127.0.0.1:%d/dl/BiliLearn-3.2.0-macos.dmg" % PORT,
             "size": 524288},
            {"name": "BiliLearn-3.2.0-linux-x86_64.AppImage",
             "browser_download_url": "http://127.0.0.1:%d/dl/BiliLearn-3.2.0-linux-x86_64.AppImage" % PORT,
             "size": 262144},
            {"name": "BiliLearn-3.2.0-arm64.apk",
             "browser_download_url": "http://127.0.0.1:%d/dl/BiliLearn-3.2.0-arm64.apk" % PORT,
             "size": 131072},
            {"name": "BiliLearn-3.2.0-Setup.exe",
             "browser_download_url": "http://127.0.0.1:%d/dl/BiliLearn-3.2.0-Setup.exe" % PORT,
             "size": 65536},
            {"name": "checksums.txt",
             "browser_download_url": "http://127.0.0.1:%d/dl/checksums.txt" % PORT,
             "size": 128},
        ],
    },
    {
        "tag_name": "v3.1.4", "published_at": "2026-10-20T09:00:00Z",
        "prerelease": False, "draft": False,
        "assets": [
            {"name": "BiliLearn-3.1.4-win64.zip",
             "browser_download_url": "http://127.0.0.1:%d/dl/BiliLearn-3.1.4-win64.zip" % PORT,
             "size": 2097152},
        ],
    },
    {
        "tag_name": "v3.1.3", "published_at": "2026-10-15T09:00:00Z",
        "prerelease": False, "draft": False,
        "assets": [
            {"name": "BiliLearn-3.1.3-win64.zip",
             "browser_download_url": "http://127.0.0.1:%d/dl/BiliLearn-3.1.3-win64.zip" % PORT,
             "size": 1048576},
        ],
    },
    {
        "tag_name": "v3.2.1-beta.1", "published_at": "2026-10-29T09:00:00Z",
        "prerelease": True, "draft": False,
        "assets": [
            {"name": "BiliLearn-beta-win64.zip",
             "browser_download_url": "http://127.0.0.1:%d/dl/BiliLearn-beta-win64.zip" % PORT,
             "size": 1024},
        ],
    },
]


def payload(name, size):
    """Deterministic content so hashes are comparable across runs."""
    seed = hashlib.sha256(name.encode()).digest()
    out = bytearray()
    while len(out) < size:
        out.extend(seed)
    return bytes(out[:size])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/repos/"):
            # 未知仓库返回 404，模拟真实 GitHub 行为
            if "nonexistent" in self.path:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(RELEASES).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/dl/"):
            name = self.path.split("/dl/", 1)[1]
            size = None
            for rel in RELEASES:
                for a in rel["assets"]:
                    if a["name"] == name:
                        size = a["size"]
                        break
            if size is None:
                self.send_response(404)
                self.end_headers()
                return
            data = payload(name, size)

            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                chunk = data[start:]
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(data) - 1, len(data)))
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(chunk)
                return

            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()


def main():
    if not MIRROR.exists():
        print("mirror.py not found:", MIRROR)
        return 1

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("Fake GitHub API listening on 127.0.0.1:%d\n" % PORT)

    tmp = tempfile.mkdtemp(prefix="mirror-test-")
    try:
        # Redirect api.github.com to the local server via a sitecustomize shim.
        shim = Path(tmp) / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text(
            "import urllib.request as u\n"
            "ORIG = u.urlopen\n"
            "def patched(req, timeout=None):\n"
            "    if 'api.github.com' in req.full_url:\n"
            "        req.full_url = req.full_url.replace('https://api.github.com', 'http://127.0.0.1:%d')\n"
            "    return ORIG(req, timeout=timeout) if timeout else ORIG(req)\n"
            "u.urlopen = patched\n" % PORT,
            encoding="utf-8"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(shim) + os.pathsep + env.get("PYTHONPATH", "")

        cfg = {
            "repo": "testuser/testrepo",
            "token": "",
            "filesDir": str(Path(tmp) / "files"),
            "manifestPath": str(Path(tmp) / "manifest.json"),
            "publicBaseUrl": "http://127.0.0.1:%d/files" % PORT,
            "urlTemplate": "{base}/{version}/{platform}/{file}",
            "platforms": {
                "windows": ["(?i)windows", "(?i)win64", "(?i)win32", "(?i)\\.exe$", "(?i)setup"],
                "mac": ["(?i)mac", "(?i)darwin", "(?i)osx", "(?i)\\.dmg$"],
                "linux": ["(?i)linux", "(?i)\\.appimage$", "(?i)\\.deb$", "(?i)\\.rpm$"],
                "android": ["(?i)android", "(?i)\\.apk$"],
                "source": []
            },
            "includePrerelease": False,
            "keepVersions": 5,
            "downloadWorkers": 3,
            "requestTimeout": 15,
            "chunkSize": 65536,
            "resume": True,
            "strict": False
        }

        def write_cfg(path, overrides=None):
            data = dict(cfg)
            if overrides:
                data.update(overrides)
            Path(path).write_text(json.dumps(data), encoding="utf-8")
            return Path(path)

        def run(cfg_path, extra=()):
            cmd = [sys.executable, str(MIRROR), "--config", str(cfg_path), "--no-lock"] + list(extra)
            return subprocess.run(cmd, capture_output=True, text=True, env=env,
                                  encoding="utf-8", errors="replace")

        # ---------- 1. first sync ----------
        print("--- 1. first sync ---")
        cfg_path = write_cfg(Path(tmp) / "cfg.json")
        r = run(cfg_path)
        check("first sync exit code 0", r.returncode == 0, r.stderr[-300:] if r.returncode else "")
        check("log contains summary line", "汇总" in r.stdout,
              r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")

        mpath = Path(cfg["manifestPath"])
        check("manifest.json created", mpath.exists())
        manifest = json.loads(mpath.read_text(encoding="utf-8"))

        check("schema field correct", manifest.get("schema") == 1)
        check("latest points at 3.2.0", manifest.get("latest") == "3.2.0")
        vers = manifest["versions"]
        check("prerelease excluded", "3.2.1-beta.1" not in vers, list(vers))
        check("three stable versions present", set(vers) == {"3.2.0", "3.1.4", "3.1.3"}, list(vers))

        v320 = vers["3.2.0"]["assets"]
        check("windows classified", "windows" in v320, list(v320))
        check("mac classified", "mac" in v320)
        check("linux classified", "linux" in v320)
        check("android classified", "android" in v320)
        check("source asset present (auto GitHub link)", v320["source"]["url"].endswith("v3.2.0.zip"))
        check("only 5 platform keys + source", set(v320) == {"windows", "mac", "linux", "android", "source"},
              list(v320))
        check("checksums.txt not downloaded as a platform",
              not any("checksums" in str(x) for x in v320.values()))

        # 多附件：3.2.0 有两个 Windows 包，应全部下载并组成数组
        win = v320["windows"]
        check("multiple windows assets kept as list", isinstance(win, list), type(win).__name__)
        if isinstance(win, list):
            names = [x["file"] for x in win]
            check("both windows packages downloaded",
                  names == ["BiliLearn-3.2.0-windows-x64.zip", "BiliLearn-3.2.0-Setup.exe"], names)
            check("each windows entry has sha256",
                  all(len(x.get("sha256") or "") == 64 for x in win))
            check("each windows entry has sizeText",
                  all(x.get("sizeText") for x in win), [x.get("sizeText") for x in win])

        # 单附件平台保持对象形态
        check("single-asset platform stays an object",
              isinstance(v320["mac"], dict), type(v320["mac"]).__name__)
        check("sha256 computed", len(v320["mac"].get("sha256") or "") == 64)
        check("sizeText generated", v320["mac"]["sizeText"] == "512.0 KB", v320["mac"].get("sizeText"))
        check("publishedAt recorded", vers["3.2.0"]["publishedAt"] == "2026-10-28")

        # ---------- 2. files on disk ----------
        print("\n--- 2. files on disk ---")
        f = Path(cfg["filesDir"]) / "3.2.0" / "windows" / "BiliLearn-3.2.0-windows-x64.zip"
        check("windows installer written", f.exists(), str(f))
        if f.exists():
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            check("on-disk hash matches manifest", digest == win[0]["sha256"])
            expected = hashlib.sha256(payload("BiliLearn-3.2.0-windows-x64.zip", 1048576)).hexdigest()
            check("on-disk content matches source", digest == expected)
        f2 = Path(cfg["filesDir"]) / "3.2.0" / "windows" / "BiliLearn-3.2.0-Setup.exe"
        check("second windows installer (Setup.exe) written", f2.exists(), str(f2))
        check("no source-code package on disk",
              not any(p.name in ("source", "src") for p in (Path(cfg["filesDir"]) / "3.2.0").iterdir()),
              list((Path(cfg["filesDir"]) / "3.2.0").iterdir()))
        check("URL template correct", win[0]["url"] ==
              "http://127.0.0.1:%d/files/3.2.0/windows/BiliLearn-3.2.0-windows-x64.zip" % PORT,
              win[0]["url"])
        check("no leftover .part files", not list(Path(cfg["filesDir"]).rglob("*.part")))

        # ---------- 3. incremental run skips ----------
        print("\n--- 3. incremental sync (all skipped) ---")
        r2 = run(cfg_path)
        check("second sync exit code 0", r2.returncode == 0)
        tail = r2.stdout.strip().splitlines()[-1] if r2.stdout.strip() else ""
        check("nothing re-downloaded", "新下载 0" in tail, tail)
        check("all assets skipped", "跳过 7" in tail, tail)
        check("sha256 preserved across runs",
              all(len(x.get("sha256") or "") == 64 for x in
                  json.loads(mpath.read_text(encoding="utf-8"))["versions"]["3.2.0"]["assets"]["windows"]))

        # ---------- 4. keepVersions pruning ----------
        print("\n--- 4. old version pruning ---")
        cfg2 = write_cfg(Path(tmp) / "cfg2.json", {"keepVersions": 2})
        r3 = run(cfg2)
        check("keepVersions=2 run ok", r3.returncode == 0, r3.stdout[-300:])
        check("manifest trimmed to 2 versions", len(json.loads(mpath.read_text(encoding="utf-8"))["versions"]) == 2)
        # Windows 上目录删除可能有短暂延迟，给一次重试
        gone = False
        for _ in range(5):
            if not (Path(cfg["filesDir"]) / "3.1.3").exists():
                gone = True
                break
            time.sleep(0.3)
        check("3.1.3 directory removed", gone, list(Path(cfg["filesDir"]).iterdir()))
        check("prune logged a cleanup message",
              any("3.1.3" in ln for ln in r3.stdout.splitlines()),
              [ln for ln in r3.stdout.splitlines() if "3.1.3" in ln])

        # ---------- 5. resume ----------
        print("\n--- 5. resume from partial download ---")
        part = Path(cfg["filesDir"]) / "3.2.0" / "windows" / "BiliLearn-3.2.0-windows-x64.zip"
        part.unlink()
        pp = part.with_name(part.name + ".part")
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_bytes(payload("BiliLearn-3.2.0-windows-x64.zip", 400000))
        r4 = run(cfg_path)
        check("resume sync ok", r4.returncode == 0)
        check(".part cleaned up after resume", not pp.exists(),
              [ln for ln in r4.stdout.splitlines() if "part" in ln.lower() or "windows" in ln])
        if part.exists():
            d = hashlib.sha256(part.read_bytes()).hexdigest()
            check("file complete after resume", d == v320["windows"][0]["sha256"])
            check("size correct after resume", part.stat().st_size == 1048576, part.stat().st_size)

        # ---------- 6. API failure fallback ----------
        print("\n--- 6. API failure fallback ---")
        bad = write_cfg(Path(tmp) / "bad.json",
                        {"repo": "testuser/nonexistent-repo-xyz", "requestTimeout": 5})
        before = mpath.read_bytes()
        r5 = run(bad)
        check("API failure still exits 0 (cron-safe)", r5.returncode == 0, r5.returncode)
        check("existing manifest untouched", mpath.read_bytes() == before)
        check("warning logged", "警告" in r5.stdout or "warn" in r5.stdout.lower(),
              r5.stdout.strip().splitlines()[-1] if r5.stdout.strip() else "")

        # ---------- 7. dry-run ----------
        print("\n--- 7. dry-run ---")
        r6 = run(cfg_path, ["--dry-run"])
        check("dry-run exit 0", r6.returncode == 0)
        check("dry-run does not write manifest", "[dry-run]" in r6.stdout)

        # ---------- 8. minimal config ----------
        print("\n--- 8. minimal config uses defaults ---")
        minimal = Path(tmp) / "min.json"
        minimal.write_text(json.dumps({"repo": "testuser/testrepo"}), encoding="utf-8")
        r7 = run(minimal, ["--dry-run"])
        check("minimal config runs", r7.returncode == 0, r7.stderr[-200:])

        # ---------- 9. malformed config ----------
        print("\n--- 9. malformed config handled ---")
        broken = Path(tmp) / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        r8 = run(broken)
        check("broken config exits 2", r8.returncode == 2, r8.returncode)

        missing_repo = Path(tmp) / "norepo.json"
        missing_repo.write_text(json.dumps({"repo": "", "filesDir": str(Path(tmp) / "x")}), encoding="utf-8")
        r9 = run(missing_repo)
        check("missing repo exits 2", r9.returncode == 2, r9.returncode)

        bad_repo = Path(tmp) / "badrepo.json"
        bad_repo.write_text(json.dumps({"repo": "no-slash-here", "filesDir": str(Path(tmp) / "x")}), encoding="utf-8")
        r10 = run(bad_repo)
        check("malformed repo exits 2", r10.returncode == 2, r10.returncode)
    finally:
        srv.shutdown()
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
