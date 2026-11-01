#!/usr/bin/env bash
# 一键安装：脚本 + systemd 定时任务 + nginx 配置
# 用法：sudo bash install.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/dl-mirror
ETC_DIR=/etc/dl-mirror
DATA_DIR=/var/lib/dl-mirror
RUN_USER="${SUDO_USER:-$(id -un)}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }

# ---------------------------------------------------------------- 依赖
say "检查依赖"
if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 python3，正在安装…"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update && apt-get install -y python3
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3
    else
        echo "无法自动安装 python3，请手动安装后重试"; exit 1
    fi
fi
python3 --version

# ---------------------------------------------------------------- 文件
say "安装文件"
mkdir -p "$APP_DIR" "$ETC_DIR" "$DATA_DIR/files"
install -m 0755 "$SRC_DIR/mirror.py" "$APP_DIR/mirror.py"

if [ ! -f "$ETC_DIR/mirror.json" ]; then
    install -m 0644 "$SRC_DIR/mirror.json" "$ETC_DIR/mirror.json"
    echo "已生成配置 $ETC_DIR/mirror.json（请按需修改 repo 和 publicBaseUrl）"
else
    echo "配置已存在，保留 $ETC_DIR/mirror.json"
fi

# 日志轮转
cat > /etc/logrotate.d/dl-mirror <<'EOF'
/var/lib/dl-mirror/mirror.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# ---------------------------------------------------------------- systemd
say "配置 systemd 服务与定时器"
cat > /etc/systemd/system/dl-mirror.service <<EOF
[Unit]
Description=GitHub Release mirror sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
ExecStart=$APP_DIR/mirror.py --config $ETC_DIR/mirror.json
WorkingDirectory=$APP_DIR
StandardOutput=append:$DATA_DIR/mirror.log
StandardError=append:$DATA_DIR/mirror.log
# 单个大文件下载可放宽超时
TimeoutStartSec=3600
Nice=10
IOSchedulingClass=idle
EOF

cat > /etc/systemd/system/dl-mirror.timer <<'EOF'
[Unit]
Description=Run GitHub Release mirror every 30 minutes

[Timer]
# 开机 2 分钟后先跑一次，之后每 30 分钟一次
OnBootSec=2min
OnUnitActiveSec=30min
# 错开整点，避开 GitHub API 高峰
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now dl-mirror.timer

# ---------------------------------------------------------------- nginx
say "配置 nginx"
if command -v nginx >/dev/null 2>&1; then
    if [ ! -f /etc/nginx/conf.d/download.conf ]; then
        install -m 0644 "$SRC_DIR/nginx.conf" /etc/nginx/conf.d/download.conf
        echo "已写入 /etc/nginx/conf.d/download.conf"
        echo "注意：请把其中的 dl.example.com 改成你的域名，并补上证书路径"
    else
        echo "/etc/nginx/conf.d/download.conf 已存在，未覆盖"
    fi
    if nginx -t; then
        systemctl reload nginx
        echo "nginx 已重载"
    else
        echo "nginx 配置测试未通过，请手动检查 /etc/nginx/conf.d/download.conf"
    fi
else
    echo "未安装 nginx，已跳过。可稍后执行："
    echo "  apt-get install -y nginx && cp $SRC_DIR/nginx.conf /etc/nginx/conf.d/download.conf && nginx -t && systemctl reload nginx"
fi

# ---------------------------------------------------------------- 收尾
say "首次同步（--dry-run 预览）"
python3 "$APP_DIR/mirror.py" --config "$ETC_DIR/mirror.json" --dry-run || true

cat <<EOF

$(say "安装完成")

接下来三件事：

  1. 改仓库地址（如需）
     sudo nano $ETC_DIR/mirror.json
     把 repo 改成 用户名/仓库名，publicBaseUrl 改成你的域名

  2. 真正跑一次
     sudo systemctl start dl-mirror.service
     journalctl -u dl-mirror.service -n 50 --no-pager
     # 或直接看日志：tail -f $DATA_DIR/mirror.log

  3. 验证结果
     curl -s http://127.0.0.1/manifest.json | python3 -m json.tool
     ls -lh $DATA_DIR/files/

  手动同步：  sudo systemctl start dl-mirror.service
  查看状态：  systemctl list-timers dl-mirror.timer
  暂停自动：  sudo systemctl disable --now dl-mirror.timer
  卸载：      sudo systemctl disable --now dl-mirror.timer && sudo rm -rf $APP_DIR $ETC_DIR $DATA_DIR /etc/systemd/system/dl-mirror.{service,timer} /etc/logrotate.d/dl-mirror /etc/nginx/conf.d/download.conf

EOF
