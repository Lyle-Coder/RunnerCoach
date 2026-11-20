#!/usr/bin/env bash
# 在服务器上装「组赛季结算预热」的 systemd timer。
# 用法：sudo bash install-tick.sh
#
# 为什么需要它
# ------------
# 结算本身有懒触发（用户打开组页时 ensure 会补），所以**没有这个 timer
# 也不会算错**。区别在于：
#   - 没人打开的组，赛季会一直停在上一幕，直到有人进来
#   - 周一第一个打开的人要承担一次结算耗时
#   - 赛季结束的翻篇也一样，得等有人来看
# 每 10 分钟跑一次，且是幂等的（条件 UPDATE 抢锁），
# 所以「跑多了」不会有副作用，只会让状态更早对上。
set -euo pipefail

APP=/opt/runnercoach

[ -f "$APP/tick.py" ] || { echo "缺少 $APP/tick.py"; exit 1; }

cat > /etc/systemd/system/runnercoach-tick.service <<'EOF'
[Unit]
Description=RunnerCoach 组赛季结算预热（幂等，只补该结算的组）
After=runnercoach.service

[Service]
Type=oneshot
User=runnercoach
Group=runnercoach
WorkingDirectory=/opt/runnercoach
EnvironmentFile=/etc/runnercoach/env
ExecStart=/usr/bin/python3 /opt/runnercoach/tick.py --quiet
Nice=10
IOSchedulingClass=idle
EOF

cat > /etc/systemd/system/runnercoach-tick.timer <<'EOF'
[Unit]
Description=每 10 分钟兜一次赛季结算

[Timer]
OnCalendar=*:0/10
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable runnercoach-tick.timer >/dev/null 2>&1
systemctl start runnercoach-tick.timer

echo "== 立刻跑一次（不写 --quiet，看清做了什么）"
sudo -u runnercoach /usr/bin/python3 "$APP/tick.py" --dry-run 2>&1 | tail -5 || true
echo
echo "== 定时器状态"
systemctl list-timers runnercoach-tick --all --no-pager | head -3
