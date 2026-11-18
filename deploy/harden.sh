#!/bin/sh
# 关掉 SSH 密码登录，只留密钥。
#
#   sudo sh deploy/harden.sh
#
# 为什么单独一个脚本，而不是塞进 install.sh：
# 它**会**让你失去一种进服务器的方式。万一公钥还没装好就先跑了这一条，
# 人就锁在外面了（只剩云控制台的 VNC）。所以做成"你自己确认要，才执行"。
#
# 前置检查：/home/*/.ssh/authorized_keys 或 /root/.ssh/authorized_keys 里
# 至少得有一把公钥，否则直接拒绝——这是最后一道自保。
set -eu

[ "$(id -u)" = "0" ] || { echo "× 需要 root：sudo sh deploy/harden.sh"; exit 1; }

KEYS="$(ls -1 /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys 2>/dev/null || true)"
if [ -z "$KEYS" ]; then
  echo "× 没找到任何 authorized_keys。先装公钥再加固，否则会把自己锁在外面。"
  exit 1
fi

echo "已有公钥："
for k in $KEYS; do
  n="$(grep -cE '^(ssh-|ecdsa-)' "$k" 2>/dev/null || echo 0)"
  echo "  $k  （$n 把）"
done

F=/etc/ssh/sshd_config.d/99-runnercoach.conf

# 放在 sshd_config.d/ 而不是改主配置：发行版升级时不会冲突，
# 想撤销就删这一个文件。
cat > "$F" <<'EOF'
# RunnerCoach 部署加固：只允许公钥登录
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF

if ! sshd -t; then
  echo "！配置校验失败，已移除，未重启"
  rm -f "$F"
  exit 1
fi

systemctl reload ssh 2>/dev/null || systemctl restart ssh
sleep 1

echo
echo "生效值："
sshd -T | grep -Ei "^(passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|permitrootlogin)"
echo
echo "！现在**别关**你正在用的这个窗口。另开一个终端，用密钥登一次："
echo "    ssh -i <你的私钥> <用户>@<IP>"
echo "  确认能进，再关。进不来就在云控制台用 VNC 登录，删掉："
echo "    $F"
echo "  然后 systemctl reload ssh"
