#!/usr/bin/env bash
# =====================================================================
# BiliLearn 下载站 · 纯 Python 一键部署
# =====================================================================
# 用法（服务器上一条命令）：
#   curl -fsSL https://raw.githubusercontent.com/baixiangyuan/bililearn-download-site/main/server-mirror/install-py.sh | bash
#
# 自定义端口：
#   curl -fsSL .../install-py.sh | bash -s -- --port 40035
#
# 不装 nginx、不装 Docker。只用 Python 标准库 + systemd 常驻。
# 完全不动服务器上已有的 openresty:80 和 docker-proxy:40033。
# =====================================================================
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/baixiangyuan/bililearn-download-site/main/server-mirror"
INSTALL_DIR="/opt/dl-mirror"
PORT="40034"
INTERVAL="1800"

# ---------- 参数解析 ----------
FORCE_IP=""
while [ $# -gt 0 ]; do
    case "$1" in
        --port)     PORT="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --dir)      INSTALL_DIR="$2"; shift 2 ;;
        --ip)       FORCE_IP="$2"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -18
            exit 0 ;;
        *) echo "未知参数：$1"; exit 1 ;;
    esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[×] %s\033[0m\n' "$*"; exit 1; }

# ---------- 0. 环境检查 ----------
say "检查环境"

if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 python3，正在安装…"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y -qq python3
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q python3
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q python3
    else
        die "无法自动安装 python3，请手动安装后重试"
    fi
fi
python3 --version

if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
    die "端口 ${PORT} 已被占用，换一个：--port 40035"
fi
echo "端口 ${PORT} 可用"

command -v systemctl >/dev/null 2>&1 || warn "无 systemd，服务方式不可用，可用前台方式运行"

# ---------- 1. 落盘文件 ----------
say "写入 ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
cd "${INSTALL_DIR}"

curl -fsSL "${REPO_RAW}/mirror.py" -o mirror.py || die "下载 mirror.py 失败"
curl -fsSL "${REPO_RAW}/serve.py"  -o serve.py  || die "下载 serve.py 失败"

# mirror.json 每次都拉最新的（平台规则、代理配置可能已更新）。
# 下面只覆盖本机相关的地址字段，用户手动改过的 repo / keepVersions 等会保留。
if [ -f mirror.json ]; then
    cp -f mirror.json mirror.json.bak
    echo "已备份原配置到 mirror.json.bak"
fi
curl -fsSL "${REPO_RAW}/mirror.json" -o mirror.json.new || die "下载 mirror.json 失败"

say "合配置"

python3 - <<'PY'
import json, pathlib

new = json.loads(pathlib.Path("mirror.json.new").read_text(encoding="utf-8-sig"))

old = {}
bak = pathlib.Path("mirror.json.bak")
if bak.exists():
    try:
        old = json.loads(bak.read_text(encoding="utf-8-sig"))
    except Exception:
        old = {}

# 继承用户可能手动改过的项；平台规则、代理等一律用仓库最新版
for key in ("repo", "token", "includePrerelease", "keepVersions",
            "downloadWorkers", "requestTimeout", "chunkSize", "resume",
            "fallbackPlatform", "fallbackExtensions", "githubProxy",
            "proxyFallback", "urlTemplate"):
    if key in old:
        new[key] = old[key]

pathlib.Path("mirror.json").write_text(
    json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
print("配置已合并（平台规则用最新版，用户自定义项已保留）")
PY

rm -f mirror.json.new
# ---------- 探测公网 IP ----------
# 必须拿到公网地址：hostname -I 首项常是内网网段（阿里云典型是 172.x），
# 写进 manifest 后用户下载的就是打不开的内网地址。
say "确定对外地址"

IP=""
if [ -n "$FORCE_IP" ]; then
    IP="$FORCE_IP"
    echo "使用 --ip 指定：$IP"
else
    for svc in "https://api.ipify.org" "https://ifconfig.me/ip" "https://ip.sb" "https://icanhazip.com"; do
        IP=$(curl -fsS -m 6 "$svc" 2>/dev/null | tr -d '[:space:]')
        # 只接受合法 IPv4 公网地址：每段 0-255，且排除 10./172.16-31./192.168./127.
        # 以及环回、链路本地、运营商级 NAT、组播、广播和保留段。
        # （早期版本用 hostname -I 兜底，那会取到阿里云的 172.x 内网地址，
        #   导致 manifest 里的下载链接全部指向打不开的内网 IP。）
        if echo "$IP" | grep -qE '^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$' \
           && ! echo "$IP" | grep -qE '^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|0\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|22[4-9]\.|2[3-5][0-9]\.|255\.)'; then
            echo "探测到公网地址：$IP"
            break
        fi
        IP=""
    done
fi

if [ -z "$IP" ]; then
    warn "没能拿到公网地址。为避免把内网地址写进下载地址，请手动指定："
    printf '请输入服务器公网 IP： '
    read -r IP
    [ -n "$IP" ] || die "未提供 IP，退出"
fi

python3 - "$IP" "$PORT" <<'PY'
import json, sys, pathlib
ip, port = sys.argv[1], sys.argv[2]
p = pathlib.Path("mirror.json")
cfg = json.loads(p.read_text(encoding="utf-8-sig"))
cfg["filesDir"] = "/opt/dl-mirror/data/files"
cfg["manifestPath"] = "/opt/dl-mirror/data/manifest.json"
cfg["publicBaseUrl"] = "http://%s:%s/files" % (ip, port)
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print("  filesDir       = %s" % cfg["filesDir"])
print("  manifestPath   = %s" % cfg["manifestPath"])
print("  publicBaseUrl  = %s" % cfg["publicBaseUrl"])
PY

mkdir -p /opt/dl-mirror/data

# ---------- 2. systemd 服务 ----------
say "注册 systemd 服务"

cat > /etc/systemd/system/dl-mirror.service <<EOF
[Unit]
Description=BiliLearn download mirror (HTTP + GitHub release sync)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -u ${INSTALL_DIR}/serve.py --config ${INSTALL_DIR}/mirror.json --port ${PORT} --interval ${INTERVAL}
WorkingDirectory=${INSTALL_DIR}
Restart=always
RestartSec=10
# 单个大文件下载可能很慢，给足超时
TimeoutStopSec=30
# 日志交给 journald
StandardOutput=journal
StandardError=journal
# 降低优先级，别和线上业务抢 IO
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable dl-mirror.service >/dev/null 2>&1
systemctl restart dl-mirror.service

# ---------- 3. 等首次同步 ----------
say "等待首次同步（安装包大的话要几分钟）"

ok=""
for i in $(seq 1 60); do
    if curl -fsS -m 3 "http://127.0.0.1:${PORT}/manifest.json" >/dev/null 2>&1; then
        ok="yes"
        break
    fi
    printf '.'
    sleep 5
done
echo

if [ -z "$ok" ]; then
    warn "首次同步还没完成。看进度：journalctl -u dl-mirror -f"
else
    say "manifest 已就绪"
fi

# ---------- 4. 验证 ----------
say "本地验证"
curl -fsS -m 5 "http://127.0.0.1:${PORT}/healthz" && echo
echo "--- manifest 摘要 ---"
curl -fsS -m 5 "http://127.0.0.1:${PORT}/manifest.json" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('latest:', d.get('latest')); print('versions:', ', '.join((d.get('versions') or {}).keys()))" 2>/dev/null \
    || echo "（manifest 还没生成，等同步完成后重试）"

cat <<EOF

$(say "部署完成")

  服务地址    http://${IP}:${PORT}/manifest.json
  安装位置    ${INSTALL_DIR}
  同步间隔    ${INTERVAL} 秒

  常用命令：
    看日志        journalctl -u dl-mirror -f
    重启          systemctl restart dl-mirror
    手动同步一次  systemctl restart dl-mirror && sleep 2 && journalctl -u dl-mirror -n 30
    停止          systemctl stop dl-mirror
    卸载          systemctl disable --now dl-mirror && rm -rf ${INSTALL_DIR} /etc/systemd/system/dl-mirror.service

  接下来：
    1. 云厂商安全组放行 TCP ${PORT} 入方向（和 40033 同样加一条）
    2. 本机防火墙：firewall-cmd --permanent --add-port=${PORT}/tcp && firewall-cmd --reload
    3. 外网验证：curl -I http://${IP}:${PORT}/manifest.json
    4. Cloudflare Worker 的 manifestUrl 改成：
       http://${IP}:${PORT}/manifest.json

EOF
