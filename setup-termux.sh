#!/usr/bin/env bash
# Termux 一次性安装：装 Python、可选 ffmpeg / termux-api，再生成桌面快捷方式。
#
# 用法（在 Termux 里，进到项目目录后）：
#   bash setup-termux.sh
#
# 它只做「装依赖 + 加执行权限 + 写快捷方式」，不碰你的数据，也不需要网络登录。
set -u
cd "$(dirname "$0")" || exit 1

case "${PREFIX:-}" in
  *com.termux*) ;;
  *)
    echo "× 这个脚本只在 Termux 里用（没检测到 \$PREFIX/com.termux）"
    echo "  普通 Linux 直接：chmod +x run.sh daily.sh"
    exit 1
    ;;
esac

echo "── 1/3  装 Python（必需）──"
pkg install -y python || { echo "× Python 装失败，先跑 pkg update"; exit 2; }

echo
echo "── 2/3  装可选组件 ──"
if command -v ffmpeg >/dev/null 2>&1; then
  echo "  ffmpeg 已就位"
else
  pkg install -y ffmpeg || echo "  ! ffmpeg 装失败：只是不能导 MP4，网页回放不受影响"
fi
if command -v termux-wake-lock >/dev/null 2>&1; then
  echo "  termux-api 已就位"
else
  # termux-api 的 pkg 只是命令行壳，真正生效要装同名 App，这里提示一句就好
  pkg install -y termux-api 2>/dev/null \
    && echo "  termux-api 已装（还需在应用商店装 Termux:API 才生效）" \
    || echo "  ! termux-api 跳过：不影响同步，只影响唤醒锁与定时任务"
fi

chmod +x run.sh daily.sh 2>/dev/null || true

# 程序里的提示都是照 Windows 写的（"先运行 run.bat sync"）。加两个别名，
# 这些提示在 Termux 里也就照着敲得动，不用在脑子里做替换。
if ! grep -q "alias run.bat" "$HOME/.bashrc" 2>/dev/null; then
  cat >> "$HOME/.bashrc" <<EOF

# garmin-coach：让提示里的 run.bat / daily.bat 直接可用
alias run.bat='$(pwd)/run.sh'
alias daily.bat='$(pwd)/daily.sh'
EOF
  echo "  已写别名到 ~/.bashrc（重开一个会话生效）"
fi

echo
echo "── 3/3  桌面快捷方式（Termux:Widget）──"
mkdir -p "$HOME/.shortcuts"
cat > "$HOME/.shortcuts/每日同步.sh" <<EOF
#!$PREFIX/bin/bash
cd "$(pwd)" && ./daily.sh
EOF
cat > "$HOME/.shortcuts/手机看板.sh" <<EOF
#!$PREFIX/bin/bash
cd "$(pwd)" && ./run.sh serve --port 8765
EOF
chmod +x "$HOME/.shortcuts/"*.sh 2>/dev/null || true
echo "  已写入 ~/.shortcuts/（装 Termux:Widget 后长按桌面 → 小部件 → Termux 即可添加）"

cat <<'TIP'

两条安卓特有的设置，不做会「跑一半被系统杀掉」：
  1) 设置 → 应用 → Termux → 电池 → 选「不受限制」
  2) 通知栏 Termux 的常驻通知别划掉（那是它活着的意思）

下一步：
  ./run.sh doctor            体检
  ./run.sh login             登录佳明（支持邮箱验证码）
  ./run.sh sync --days 30    拉数据
  ./run.sh report            出看板
  ./run.sh serve             然后手机浏览器打开 http://127.0.0.1:8765
TIP
