#!/bin/sh
# RunnerCoach 服务端安装（无域名：自签证书 + IP 直连）
#
# 在服务器上、仓库根目录执行：
#   sudo sh deploy/install.sh 1.2.3.4
#   sudo sh deploy/install.sh 1.2.3.4 8443
#
# 它做的事：建系统用户 → 拷贝程序与内容 → 生成证书 → 写环境变量模板
#          → 装 systemd 服务 → 打印还缺什么。
# 它**不做**的事：填密钥（那是你的）、开防火墙（打印命令，不替你改 iptables）。
#
# 幂等：重复跑不会覆盖已有的证书和 /etc/runnercoach/env。
set -eu

IP="${1:?用法: sudo sh deploy/install.sh <服务器IP> [端口]}"
PORT="${2:-8443}"

APP=/opt/runnercoach
ETC=/etc/runnercoach
SVC=runnercoach
PY="$(command -v python3 || true)"

[ "$(id -u)" = "0" ] || { echo "× 需要 root：sudo sh deploy/install.sh $IP"; exit 1; }
[ -n "$PY" ] || { echo "× 没找到 python3。先 apt install -y python3"; exit 1; }
command -v openssl >/dev/null || { echo "× 没找到 openssl。先 apt install -y openssl"; exit 1; }
[ -f server/app.py ] || { echo "× 请在仓库根目录执行（找不到 server/app.py）"; exit 1; }
[ -f deploy/gen-cert.sh ] || { echo "× 缺 deploy/gen-cert.sh"; exit 1; }

echo "→ 用户与目录"
id "$SVC" >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin "$SVC"
mkdir -p "$APP" "$APP/data" "$APP/certs" "$APP/site" "$ETC"

echo "→ 拷贝程序"
for f in app.py groups.py expedition.py tick.py; do
  [ -f "server/$f" ] && cp -f "server/$f" "$APP/$f"
done
if [ -d server/content ]; then
  cp -rf server/content "$APP/"
fi
if [ -d site ]; then
  cp -rf site/. "$APP/site/" 2>/dev/null || true
fi

# 内容池走 .gitignore，不在版本库里。忘了带上来，服务能起但远征一进就 503。
if [ ! -f "$APP/content/expedition_events.json" ]; then
  echo "  ! 缺 content/expedition_events.json（内容池不进仓库，必须单独 scp）"
fi

echo "→ 证书"
if [ -f "$APP/certs/cert.pem" ]; then
  echo "  已存在，不覆盖"
else
  sh deploy/gen-cert.sh "$IP" "$APP/certs"
fi

echo "→ 环境变量"
if [ -f "$ETC/env" ]; then
  echo "  已存在，不覆盖（改完 systemctl restart $SVC）"
else
  TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
  sed -e "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=$TOKEN|" \
      -e "s|^RC_TLS_CERT=.*|RC_TLS_CERT=$APP/certs/cert.pem|" \
      -e "s|^RC_TLS_KEY=.*|RC_TLS_KEY=$APP/certs/key.pem|" \
      -e "s|^DB_PATH=.*|DB_PATH=$APP/data/data.db|" \
      -e "s|^RC_SITE_DIR=.*|RC_SITE_DIR=$APP/site|" \
      -e "s|^PORT=.*|PORT=$PORT|" \
      -e "s|^RC_PUBLIC_BASE=.*|RC_PUBLIC_BASE=https://$IP:$PORT|" \
      deploy/env.example > "$ETC/env"
  chmod 600 "$ETC/env"
  echo "  已生成 $ETC/env（ADMIN_TOKEN 已随机填好）"
fi

echo "→ systemd"
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=RunnerCoach Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC
Group=$SVC
WorkingDirectory=$APP
EnvironmentFile=$ETC/env
ExecStart=$PY $APP/app.py
Restart=always
RestartSec=3
LimitNOFILE=8192
# 只让这个进程写自己的数据目录，别的一律只读
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=$APP/data

[Install]
WantedBy=multi-user.target
EOF

chown -R "$SVC:$SVC" "$APP"
chmod 700 "$APP/certs"
chmod 600 "$APP/certs/key.pem" 2>/dev/null || true
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1 || true

echo
# 模板里留的是占位值（"你的发件邮箱@qq.com"），既不是空也不算填好——
# 空密码和占位值都视为「还没配」，否则会启动一个马上被护栏拒掉的服务。
if grep -qE '^SMTP_PASS=$|^SMTP_USER=你的' "$ETC/env"; then
  echo "！先别急，还差登录通道："
  echo "   编辑 $ETC/env，把 SMTP_USER / SMTP_PASS 换成你的 QQ 邮箱与授权码"
  echo "   然后：systemctl start $SVC && journalctl -u $SVC -n 30 --no-pager"
  echo "   （不填的话服务会拒绝启动——验证码明文回显等于谁都能登录任何账号）"
else
  systemctl restart "$SVC" || true
  sleep 1
  echo "→ 本机自检"
  curl -sS --cacert "$APP/certs/cert.pem" "https://127.0.0.1:$PORT/health" || \
    echo "  × 探不通，看 journalctl -u $SVC -n 50 --no-pager"
fi

echo
echo "防火墙（三选一，按你机器上装的那个）："
echo "  ufw:       ufw allow $PORT/tcp && ufw reload"
echo "  firewalld: firewall-cmd --add-port=$PORT/tcp --permanent && firewall-cmd --reload"
echo "  云控制台:   安全组放行 TCP $PORT"
echo
echo "装好之后在开发机上跑："
echo "  sh deploy/smoke.sh $IP $PORT"
