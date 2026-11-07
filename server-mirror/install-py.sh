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
while [ $# -gt 0 ]; do
    case "$1" in
        --port)     PORT="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --dir)      INSTALL_DIR="$2"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -14
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

# mirror.json 不存在才拉，避免覆盖已有配置
if [ ! -f mirror.json ]; then
    curl -fsSL "${REPO_RAW}/mirror.json" -o mirror.json || die "下载 mirror.json 失败"
fi

say "生成本机配置"

IP=$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
[ -n "${IP}" ] || IP="8.152.0.186"

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
