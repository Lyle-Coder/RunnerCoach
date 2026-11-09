#!/usr/bin/env bash
# 华为运动健康抓包环境一键搭建（MuMu 模拟器 + mitmproxy）。
#
# 背景：docs/华为体重自动同步调研.md —— 协议事实只能从真实流量里拿，
# 这是开发期取证工具，不是产品流程。
#
# 前提：
#   • MuMu 12 已启动（adb 端口 16384；多开实例依次 +16）
#   • .tools/mitm-venv 已装好 mitmproxy（见 README 协作约定）
#
# 做了什么：
#   1. adb connect + adb root（MuMu 默认允许）
#   2. adb reverse：模拟器里 127.0.0.1:8080 → 本机 8080，不依赖局域网 IP
#   3. mitmproxy CA 按 subject_hash_old 命名，经 tmpfs 覆盖装进系统证书目录
#      （/system 只读，remount 被拒；tmpfs 覆盖是 root 下的标准做法）
#   4. 全局代理指向 127.0.0.1:8080
#
# 注意：tmpfs 挂载与代理设置在模拟器重启后失效，重跑本脚本即可（幂等）。
# 抓完要恢复直连：adb shell settings put global http_proxy :0
set -e

DEV="${DEV:-127.0.0.1:16384}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_W="$(cygpath -m "$ROOT" 2>/dev/null || echo "$ROOT")"
MITMDUMP="$ROOT/.tools/mitm-venv/Scripts/mitmdump.exe"
CA_DIR="$ROOT/.tools/mitm-ca"

[ -x "$MITMDUMP" ] || { echo "× 没找到 $MITMDUMP（先装 mitmproxy）"; exit 2; }

echo "→ 连接 $DEV"
adb connect "$DEV" || { echo "× 连不上，确认 MuMu 已启动"; exit 2; }
adb -s "$DEV" root >/dev/null 2>&1 || true
sleep 1
adb connect "$DEV" >/dev/null        # root 会重启 adbd，重连一次
adb -s "$DEV" reverse tcp:8080 tcp:8080
echo "  reverse: 模拟器 127.0.0.1:8080 → 本机 8080"

# 首次运行时生成 CA
if [ ! -f "$CA_DIR/mitmproxy-ca-cert.pem" ]; then
    echo "→ 生成 mitmproxy CA"
    timeout 4 "$MITMDUMP" --set "confdir=$ROOT_W/.tools/mitm-ca" -q >/dev/null 2>&1 || true
fi
HASH="$(openssl x509 -inform PEM -subject_hash_old -in "$CA_DIR/mitmproxy-ca-cert.pem" -noout)"
echo "→ 安装 CA（hash=$HASH）到系统证书目录"
adb -s "$DEV" shell "mkdir -p /data/local/tmp/cacerts"
# 注意：MSYS_NO_PATHCONV=1 会连本地路径一起跳过转换，所以本地路径必须先用 cygpath -w 转成 Windows 形式
CA_LOCAL="$(cygpath -w "$CA_DIR/$HASH.0" 2>/dev/null || echo "$CA_DIR/$HASH.0")"
MSYS_NO_PATHCONV=1 adb -s "$DEV" push "$CA_LOCAL" /data/local/tmp/cacerts/"$HASH.0" >/dev/null

# /system 只读：先把原证书备份到 /data，再 tmpfs 覆盖，最后合并回来
adb -s "$DEV" shell "
  [ -d /data/local/tmp/cacerts ] || mkdir -p /data/local/tmp/cacerts
  grep -q ' /system/etc/security/cacerts ' /proc/mounts || {
    cp /system/etc/security/cacerts/* /data/local/tmp/cacerts/ 2>/dev/null
    mount -t tmpfs tmpfs /system/etc/security/cacerts
  }
  cp /data/local/tmp/cacerts/* /system/etc/security/cacerts/
  chmod 644 /system/etc/security/cacerts/*
  chmod 755 /system/etc/security/cacerts
  ls /system/etc/security/cacerts/$HASH.0 >/dev/null && echo '  CA 已在系统信任列表'
"

adb -s "$DEV" shell "settings put global http_proxy 127.0.0.1:8080"
echo "→ 全局代理已设为 127.0.0.1:8080"

cat <<'TIP'

环境就绪。接下来：
  1. 在 MuMu 里装「华为运动健康」（应用中心搜，或官网 APK 直装；缺组件就补装 HMS Core）
  2. 登录华为账号，进「体重」页下拉刷新一次
  3. 抓包服务没起的话：
       .tools/mitm-venv/Scripts/mitmdump.exe \
         --listen-host 127.0.0.1 --listen-port 8080 \
         --set confdir=<项目>/.tools/mitm-ca \
         --set hardump=<项目>/output/hw_capture.har \
         -w <项目>/output/hw_flows.mitm
  4. 分析：python cli.py hw probe --capture output/hw_capture.har

收尾（恢复模拟器直连）：
  adb shell settings put global http_proxy :0
TIP
