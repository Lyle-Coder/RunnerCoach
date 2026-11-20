#!/usr/bin/env bash
# 装每日备份：systemd service + timer，每天 03:20 跑一次 deploy/backup.py。
#
#   sudo bash deploy/install-backup.sh
#   systemctl list-timers runnercoach-backup.timer
#   journalctl -u runnercoach-backup -n 20
#
# 也可以手动跑一次看结果：sudo /usr/bin/python3 /opt/runnercoach/backup.py
set -euo pipefail

ROOT="${1:-/opt/runnercoach}"
SVC=runnercoach-backup

echo "→ 复制备份脚本到 $ROOT"
install -m 755 "$(dirname "$0")/backup.py" "$ROOT/backup.py"

echo "→ 写 systemd unit"
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=RunnerCoach SQLite daily backup
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $ROOT/backup.py --root $ROOT --keep 14
Nice=10
IOSchedulingClass=idle
EOF

cat > "/etc/systemd/system/$SVC.timer" <<EOF
[Unit]
Description=RunnerCoach daily backup at 03:20

[Timer]
OnCalendar=*-*-* 03:20:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "$SVC.timer"
echo "→ 装好了，先手动跑一次："
systemctl start "$SVC.service" || true
systemctl --no-pager status "$SVC.service" | tail -6 || true
echo
systemctl list-timers "$SVC.timer" --no-pager || true
