/**
 * RunnerCoach 佳明登录中转（Cloudflare Worker）
 * ---------------------------------------------------------------
 * 为什么需要它
 *   2026-11 佳明把国区登录迁到 SSO Portal 后，机房 IP（阿里云等）访问
 *   sso.garmin.cn/portal/* 会被 Cloudflare 挑战（403 cf-mitigated: challenge），
 *   服务器没法直接替用户登录。Worker 本身跑在 Cloudflare 里，出口能过校验，
 *   所以由它代跑一遍登录，只把 Cookie 交回我们的服务器。
 *
 * 部署
 *   1. Cloudflare 控制台 → Workers & Pages → Create → Worker，粘贴本文件；
 *   2. Settings → Variables → 添加 `RELAY_TOKEN`（随机长串，务必设）；
 *      可选 `ALLOW_ORIGIN` 一般不用；
 *   3. 记下 worker 地址，例如 https://rc-login.<你>.workers.dev
 *   4. 服务器 /etc/runnercoach/env 加一行：
 *        RC_LOGIN_RELAY=https://rc-login.<你>.workers.dev
 *      然后 systemctl restart runnercoach。
 *
 * 接口
 *   POST <worker>
 *     X-Relay-Token: <RELAY_TOKEN>
 *     {"username":"...","password":"..."}
 *   -> {"ok":true,"cookies":[{"name":"JWT_WEB","value":"...","domain":".connect.garmin.cn","path":"/"}]}
 *   -> {"ok":false,"error":"..."}
 *
 * 注意：密码只在这一个请求里穿过，不落盘、不写日志。
 */

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const SERVICE = "https://connect.garmin.cn/app";
const QS = "clientId=GarminConnect&service=" + encodeURIComponent(SERVICE);
const SSO = "https://sso.garmin.cn";

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });

function jarAdd(jar, resp) {
  const list =
    typeof resp.headers.getSetCookie === "function"
      ? resp.headers.getSetCookie()
      : [];
  for (const line of list) {
    const [pair, ...attrs] = line.split(";");
    const idx = pair.indexOf("=");
    if (idx < 0) continue;
    const name = pair.slice(0, idx).trim();
    const value = pair.slice(idx + 1).trim();
    let domain = "";
    let path = "/";
    for (const a of attrs) {
      const [k, v] = a.split("=");
      const key = k.trim().toLowerCase();
      if (key === "domain" && v) domain = v.trim();
      if (key === "path" && v) path = v.trim();
    }
    if (!name) continue;
    const prev = jar.find((c) => c.name === name && c.domain === domain);
    if (prev) {
      prev.value = value;
    } else {
      jar.push({ name, value, domain: domain || ".garmin.cn", path });
    }
  }
}

const cookieHeader = (jar) => jar.map((c) => `${c.name}=${c.value}`).join("; ");

async function step(url, jar, init = {}) {
  const headers = new Headers(init.headers || {});
  headers.set("User-Agent", UA);
  headers.set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8");
  if (jar.length) headers.set("Cookie", cookieHeader(jar));
  const resp = await fetch(url, { ...init, headers, redirect: "follow" });
  jarAdd(jar, resp);
  return resp;
}

export default {
  async fetch(req, env) {
    if (req.method !== "POST") return json({ ok: false, error: "只接受 POST" }, 405);
    if (!env.RELAY_TOKEN) return json({ ok: false, error: "服务端没配 RELAY_TOKEN" }, 500);
    if ((req.headers.get("X-Relay-Token") || "") !== env.RELAY_TOKEN) {
      return json({ ok: false, error: "令牌不对" }, 401);
    }
    let body;
    try {
      body = await req.json();
    } catch {
      return json({ ok: false, error: "请求不是 JSON" }, 400);
    }
    const username = String(body.username || "");
    const password = String(body.password || "");
    if (!username || !password) return json({ ok: false, error: "缺账号或密码" }, 400);

    const jar = [];
    try {
      // 1) 预热登录页：Cloudflare 的「验证码已评估」状态挂在这步的 Cookie 上
      await step(`${SSO}/portal/sso/zh-CN/sign-in?${QS}`, jar, {
        headers: { Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" },
      });

      // 2) 提交账号密码
      const loginResp = await step(`${SSO}/portal/api/login?${QS}`, jar, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json, text/plain, */*",
          Origin: SSO,
          Referer: `${SSO}/portal/sso/zh-CN/sign-in`,
        },
        body: JSON.stringify({
          username,
          password,
          rememberMe: true,
          captchaToken: null,
        }),
      });
      const text = await loginResp.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        return json({
          ok: false,
          error: `登录接口返回 ${loginResp.status}：${text.slice(0, 120)}`,
        });
      }
      const type = (data.responseStatus || {}).type || "";
      if (!data.serviceTicketId) {
        if (data.customerMfaInfo) {
          return json({ ok: false, error: "MFA_REQUIRED", need_mfa: true });
        }
        return json({
          ok: false,
          error: `${type || "登录失败"} ${(data.responseStatus || {}).message || ""}`.trim(),
        });
      }

      // 3) 用票据换会话 Cookie（JWT_WEB）
      const svc = data.serviceURL || SERVICE;
      const url = svc.startsWith("https://connect.garmin.cn/")
        ? svc
        : SERVICE;
      await step(`${url}${url.includes("?") ? "&" : "?"}ticket=${encodeURIComponent(data.serviceTicketId)}`,
        jar,
        { headers: { Accept: "text/html,application/xhtml+xml,*/*;q=0.8" } });

      const names = new Set(jar.map((c) => c.name));
      if (!names.has("JWT_WEB")) {
        return json({ ok: false, error: "票据没有换出 JWT_WEB" });
      }
      // 只回传佳明相关的 Cookie
      const keep = jar.filter((c) => /garmin|connect/i.test(c.domain) || c.domain.endsWith(".garmin.cn"));
      return json({ ok: true, cookies: keep.length ? keep : jar });
    } catch (e) {
      return json({ ok: false, error: `中转异常：${e && e.message ? e.message : e}` });
    }
  },
};
