#!/bin/sh
# 上线自检：从开发机打一遍服务器，只看「能不能安全地被手机连上」。
#
#   sh deploy/smoke.sh 1.2.3.4                 # 端口默认 8443
#   sh deploy/smoke.sh 1.2.3.4 8443 deploy/certs/cert.pem
#
# 给了证书文件就真校验（推荐）；没给就用 -k，但会明确提示「这次没验证书」。
# 任何一项不过就以非 0 退出——方便直接串进发版脚本。
set -u

IP="${1:?用法: sh deploy/smoke.sh <IP> [端口] [cert.pem]}"
PORT="${2:-8443}"
CA="${3:-}"

if [ -n "$CA" ]; then CURL_TLS="--cacert $CA"; VERIFIED=1
else CURL_TLS="-k"; VERIFIED=0; fi

BASE="https://$IP:$PORT"
OK=0; BAD=0
pass() { OK=$((OK+1)); echo "  [ok] $1"; }
fail() { BAD=$((BAD+1)); echo "  [×]  $1  ${2:-}"; }

echo "目标 $BASE"
[ "$VERIFIED" = "1" ] && echo "证书 由 $CA 校验" || echo "证书 **未校验**（没给 cert.pem，仅看连通性）"
echo

echo "1. 证书"
SAN="$(echo | openssl s_client -connect "$IP:$PORT" -servername "$IP" 2>/dev/null |
       openssl x509 -noout -ext subjectAltName 2>/dev/null | tr -d ' ')"
case "$SAN" in
  *"IPAddress:$IP"*) pass "证书 SAN 里有 $IP" ;;
  *) fail "证书 SAN 里没有 $IP" "$SAN" ;;
esac
END="$(echo | openssl s_client -connect "$IP:$PORT" 2>/dev/null |
       openssl x509 -noout -enddate 2>/dev/null)"
[ -n "$END" ] && pass "证书有效期 $END" || fail "取不到证书有效期"

echo "2. 明文 HTTP 必须打不通"
# 同一个端口上 plain HTTP 应该被 TLS 层直接断掉；能拿到 200 说明 TLS 没生效
CODE="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://$IP:$PORT/health" 2>/dev/null)"
[ "$CODE" = "000" ] && pass "明文请求被拒" || fail "明文还能通（HTTP $CODE）" "TLS 没生效"

echo "3. /health"
H="$(curl -sS -m 8 $CURL_TLS "$BASE/health" 2>/dev/null)"
echo "$H" | grep -q '"ok": *true' && pass "服务在线" || fail "服务不健康" "$H"
echo "$H" | grep -q '"tls": *true' && pass "报的是 HTTPS" || fail "health 没报 tls"
if echo "$H" | grep -q '"dev_echo": *true'; then
  fail "验证码仍会明文回显" "任何人可登录任意账号，先配 SMTP"
else
  pass "验证码不回显"
fi
MODEL="$(echo "$H" | sed -n 's/.*"model": *"\([^"]*\)".*/\1/p')"
[ -n "$MODEL" ] && echo "       模型 $MODEL"
# 平台额度/代理与支付：资质没办之前应该是关的。开着不拦，但要提醒——
# 无资质提供生成式 AI 服务 / 收费，都是实质性的合规风险。
echo "$H" | grep -q '"ai_proxy": *true' && \
  echo "       ！平台代理开着（无资质时不该开，检查 AI_PROXY）"
echo "$H" | grep -q '"pay": *true' && \
  echo "       ！支付通道开着（无资质时不该开，检查 PAY_PROVIDER）"

echo "4. 未授权必须被拒"
C="$(curl -s -o /dev/null -m 8 $CURL_TLS -w '%{http_code}' "$BASE/v1/me" 2>/dev/null)"
[ "$C" = "401" ] && pass "/v1/me 无令牌 401" || fail "/v1/me 无令牌返回 $C" "应为 401"
C="$(curl -s -o /dev/null -m 8 $CURL_TLS -w '%{http_code}' "$BASE/v1/group/1/board" 2>/dev/null)"
[ "$C" = "401" ] && pass "小组接口无令牌 401" || fail "小组接口无令牌返回 $C" "应为 401"
C="$(curl -s -o /dev/null -m 8 $CURL_TLS -w '%{http_code}' "$BASE/admin/export" 2>/dev/null)"
[ "$C" = "403" ] && pass "管理接口无令牌 403" || fail "管理接口无令牌返回 $C" "应为 403"

echo "5. 客户端配置下发"
CC="$(curl -sS -m 8 $CURL_TLS "$BASE/v1/client/config" 2>/dev/null)"
echo "$CC" | grep -q '"ok": *true' && pass "config 可读" || fail "config 读不到" "$CC"

echo "6. 安装包分发"
AL="$(curl -sS -m 8 $CURL_TLS "$BASE/v1/app/latest" 2>/dev/null)"
if echo "$AL" | grep -q '"ok": *true'; then
  V="$(echo "$AL" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')"
  U="$(echo "$AL" | sed -n 's/.*"url": *"\([^"]*\)".*/\1/p')"
  pass "已发布 $V"
  AHOST="$(echo "$U" | sed -e 's|^http://||' -e 's|/.*||')"
  if [ -n "$AHOST" ]; then
    AC="$(curl -s -o /dev/null -m 8 -w '%{http_code}' -I "$U" 2>/dev/null)"
    [ "$AC" = "200" ] && pass "安装包可下载（$AHOST）" || \
      fail "安装包下载返回 $AC" "检查防火墙是否放行 8088"
  fi
elif echo "$AL" | grep -q '还没有发布过'; then
  echo "       还没发布过安装包（tools/publish_apk.py 传一次就有）"
else
  fail "版本接口异常" "$AL"
fi

echo
echo "通过 $OK 项，失败 $BAD 项"
[ "$BAD" = "0" ] || exit 1
if [ "$VERIFIED" = "0" ]; then
  echo "提醒：这次没验证书。发版前用 deploy/certs/cert.pem 再跑一次。"
fi
