#!/usr/bin/env bash
# =====================================================================
# BiliLearn 下载站 · Docker 一键部署
# =====================================================================
# 用法（服务器上一条命令）：
#   curl -fsSL https://raw.githubusercontent.com/baixiangyuan/bililearn-download-site/main/server-mirror/deploy-docker.sh | bash
#
# 自定义端口和地址：
#   curl -fsSL .../deploy-docker.sh | bash -s -- --port 40034 --host 8.152.0.186
#
# 特点：
#   - 不动服务器上任何现有配置（现有 openresty:80、docker-proxy:40033 均不受影响）
#   - 自带定时同步，容器重启自动恢复
#   - 卸载：docker compose down -v && rm -rf /opt/dl-mirror
# =====================================================================
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/baixiangyuan/bililearn-download-site/main/server-mirror"
INSTALL_DIR="/opt/dl-mirror"
PROJECT="dlmirror"
PORT="40034"
HOST="8.152.0.186"
INTERVAL="1800"

# ---------- 参数解析 ----------
while [ $# -gt 0 ]; do
    case "$1" in
        --port)     PORT="$2"; shift 2 ;;
        --host)     HOST="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --dir)      INSTALL_DIR="$2"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -20
            exit 0 ;;
        *) echo "未知参数：$1（用 --help 看用法）"; exit 1 ;;
    esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[×] %s\033[0m\n' "$*"; exit 1; }

# ---------- 0. 环境检查 ----------
say "检查运行环境"
command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装"
docker info >/dev/null 2>&1 || die "docker 守护进程未运行，或当前用户无权限（试试 sudo）"

if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    die "未找到 docker compose 插件（docker-compose v1 也可）"
fi
echo "使用：$DC"

# 端口冲突预检
if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
    die "端口 ${PORT} 已被占用，换一个：--port 40035"
fi
echo "端口 ${PORT} 可用"

# ---------- 1. 落盘文件 ----------
say "写入文件到 ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
cd "${INSTALL_DIR}"

# mirror.py / mirror.json 从仓库拉最新版，保证和 GitHub 上的实现一致
curl -fsSL "${REPO_RAW}/mirror.py"   -o mirror.py   || die "下载 mirror.py 失败"
curl -fsSL "${REPO_RAW}/mirror.json" -o mirror.json || die "下载 mirror.json 失败"

# 把配置里的路径改成容器内的 /data，下载地址改成对外的 IP:端口
python3 - "$HOST" "$PORT" <<'PY' 2>/dev/null || sed -i \
    -e "s|\"filesDir\": *\"[^\"]*\"|\"filesDir\": \"/data/files\"|" \
    -e "s|\"manifestPath\": *\"[^\"]*\"|\"manifestPath\": \"/data/manifest.json\"|" \
    -e "s|\"publicBaseUrl\": *\"[^\"]*\"|\"publicBaseUrl\": \"http://${HOST}:${PORT}/files\"|" \
    mirror.json
import json, sys, pathlib
host, port = sys.argv[1], sys.argv[2]
p = pathlib.Path("mirror.json")
cfg = json.loads(p.read_text(encoding="utf-8"))
cfg["filesDir"] = "/data/files"
cfg["manifestPath"] = "/data/manifest.json"
cfg["publicBaseUrl"] = f"http://{host}:{port}/files"
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print("配置已按容器环境改写")
PY

say "生成容器定义"

cat > Dockerfile <<'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY mirror.py /app/mirror.py
COPY mirror.json /app/mirror.json
# 健康检查用，避免额外装 curl
RUN pip install --no-cache-dir --quiet requests 2>/dev/null || true
ENTRYPOINT ["/app/entrypoint.sh"]
EOF

cat > entrypoint.sh <<'EOF'
#!/bin/sh
# 启动立即同步一次，之后按间隔循环。单次失败不退出，等下一轮重试。
set -u
INTERVAL="${MIRROR_INTERVAL:-1800}"
echo "[mirror] 容器启动，立即执行首次同步"
python3 -u /app/mirror.py --config /app/mirror.json \
    && echo "[mirror] 首次同步完成" \
    || echo "[mirror] 首次同步失败（不影响后续重试）"

while true; do
    sleep "$INTERVAL"
    echo "[mirror] 定时同步开始"
    python3 -u /app/mirror.py --config /app/mirror.json \
        && echo "[mirror] 定时同步完成" \
        || echo "[mirror] 定时同步失败，下一轮继续"
done
EOF
chmod +x entrypoint.sh

cat > nginx.conf <<'EOF'
server {
    listen 80;
    server_name _;
    root /data;

    # 安装包：文件名带内容特征，可长期强缓存
    location /files/ {
        add_header Cache-Control "public, max-age=31536000, immutable";
        add_header Access-Control-Allow-Origin "*";
        # Range 支持，断点续传和大文件必备
        chunked_transfer_encoding on;
    }

    # manifest：必须短缓存，否则前端读到旧版本列表
    location = /manifest.json {
        add_header Cache-Control "public, max-age=60";
        add_header Access-Control-Allow-Origin "*";
        try_files /manifest.json =404;
    }

    # 下载中的临时文件绝不外泄
    location ~ \.part$ {
        deny all;
        return 404;
    }

    # 健康检查
    location = /healthz {
        access_log off;
        default_type application/json;
        return 200 '{"ok":true}';
    }

    server_tokens off;
    sendfile on;
    tcp_nopush on;
}
EOF

cat > docker-compose.yml <<EOF
services:
  mirror:
    build: .
    image: dlmirror:latest
    container_name: dlmirror-sync
    restart: unless-stopped
    environment:
      MIRROR_INTERVAL: "${INTERVAL}"
    volumes:
      - dl-data:/data
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  web:
    image: nginx:alpine
    container_name: dlmirror-web
    restart: unless-stopped
    ports:
      - "${PORT}:80"
    volumes:
      - dl-data:/data:ro
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      - mirror
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  dl-data:
EOF

say "文件就绪"
ls -1

# ---------- 2. 启动 ----------
say "构建并启动容器"
$DC -p "$PROJECT" up -d --build

# ---------- 3. 等首次同步 ----------
say "等待首次同步（最多 120 秒，取决于安装包大小和数量）"
ok=""
for i in $(seq 1 24); do
    if [ -f /var/lib/docker/volumes/${PROJECT}_dl-data/_data/manifest.json ] 2>/dev/null \
       || docker exec dlmirror-web test -f /data/manifest.json 2>/dev/null; then
        ok="yes"
        break
    fi
    printf '.'
    sleep 5
done
echo

if [ -z "$ok" ]; then
    warn "120 秒内没看到 manifest.json，可能是下载量大。查看进度："
    echo "  $DC -p $PROJECT logs -f mirror"
else
    say "同步完成"
fi

# ---------- 4. 验证 ----------
say "本地验证"
docker exec dlmirror-web sh -c 'wget -qO- http://127.0.0.1/manifest.json 2>/dev/null | head -25' \
    || curl -fsS -m 5 http://127.0.0.1:${PORT}/manifest.json | head -25 \
    || warn "容器内取 manifest 失败，看日志：$DC -p $PROJECT logs mirror"

cat <<EOF

$(say "部署完成")

  下载站地址   http://${HOST}:${PORT}/manifest.json
  安装包目录   http://${HOST}:${PORT}/files/<版本>/<平台>/<文件名>
  安装位置     ${INSTALL_DIR}
  同步间隔     ${INTERVAL} 秒

  常用命令：
    看同步日志    $DC -p ${PROJECT} logs -f mirror
    手动同步一次  $DC -p ${PROJECT} exec mirror python3 /app/mirror.py --config /app/mirror.json
    重启          $DC -p ${PROJECT} restart
    停止          $DC -p ${PROJECT} stop
    完全卸载      $DC -p ${PROJECT} down -v && rm -rf ${INSTALL_DIR}

  接下来：
    1. 云厂商安全组放行 TCP ${PORT} 入方向（和 40033 一样加一条）
    2. 本机防火墙：firewall-cmd --permanent --add-port=${PORT}/tcp && firewall-cmd --reload
    3. 外网验证：curl -I http://${HOST}:${PORT}/manifest.json
    4. 把 Cloudflare Worker 的 manifestUrl 改成：
       http://${HOST}:${PORT}/manifest.json

EOF
