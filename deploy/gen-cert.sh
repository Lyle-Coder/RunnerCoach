#!/bin/sh
# 生成自签证书（带 IP SAN）。没有域名时用它跑 HTTPS。
#
#   ./gen-cert.sh 1.2.3.4            # 写 certs/cert.pem + certs/key.pem
#   ./gen-cert.sh 1.2.3.4 /opt/x    # 指定输出目录
#
# 为什么是 IP SAN：TLS 校验的是「证书里的名字」和「你连的地址」是否一致。
# 没有域名，就得把 IP 写进 subjectAltName，否则浏览器/客户端一律报
# 证书不匹配——不是"不安全"，是"对不上号"。
#
# 有效期 10 年：这是内测期的自签根，不该比服务器寿命短。换证要重发 APK
# （App 里内置的就是这一张），所以别设一年然后忘了续。
set -eu

IP="${1:?用法: $0 <服务器IP> [输出目录]}"
OUT="${2:-$(cd "$(dirname "$0")" && pwd)/certs}"

case "$IP" in
  *[!0-9.]*) echo "× '$IP' 不是 IPv4 地址"; exit 1 ;;
esac

mkdir -p "$OUT"
chmod 700 "$OUT"

if [ -f "$OUT/cert.pem" ]; then
  echo "已存在 $OUT/cert.pem，不覆盖。要重签先把它删掉。"
  exit 0
fi

# SAN 同时写 localhost/127.0.0.1：在服务器本机 curl 自检时不用 -k。
# 传进来的就是 127.0.0.1 时别写两遍（重复项会有序校验器不认的老客户端）。
if [ "$IP" = "127.0.0.1" ]; then
  SAN="IP:127.0.0.1,DNS:localhost"
else
  SAN="IP:${IP},IP:127.0.0.1,DNS:localhost"
fi
openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -days 3650 \
  -keyout "$OUT/key.pem" -out "$OUT/cert.pem" \
  -subj "/CN=RunnerCoach Gateway ($IP)" \
  -addext "subjectAltName=${SAN}" \
  -addext "keyUsage=digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" 2>/dev/null

chmod 600 "$OUT/key.pem"
chmod 644 "$OUT/cert.pem"

echo "√ 证书 $OUT/cert.pem"
echo "  SHA256 指纹（装 App 前核对用）："
openssl x509 -in "$OUT/cert.pem" -noout -fingerprint -sha256 | sed 's/^/    /'
echo
echo "下一步：把 cert.pem 拷到开发机，执行"
echo "  python tools/set_server.py https://${IP}:8443 --cert cert.pem"
