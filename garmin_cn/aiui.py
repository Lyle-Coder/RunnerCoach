# -*- coding: utf-8 -*-
"""AI 教练页面：一个自包含 HTML，通过同源 fetch 调 `/ai/*` 接口。

为什么是网页而不是原生页
------------------------
App 里 WebView 已经加载着 `http://127.0.0.1:<port>/`，同源就能 POST，
不用再过一遍 JS 桥；桌面端 `run.bat serve` 打开的也是同一页。
一套 UI 两端可用，改样式不用重新打包 APK。

三块内容对应三种真实使用场景
----------------------------
- **今日**：早上打开看「今天怎么练」，30 秒读完，不需要打字。
- **体重**：每周看一次趋势，判断减脂速率和训练负荷配不配。
- **对话**：有具体疑问时追问，模型带着今日简报回答。

设计约束：手机竖屏 375px 起可用；不引外部字体和脚本（离线可用）；
所有数字用 tabular-nums，避免跳动。
"""
from __future__ import annotations

import datetime as dt
import html as _h
import json

CSS = """
:root{
  /* 与 ui.py 同一套深空 token：AI 页也走蓝青辉光，不在深色 App 里留一块白 */
  --bg:#070B12; --card:#0F1622; --line:rgba(96,165,250,.16); --line-2:rgba(96,165,250,.09);
  --ink:#E8F0FB; --ink-2:#B7C7DD; --muted:#7C8FA8; --faint:#4E6076;
  --green:#3B82F6; --green-soft:rgba(59,130,246,.14);
  --amber:#F5A524; --amber-soft:rgba(245,165,36,.14);
  --red:#F2555A; --red-soft:rgba(242,85,90,.14);
  --blue:#5EA0FF; --blue-soft:rgba(94,160,255,.14);
  --cyan:#22D3EE;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;color:var(--ink);
  background:
    radial-gradient(900px 520px at 78% -8%, rgba(59,130,246,.17), transparent 62%),
    radial-gradient(680px 420px at 6% 2%, rgba(34,211,238,.10), transparent 60%),
    var(--bg);
  background-attachment:fixed;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.6;font-size:14px;
  padding-bottom:env(safe-area-inset-bottom)}
.wrap{max-width:720px;margin:0 auto;padding:16px 14px 24px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.top h1{margin:0;font-size:18px;font-weight:600;letter-spacing:.2px}
.top .sub{font-size:11.5px;color:var(--muted);margin-top:2px}
.gear{width:34px;height:34px;border-radius:10px;border:1px solid var(--line);background:var(--card);
  display:flex;align-items:center;justify-content:center;font-size:16px;color:var(--ink-2);cursor:pointer}
.tabs{display:flex;gap:6px;background:rgba(9,14,22,.9);border:1px solid rgba(96,165,250,.12);border-radius:12px;padding:4px;margin-bottom:14px}
.tabs button{flex:1;border:0;background:transparent;color:var(--ink-2);font-size:13px;font-weight:500;
  padding:8px 0;border-radius:9px;font-family:inherit;cursor:pointer;transition:.15s}
.tabs button.on{background:var(--card);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.06)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:12px}
.card h3{margin:0 0 10px;font-size:11.5px;font-weight:600;color:var(--muted);letter-spacing:1px}
.head{font-size:21px;font-weight:600;line-height:1.35;letter-spacing:.2px}
.focus{font-size:13.5px;color:var(--ink-2);margin-top:6px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.chip{font-size:11.5px;padding:3px 9px;border-radius:999px;background:var(--green-soft);color:#8CC2FF}
.chip.gray{background:var(--line-2);color:var(--ink-2)}
.chip.amber{background:var(--amber-soft);color:#F5C26B}
ul.list{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:9px}
ul.list li{display:flex;gap:9px;font-size:13.5px;line-height:1.6;color:var(--ink-2)}
ul.list li .ic{flex:0 0 auto;width:17px;height:17px;border-radius:50%;font-size:11px;
  display:flex;align-items:center;justify-content:center;margin-top:2px;font-weight:700}
.ic.yes{background:var(--green-soft);color:var(--green)}
.ic.no{background:var(--line-2);color:var(--muted)}
.ic.warn{background:var(--amber-soft);color:var(--amber)}
.watch{display:flex;gap:9px;background:var(--amber-soft);border-radius:12px;padding:11px 13px;
  font-size:13px;color:#7A5A0E;line-height:1.55}
.why{font-size:13px;color:var(--ink-2);line-height:1.75;white-space:pre-wrap}
.meta{font-size:11px;color:var(--faint);margin-top:12px;display:flex;justify-content:space-between;
  gap:8px;align-items:center;flex-wrap:wrap}
.btn{border:1px solid var(--line);background:var(--card);color:var(--ink-2);border-radius:10px;
  padding:8px 13px;font-size:12.5px;font-family:inherit;cursor:pointer;white-space:nowrap;flex:0 0 auto}
.btn:active{background:rgba(59,130,246,.14)}
.btn.primary{background:linear-gradient(135deg,#3B82F6,#22D3EE);border-color:transparent;color:#04121F;font-weight:650}
.btn.primary:active{background:#2A6FE0}
.btn:disabled{opacity:.5}
.skel{height:14px;border-radius:6px;background:linear-gradient(90deg,#16202F 25%,#22304A 37%,#16202F 63%);
  background-size:400% 100%;animation:sh 1.2s ease infinite;margin-bottom:9px}
@keyframes sh{0%{background-position:100% 50%}100%{background-position:0 50%}}
.empty{text-align:center;padding:22px 10px;color:var(--muted);font-size:13px;line-height:1.7}
.empty b{display:block;color:var(--ink);font-size:14.5px;font-weight:600;margin-bottom:6px}
.err{border-left:3px solid var(--red);background:var(--red-soft);border-radius:10px;padding:12px 14px;
  font-size:13px;color:#8C2F2A;line-height:1.65}
.err b{display:block;margin-bottom:4px}
.det{margin-top:8px;font-size:11.5px;color:var(--faint)}
.det summary{cursor:pointer;min-height:32px;display:flex;align-items:center}
.quote{font-size:12px;color:var(--muted);background:rgba(9,14,22,.9);border-radius:8px;padding:8px 10px;
  margin-top:8px;white-space:pre-wrap;max-height:160px;overflow:auto}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:4px}
.kv div{background:rgba(9,14,22,.9);border-radius:10px;padding:9px 8px;text-align:center}
.kv .v{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.kv .k{font-size:10.5px;color:var(--muted);margin-top:2px}
/* 聊天 */
.chat{display:flex;flex-direction:column;gap:10px;min-height:40vh}
.msg{max-width:86%;padding:10px 13px;border-radius:14px;font-size:13.5px;line-height:1.65;
  white-space:pre-wrap;word-break:break-word}
.msg.u{align-self:flex-end;background:linear-gradient(135deg,#3B82F6,#2A6FE0);color:#F2F8FF;border-bottom-right-radius:5px}
.msg.a{align-self:flex-start;background:var(--card);border:1px solid var(--line);
  border-bottom-left-radius:5px}
.msg.a.think{color:var(--muted);font-style:normal}
.quick{display:flex;gap:7px;flex-wrap:wrap;margin-top:4px}
.quick button{border:1px solid var(--line);background:var(--card);border-radius:999px;
  padding:7px 12px;font-size:12.5px;color:var(--ink-2);font-family:inherit}
.composer{position:sticky;bottom:0;background:linear-gradient(180deg,rgba(7,11,18,0) 0%,var(--bg) 30%);
  padding:10px 0 calc(10px + env(safe-area-inset-bottom))}
.composer .row{display:flex;gap:8px;align-items:flex-end;background:var(--card);border:1px solid var(--line);
  border-radius:14px;padding:7px 7px 7px 12px}
.composer textarea{flex:1;border:0;outline:0;resize:none;font-family:inherit;font-size:14px;
  line-height:1.5;max-height:110px;background:transparent;color:var(--ink);padding:6px 0}
.composer .send{width:34px;height:34px;border-radius:10px;border:0;background:linear-gradient(135deg,#3B82F6,#22D3EE);color:#04121F;
  font-size:15px;flex:0 0 auto;cursor:pointer}
.composer .send:disabled{background:#243246}
/* 设置抽屉 */
.sheet{position:fixed;inset:0;background:rgba(3,6,11,.62);display:none;z-index:40}
.sheet.on{display:block}
.panel{position:absolute;left:0;right:0;bottom:0;max-height:88vh;overflow:auto;background:var(--bg);
  border-radius:20px 20px 0 0;padding:18px 16px calc(24px + env(safe-area-inset-bottom));
  animation:up .22s ease}
@keyframes up{from{transform:translateY(18px);opacity:.6}to{transform:none;opacity:1}}
.panel h2{margin:0 0 4px;font-size:16px;font-weight:600}
.panel .hint{font-size:12px;color:var(--muted);line-height:1.6;margin-bottom:14px}
.seg{display:flex;gap:6px;background:rgba(9,14,22,.9);border:1px solid rgba(96,165,250,.12);border-radius:11px;padding:4px;margin-bottom:14px}
.seg button{flex:1;border:0;background:transparent;padding:8px 0;border-radius:8px;font-size:13px;
  font-family:inherit;color:var(--ink-2);font-weight:500}
.seg button.on{background:var(--card);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.06)}
label.f{display:block;font-size:12px;color:var(--muted);margin:12px 0 5px}
input.f,select.f,textarea.f{width:100%;border:1px solid var(--line);border-radius:10px;padding:10px 12px;
  font-size:13.5px;font-family:inherit;background:var(--card);color:var(--ink);outline:0}
input.f:focus,select.f:focus{border-color:var(--green)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.note{font-size:11.5px;color:var(--faint);line-height:1.65;margin-top:6px}
.act{display:flex;gap:8px;margin-top:16px}
.act .btn{flex:1;text-align:center;padding:11px 0}
.bal{display:flex;align-items:baseline;gap:8px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:12px 14px;margin-top:10px}
.bal .n{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.bal .u{font-size:12px;color:var(--muted)}
details.adv{margin-top:14px;border-top:1px dashed var(--line);padding-top:6px}
details.adv summary{cursor:pointer;color:var(--muted);font-size:12.5px;padding:4px 0}
details.adv[open] summary{margin-bottom:8px}
.step{display:inline-block;min-width:18px;height:18px;line-height:18px;text-align:center;
  border-radius:9px;background:var(--green-soft);color:var(--green);font-size:11px;margin-right:6px}
.spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.4);
  border-top-color:#8CC2FF;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px}
@keyframes sp{to{transform:rotate(360deg)}}
svg.spark{width:100%;height:96px;display:block;margin:2px 0 6px}
.foot{text-align:center;font-size:11px;color:var(--faint);line-height:1.7;margin-top:18px}
"""

BODY = """
<div class="wrap">
  <div class="top">
    <div>
      <h1>AI 教练</h1>
      <div class="sub" id="sub">正在读取状态…</div>
    </div>
    <div class="gear" id="gear" title="模型服务">⚙</div>
  </div>

  <div class="tabs" id="tabs">
    <button data-v="today" class="on">今日</button>
    <button data-v="weight">体重</button>
    <button data-v="chat">对话</button>
  </div>

  <div id="view"></div>
  <div class="foot" id="foot"></div>
</div>

<div class="sheet" id="sheet">
  <div class="panel">
    <h2>模型服务</h2>
    <div class="hint" id="setHint">两种接入方式：填自己的 API Key 直连，或接入平台额度（充值后按量扣）。</div>

    <div class="seg" id="modeSeg">
      <button data-m="own">自配 API</button>
      <button data-m="platform">平台额度</button>
    </div>

    <div id="ownBox">
      <label class="f">供应商</label>
      <select class="f" id="provider"></select>
      <div class="note" id="provNote"></div>

      <label class="f">API 地址</label>
      <input class="f" id="baseUrl" placeholder="https://api.deepseek.com/v1" autocapitalize="off" spellcheck="false">
      <div class="note">留空即用上面预设的地址。</div>

      <label class="f">模型名</label>
      <input class="f" id="model" placeholder="deepseek-chat" autocapitalize="off" spellcheck="false">

      <label class="f">API Key</label>
      <input class="f" id="apiKey" type="password" placeholder="sk-…" autocapitalize="off" spellcheck="false">
      <div class="note" id="keyNote">只存在本机 data/llm_config.json（权限 600），不上传。已有：<span id="keyHint">无</span></div>
    </div>

    <div id="platBox" style="display:none">
      <!-- 状态一：未登录，三步走完 -->
      <div id="platLogin">
        <div class="note" id="platService">官方服务已内置，无需填写地址。</div>

        <label class="f"><span class="step">1</span>手机号 / 邮箱</label>
        <div class="row2">
          <input class="f" id="phoneInput" type="text" placeholder="11 位手机号或邮箱" autocapitalize="off" autocorrect="off" spellcheck="false">
          <button class="btn" id="smsBtn">发验证码</button>
        </div>

        <label class="f"><span class="step">2</span>验证码</label>
        <div class="row2">
          <input class="f" id="smsCode" inputmode="numeric" maxlength="6" placeholder="6 位数字" autocapitalize="off" spellcheck="false">
          <button class="btn primary" id="loginBtn">登录 / 注册</button>
        </div>
        <div class="note" id="smsNote">邮箱登录免费直达；未注册的账号自动注册并赠送试用额度，登录后就能直接问答，不用先充值。</div>
      </div>

      <!-- 状态二：已登录，账户面板 -->
      <div id="platAcct" style="display:none">
        <div class="bal" id="balBox" style="display:none">
          <span class="n" id="balNum">—</span><span class="u" id="balUnit">元</span>
        </div>
        <div class="note" id="acctNote">余额挂在账号上：卸载重装 / 换手机后登录同一账号即可找回。新用户赠送额度已到账，可直接问答。</div>

        <label class="f">充值 · 在线支付</label>
        <div class="row2">
          <button class="btn" data-pay="5">¥ 5</button>
          <button class="btn" data-pay="10">¥ 10</button>
        </div>
        <div class="row2">
          <button class="btn" data-pay="20">¥ 20</button>
          <button class="btn" data-pay="50">¥ 50</button>
        </div>
        <div class="note" id="payNote">在线支付由服务端开通，未开通时点金额会明确提示；期间可先用兑换码充值。</div>

        <label class="f">充值 · 兑换码</label>
        <div class="row2">
          <input class="f" id="codeInput" placeholder="RC-XXXX-XXXX" autocapitalize="characters" spellcheck="false">
          <button class="btn primary" id="redeemBtn">兑换</button>
        </div>
        <div class="note" id="redeemNote">兑换码由服务端生成，一次一用。</div>

        <div class="act">
          <button class="btn" id="balBtn">刷新余额</button>
          <button class="btn" id="logoutBtn">退出登录</button>
        </div>

      </div>

      <details class="adv">
        <summary>高级 · 服务地址与设备令牌（一般不用管）</summary>
        <div class="note"><label><input type="checkbox" id="platCustom" style="vertical-align:middle"> 使用自定义服务地址（自建网关 / 内网直连）</label></div>
        <div id="platCustomBox" style="display:none">
          <input class="f" id="platBase" placeholder="https://你的域名 或 http://内网IP:8300" autocapitalize="off" spellcheck="false">
          <div class="note">可粘贴带 ?sharecode= 的完整分享链接；取消勾选即恢复官方服务。</div>
        </div>
        <input class="f" id="platToken" type="password" placeholder="登录后自动获取，也可手动粘贴" autocapitalize="off" spellcheck="false">
        <div class="note">令牌只是登录态，余额在账号上。换设备登录会自动覆盖。</div>
      </details>
    </div>

    <div class="act" id="mainAct">
      <button class="btn" id="testBtn">测试连接</button>
      <button class="btn primary" id="saveBtn">保存</button>
    </div>
    <div class="note" id="setMsg"></div>
  </div>
</div>
"""

JS = r"""
const $ = s => document.querySelector(s);
const el = (t, c, h) => { const n = document.createElement(t); if (c) n.className = c; if (h != null) n.innerHTML = h; return n; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
/* 切显示状态一律走这里：某个节点哪天被删了，也只是少一处变化，
   不会抛 TypeError 把后面的整个抽屉打开流程打断。 */
const shown = (sel, on) => { const n = $(sel); if (n) n.style.display = on ? '' : 'none'; };

let STATE = { config: null, tab: 'today', busy: false };
let CHAT = [];              // {role, content}
const cache = {};           // 每个 tab 的渲染结果，切回来不用重新请求

async function api(action, body) {
  let r;
  try {
    r = await fetch('/ai/' + action, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    });
  } catch (e) {
    /* 旧版这里直接抛异常，按钮会永远停在「发送中…」且没有任何提示 */
    return {ok: false, kind: 'net',
            error: '连不上本机服务（App 内部桥接未就绪或网络异常）', detail: String(e)};
  }
  let j;
  try { j = await r.json(); }
  catch (e) {
    const denied = r.status === 403;
    j = {ok: false, kind: denied ? 'auth' : 'error',
         error: denied ? '平台访问凭证无效（分享链接不完整），请升级到最新版 App'
                       : '服务返回的不是 JSON（HTTP ' + r.status + '）'};
  }
  if (!j.ok && !j.kind) j.kind = 'error';
  return j;
}

/* 错误要给出下一步动作，而不是只报一句红字 */
function errBox(j, retry) {
  const map = {
    config: ['还没配置模型服务', '点右上角 ⚙ 填 API Key，或切到平台额度。', 'gap'],
    auth:   ['密钥无效', '检查 API Key 是否复制完整、有没有多余空格；平台令牌可能已吊销。', 'gap'],
    quota:  ['额度不足', '上游账户余额用完了。去供应商后台充值，或用兑换码加平台额度。', 'gap'],
    rate:   ['请求太频繁', '等 30 秒再试。连续失败先确认账户额度。', 'retry'],
    net:    ['网络到不了', '检查手机网络；平台额度还需要电脑开机且隧道客户端在运行。', 'retry'],
    data:   ['数据读不到', '先在 App 里同步一次，或确认今天有数据。', 'gap'],
    upstream: ['模型服务报错', '这多半是上游临时故障，稍后重试。', 'retry'],
    pay_off: ['在线支付未开通', '用兑换码充值，或联系管理员开通微信支付。', 'gap'],
    request: ['请求被拒绝', '换一个模型试试，或检查模型名是否写错。', 'retry']
  };
  const m = map[j.kind] || ['出错了', '', 'retry'];
  const d = el('div', 'err');
  d.appendChild(el('b', null, esc(m[0])));
  d.appendChild(el('div', null, esc(j.error || '')));
  if (m[1]) d.appendChild(el('div', 'note', esc(m[1])));
  if (j.detail) {
    /* 原文（"TypeError: Failed to fetch" 之类）收进折叠块：排障要看，
       但默认摊在红卡里只会让人觉得这是个没做完的东西。 */
    const det = el('details', 'det');
    det.appendChild(el('summary', null, '技术细节'));
    det.appendChild(el('div', 'quote', esc(j.detail)));
    d.appendChild(det);
  }
  const bar = el('div', 'act');
  if (m[2] === 'retry' && retry) {
    const b = el('button', 'btn', '重试'); b.onclick = retry; bar.appendChild(b);
  }
  if (m[2] === 'gap') {
    const b = el('button', 'btn primary', '去设置'); b.onclick = openSheet; bar.appendChild(b);
  }
  d.appendChild(bar);
  return d;
}

function skeletons(n) {
  const f = document.createDocumentFragment();
  for (let i = 0; i < n; i++) f.appendChild(el('div', 'skel'));
  return f;
}

/* ---------------- 今日 ---------------- */
async function viewToday(force) {
  const v = $('#view');
  if (cache.today && !force) { v.replaceChildren(cache.today); return; }
  v.replaceChildren(el('div', 'card', ''));
  v.firstChild.appendChild(skeletons(5));

  const j = await api('daily', {force: !!force});
  if (!j.ok) {
    const box = el('div');
    box.appendChild(errBox(j, () => viewToday(false)));
    v.replaceChildren(box);
    return;
  }
  const card = el('div', 'card');
  card.appendChild(el('div', 'head', esc(j.headline || '今日建议')));
  if (j.focus) card.appendChild(el('div', 'focus', esc(j.focus)));

  if ((j.dos || []).length) {
    card.appendChild(el('h3', null, '今天做'));
    const ul = el('ul', 'list');
    j.dos.forEach(t => {
      const li = el('li');
      li.appendChild(el('span', 'ic yes', '✓'));
      li.appendChild(el('span', null, esc(t)));
      ul.appendChild(li);
    });
    card.appendChild(ul);
  }
  if ((j.donts || []).length) {
    const h = el('h3', null, '今天别'); h.style.marginTop = '16px';
    card.appendChild(h);
    const ul = el('ul', 'list');
    j.donts.forEach(t => {
      const li = el('li');
      li.appendChild(el('span', 'ic no', '×'));
      li.appendChild(el('span', null, esc(t)));
      ul.appendChild(li);
    });
    card.appendChild(ul);
  }
  if (j.watch) {
    const w = el('div', 'watch');
    w.appendChild(el('span', null, '⚠'));
    w.appendChild(el('span', null, esc(j.watch)));
    w.style.marginTop = '16px';
    card.appendChild(w);
  }
  if (j.why) {
    const h = el('h3', null, '为什么'); h.style.marginTop = '16px';
    card.appendChild(h);
    card.appendChild(el('div', 'why', esc(j.why)));
  }

  const meta = el('div', 'meta');
  meta.appendChild(el('span', null,
    (j.cached ? '缓存 · ' : '') + esc(j.model || '') + (j.elapsed ? ' · ' + j.elapsed + 's' : '')));
  const b = el('button', 'btn', '重新生成');
  b.onclick = () => viewToday(true);
  meta.appendChild(b);
  card.appendChild(meta);

  const frag = document.createDocumentFragment();
  frag.appendChild(card);

  // 最近一次训练：一句话解读，点开看详情
  const last = el('div', 'card');
  last.appendChild(el('h3', null, '上一次训练'));
  last.appendChild(el('div', 'focus', '让教练单独拆解这次训练的质量和下次怎么改。'));
  const ab = el('button', 'btn', '分析最近一次训练');
  ab.style.marginTop = '12px';
  ab.onclick = () => analyzeLast(ab, last);
  last.appendChild(ab);
  frag.appendChild(last);

  cache.today = frag;
  v.replaceChildren(frag);
}

async function analyzeLast(btn, host) {
  btn.disabled = true; btn.textContent = '分析中…';
  const j = await api('activity', {});     // 不带 id 时由后端取最近一条
  btn.disabled = false; btn.textContent = '分析最近一次训练';
  if (!j.ok) { host.replaceChildren(errBox(j, () => analyzeLast(btn, host))); return; }
  const box = el('div');
  box.appendChild(el('div', 'head', esc(j.title || '训练解读')));
  box.appendChild(el('div', 'focus', esc(j.summary || '')));
  const wrap2 = el('div');
  if ((j.good || []).length || (j.improve || []).length) {
    wrap2.style.marginTop = '12px';
    const ul = el('ul', 'list');
    (j.good || []).forEach(t => {
      const li = el('li'); li.appendChild(el('span', 'ic yes', '✓'));
      li.appendChild(el('span', null, esc(t))); ul.appendChild(li);
    });
    (j.improve || []).forEach(t => {
      const li = el('li'); li.appendChild(el('span', 'ic no', '↑'));
      li.appendChild(el('span', null, esc(t))); ul.appendChild(li);
    });
    box.appendChild(ul);
  }
  if (j.next) {
    const w = el('div', 'watch');
    w.appendChild(el('span', null, '→'));
    w.appendChild(el('span', null, esc(j.next)));
    w.style.marginTop = '12px';
    w.style.background = 'var(--green-soft)'; w.style.color = '#0C6349';
    box.appendChild(w);
  }
  host.replaceChildren(box);
}

/* ---------------- 体重 ---------------- */
function spark(points, key) {
  const W = 320, H = 96, P = 8;
  if (!points || points.length < 2) return el('div', 'note', '数据点太少，画不出趋势');
  const vs = points.map(p => p[key]);
  const min = Math.min(...vs), max = Math.max(...vs);
  const span = (max - min) || 1;
  const x = i => P + i * (W - 2 * P) / (points.length - 1);
  const y = v => H - P - (v - min) / span * (H - 2 * P);
  const d = points.map((p, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p[key]).toFixed(1)).join(' ');
  const area = d + ' L' + x(points.length - 1).toFixed(1) + ' ' + (H - P) + ' L' + P + ' ' + (H - P) + ' Z';
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.setAttribute('class', 'spark');
  svg.innerHTML =
    '<defs><linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0%" stop-color="#17845F" stop-opacity=".22"/>' +
    '<stop offset="100%" stop-color="#17845F" stop-opacity="0"/></linearGradient></defs>' +
    '<path d="' + area + '" fill="url(#g1)"/>' +
    '<path d="' + d + '" fill="none" stroke="#17845F" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' +
    '<circle cx="' + x(points.length - 1).toFixed(1) + '" cy="' + y(vs[vs.length - 1]).toFixed(1) +
    '" r="3.2" fill="#17845F"/>';
  return svg;
}

async function viewWeight(force) {
  const v = $('#view');
  if (cache.weight && !force) { v.replaceChildren(cache.weight); return; }
  v.replaceChildren(el('div', 'card', ''));
  v.firstChild.appendChild(skeletons(4));

  const j = await api('weight', {days: 60});
  if (!j.ok) {
    const box = el('div');
    box.appendChild(errBox(j, () => viewWeight(false)));
    v.replaceChildren(box);
    return;
  }
  const frag = document.createDocumentFragment();

  const c1 = el('div', 'card');
  c1.appendChild(el('h3', null, '最近 ' + j.days + ' 天'));
  c1.appendChild(el('div', 'head', esc(j.summary || '体重趋势')));
  const chips = el('div', 'chips');
  const dl = Number(j.delta_total || 0);
  chips.appendChild(el('span', 'chip' + (dl > 0 ? ' amber' : ''), (dl > 0 ? '+' : '') + dl.toFixed(1) + ' kg'));
  chips.appendChild(el('span', 'chip gray', (Number(j.delta_per_week) > 0 ? '+' : '') +
    Number(j.delta_per_week).toFixed(2) + ' kg/周'));
  c1.appendChild(chips);
  c1.appendChild(spark(j.points, 'weight'));
  if (j.rate_verdict) c1.appendChild(el('div', 'why', esc(j.rate_verdict)));
  frag.appendChild(c1);

  if ((j.actions || []).length || (j.cautions || []).length) {
    const c2 = el('div', 'card');
    if ((j.actions || []).length) {
      c2.appendChild(el('h3', null, '接下来两周'));
      const ul = el('ul', 'list');
      j.actions.forEach(t => {
        const li = el('li'); li.appendChild(el('span', 'ic yes', '✓'));
        li.appendChild(el('span', null, esc(t))); ul.appendChild(li);
      });
      c2.appendChild(ul);
    }
    if ((j.cautions || []).length) {
      const h = el('h3', null, '注意'); h.style.marginTop = '16px';
      c2.appendChild(h);
      const ul = el('ul', 'list');
      j.cautions.forEach(t => {
        const li = el('li'); li.appendChild(el('span', 'ic warn', '!'));
        li.appendChild(el('span', null, esc(t))); ul.appendChild(li);
      });
      c2.appendChild(ul);
    }
    frag.appendChild(c2);
  }

  if (j.nutrition || (j.target && j.target.target_kcal)) {
    const c3 = el('div', 'card');
    c3.appendChild(el('h3', null, '吃法'));
    if (j.nutrition) c3.appendChild(el('div', 'why', esc(j.nutrition)));
    const t = j.target || {};
    if (t.target_kcal) {
      const kv = el('div', 'kv');
      [['热量', t.target_kcal + '', 'kcal'], ['蛋白', t.protein_g + '', 'g'],
       ['碳水', t.carb_g + '', 'g'], ['脂肪', t.fat_g + '', 'g']].forEach(([k, v, u]) => {
        const d = el('div');
        d.appendChild(el('div', 'v', esc(v)));
        d.appendChild(el('div', 'k', k + ' · ' + u));
        kv.appendChild(d);
      });
      kv.style.marginTop = '12px';
      c3.appendChild(kv);
    }
    frag.appendChild(c3);
  }

  const meta = el('div', 'meta');
  meta.appendChild(el('span', null, esc(j.model || '') + (j.elapsed ? ' · ' + j.elapsed + 's' : '')));
  const b = el('button', 'btn', '重新分析');
  b.onclick = () => viewWeight(true);
  meta.appendChild(b);
  frag.appendChild(meta);

  cache.weight = frag;
  v.replaceChildren(frag);
}

/* ---------------- 对话 ---------------- */
const QUICK = ['今天的心率区间怎么控制？', '这周跑量是不是加太快了？', '比赛前两周该怎么减量？'];

function renderChat() {
  const v = $('#view');
  const frag = document.createDocumentFragment();
  const box = el('div', 'chat');
  if (!CHAT.length) {
    const e = el('div', 'empty');
    e.appendChild(el('b', null, '问点什么'));
    e.appendChild(el('div', null, '教练已经知道你今天的数据：恢复度、睡眠、处方和体重。直接问就行。'));
    const q = el('div', 'quick');
    QUICK.forEach(t => {
      const b = el('button', null, esc(t));
      b.onclick = () => send(t);
      q.appendChild(b);
    });
    e.appendChild(q);
    box.appendChild(e);
  } else {
    CHAT.forEach(m => box.appendChild(el('div', 'msg ' + (m.role === 'user' ? 'u' : 'a'), esc(m.content))));
  }
  frag.appendChild(box);

  const comp = el('div', 'composer');
  const row = el('div', 'row');
  const ta = el('textarea');
  ta.rows = 1; ta.placeholder = '问教练…';
  ta.oninput = () => { ta.style.height = 'auto'; ta.style.height = Math.min(110, ta.scrollHeight) + 'px'; };
  ta.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(ta.value); } };
  const btn = el('button', 'send', '↑');
  btn.onclick = () => send(ta.value);
  row.appendChild(ta); row.appendChild(btn);
  comp.appendChild(row);
  frag.appendChild(comp);

  v.replaceChildren(frag);
  setTimeout(() => ta.focus(), 80);
}

async function send(text) {
  text = (text || '').trim();
  if (!text || STATE.busy) return;
  STATE.busy = true;
  CHAT.push({role: 'user', content: text});
  renderChat();
  const v = $('#view');
  const think = el('div', 'msg a think', '思考中…');
  v.querySelector('.chat').appendChild(think);

  const j = await api('chat', {messages: CHAT});
  STATE.busy = false;
  if (!j.ok) {
    think.remove();
    CHAT.pop();                      // 没答上就把这句撤回去，避免历史里留个孤立提问
    const box = el('div');
    box.appendChild(errBox(j, () => send(text)));
    v.querySelector('.chat').appendChild(box);
    return;
  }
  CHAT.push({role: 'assistant', content: j.reply});
  renderChat();
}

/* ---------------- 设置 ---------------- */
function openSheet() {
  /* 先把抽屉亮出来再填内容：填表万一出错，用户至少还能看到面板，
     错误信息落在 setMsg 里，而不是「点了没反应」。 */
  $('#sheet').classList.add('on');
  try {
    if (STATE.config) fillSheet(STATE.config);
  } catch (e) {
    const m = $('#setMsg');
    if (m) m.textContent = '设置界面加载异常：' + (e && e.message ? e.message : e);
    console.error('fillSheet failed', e);
  }
}
function closeSheet() {
  const s = $('#sheet');
  if (s) s.classList.remove('on');
  // 从 /ai#settings 进来的，关掉抽屉时把 hash 抹掉，刷新不会自动再弹
  if (location.hash === '#settings') history.replaceState(null, '', location.pathname);
}

/* 返回键先问页面：抽屉开着就关掉，返回 true 表示已消费。
   二级页与 tab 之间的回退交给原生统一处理，页面不自己判断层级，
   否则会和 WebView 历史栈叠成「返回在两个界面之间来回跳」。 */
window.__gcBack = function(){
  const s = $('#sheet');
  if (s && s.classList.contains('on')) { closeSheet(); return true; }
  return false;
};

function fillSheet(c) {
  const mode = c.mode || 'own';
  document.querySelectorAll('#modeSeg button').forEach(b =>
    b.classList.toggle('on', b.dataset.m === mode));
  shown('#ownBox', mode === 'own');
  shown('#platBox', mode === 'platform');

  const sel = $('#provider');
  if (!sel.options.length) {
    (c.providers || []).forEach(p => {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.label;
      sel.appendChild(o);
    });
  }
  sel.value = c.provider || 'deepseek';
  $('#baseUrl').value = c.base_url_custom ? c.base_url : '';
  $('#baseUrl').placeholder = c.base_url || '';
  $('#model').value = c.model_custom ? c.model : '';
  $('#model').placeholder = c.model || '';
  $('#apiKey').value = '';
  $('#keyHint').textContent = c.api_key_hint || '无';
  /* 官方服务地址出厂内置，界面上不再让用户填；只有勾了「自定义」才显示输入框 */
  const auto = c.platform_auto !== false;
  const cb = $('#platCustom');
  if (cb) cb.checked = !auto;
  shown('#platCustomBox', !auto);
  $('#platBase').value = auto ? '' : (c.platform_base || '');
  const svc = $('#platService');
  if (svc) svc.textContent = auto
    ? (c.platform_source === 'server'
        ? '官方服务已内置并已连接，无需填写地址。'
        : '官方服务已内置，无需填写地址。')
    : ('自定义服务地址：' + (c.platform_base || '未填写'));
  $('#platToken').value = '';
  const logged = !!c.has_platform_token;
  shown('#platLogin', !logged);
  shown('#platAcct', logged);
  // 未登录时发码会自动存配置，测试/保存这排按钮只会添乱
  shown('#mainAct', !(mode === 'platform' && !logged));
  $('#setHint').textContent = c.mode === 'platform'
    ? (logged
        ? '平台额度：已登录，每次问答按用量扣费。'
        : '平台额度：服务地址已内置，邮箱 / 手机号登录即可。')
    : '自配 API：用你自己的 Key 直连模型服务商，数据只发给这一家。';

  const p = (c.providers || []).find(x => x.id === sel.value);
  if (p) $('#provNote').textContent = p.note + (p.key_url ? ' · 申请地址 ' + p.key_url : '');

  if (mode === 'platform' && logged) loadBalance();
  else shown('#balBox', false);
}

async function loadBalance() {
  const j = await api('balance', {});
  const box = $('#balBox');
  box.style.display = '';
  if (!j.ok) {
    $('#balNum').textContent = '—';
    $('#balUnit').textContent = '获取失败';
    return;
  }
  $('#balNum').textContent = (j.balance == null ? '—' : Number(j.balance).toFixed(2));
  $('#balUnit').textContent = (j.currency || 'CNY') + (j.name ? ' · ' + j.name : '');
}

function collect() {
  const mode = (document.querySelector('#modeSeg button.on') || {}).dataset?.m || 'own';
  const custom = !!($('#platCustom') && $('#platCustom').checked);
  const p = {
    mode,
    provider: $('#provider').value,
    base_url: $('#baseUrl').value.trim(),
    model: $('#model').value.trim(),
    platform_auto: !custom
  };
  if (custom) p.platform_base = $('#platBase').value.trim();
  const k = $('#apiKey').value.trim();
  if (k) p.api_key = k;
  const t = $('#platToken').value.trim();
  if (t) p.platform_token = t;
  return p;
}

async function save() {
  const msg = $('#setMsg');
  msg.textContent = '保存中…';
  const j = await api('config.set', collect());
  if (!j.ok) { msg.textContent = j.error || '保存失败'; return; }
  STATE.config = j; cache.today = null; cache.weight = null;
  msg.textContent = '已保存';
  fillSheet(j);
  refreshSub();
  setTimeout(closeSheet, 400);
}

async function test() {
  const msg = $('#setMsg');
  msg.textContent = '正在调用模型…';
  await api('config.set', collect());       // 先存再测，否则测的是旧配置
  const j = await api('test', {});
  if (j.ok) {
    msg.textContent = '连接正常 · ' + j.model + ' · ' + j.elapsed + 's → ' + (j.reply || '');
    const c = await api('config.get', {});
    if (c.ok) { STATE.config = c; fillSheet(c); }
  } else {
    msg.textContent = (j.error || '连接失败') + (j.kind === 'auth' ? '（密钥无效）' : '');
  }
}

async function sendSms() {
  const msg = $('#setMsg'), note = $('#smsNote'), btn = $('#smsBtn');
  await api('config.set', collect());       // 先存配置（含平台模式/自定义开关），再发码
  note.textContent = '发送中…';
  const j = await api('sms', {account: $('#phoneInput').value.trim()});
  if (!j.ok) { note.textContent = j.error || '发送失败'; return; }
  if (j.dev_code) note.textContent = '【开发模式】验证码 ' + j.dev_code;
  else if (j.channel === 'email') note.textContent = '验证码已发到邮箱，5 分钟内有效（也看下垃圾邮件夹）';
  else note.textContent = '验证码已发送，5 分钟内有效';
  let left = 60;
  btn.disabled = true;
  const t = setInterval(() => {
    btn.textContent = left > 0 ? left + 's' : '发验证码';
    if (left-- <= 0) { clearInterval(t); btn.disabled = false; }
  }, 1000);
}

async function doLogin() {
  const msg = $('#setMsg');
  msg.textContent = '登录中…';
  const j = await api('auth', {account: $('#phoneInput').value.trim(),
                               code: $('#smsCode').value.trim(),
                               device_name: 'RunnerCoach'});
  if (!j.ok) { msg.textContent = j.error || '登录失败'; return; }
  msg.textContent = (j.created ? '注册成功，已赠送试用额度' : '欢迎回来') +
                    ' · ' + (j.phone_masked || '');
  $('#smsCode').value = '';
  const c = await api('config.get', {});
  if (c.ok) { STATE.config = c; fillSheet(c); }
  loadBalance();
}

async function doLogout() {
  const msg = $('#setMsg');
  const j = await api('logout', {});
  if (j.ok) {
    msg.textContent = '已退出登录（余额还在账号上，下次登录找回）';
    $('#platToken').value = '';
    const c = await api('config.get', {});
    if (c.ok) { STATE.config = c; fillSheet(c); }
    $('#balBox').style.display = 'none';
  } else msg.textContent = j.error || '操作失败';
}

async function doRedeem() {
  const code = $('#codeInput').value.trim();
  if (!code) return;
  const note = $('#redeemNote');
  note.textContent = '兑换中…';
  const j = await api('redeem', {code});
  if (!j.ok) { note.textContent = j.error || '兑换失败'; return; }
  note.textContent = '已加 ' + j.added + '，当前余额 ' + j.balance;
  $('#codeInput').value = '';
  loadBalance();
}

let PAY_TIMER = null;                    // 同一时间只轮询一个订单
async function pay(amount) {
  const note = $('#payNote');
  if (PAY_TIMER) { clearInterval(PAY_TIMER); PAY_TIMER = null; }
  note.textContent = '创建订单中…';
  const j = await api('order.create', {amount: Number(amount)});
  if (!j.ok) { note.textContent = j.error || '下单失败'; return; }
  if (j.channel === 'demo') {
    note.textContent = '【测试通道】订单 ' + j.order_id + ' 已创建，等待模拟到账…';
  } else {
    note.textContent = '订单 ' + j.order_id + ' 已创建（¥' + j.amount + '），请在微信中完成支付…';
    if (j.code_url) console.log('code_url:', j.code_url);   // 后续接扫码/APP支付时用
  }
  const oid = j.order_id, t0 = Date.now();
  PAY_TIMER = setInterval(async () => {
    const q = await api('order.query', {order_id: oid});
    if (q.ok && q.status === 'paid') {
      clearInterval(PAY_TIMER); PAY_TIMER = null;
      note.textContent = '支付成功，已到账 ¥' + q.amount + ' · 余额 ¥' + Number(q.balance).toFixed(2);
      loadBalance();
    } else if (q.ok && q.status === 'expired') {
      clearInterval(PAY_TIMER); PAY_TIMER = null;
      note.textContent = '订单已过期，请重新下单';
    } else if (Date.now() - t0 > 5 * 60 * 1000) {
      clearInterval(PAY_TIMER); PAY_TIMER = null;
      note.textContent = '等待超时。若已支付，稍后点「刷新余额」核对';
    } else {
      note.textContent = '等待支付完成…（订单 ' + oid + ' · ' + Math.round((Date.now() - t0) / 1000) + 's）';
    }
  }, 3000);
}

/* ---------------- 壳 ---------------- */
function refreshSub() {
  const c = STATE.config;
  const s = $('#sub');
  if (!c) { s.textContent = ''; return; }
  if (!c.configured) { s.textContent = '还没配置模型服务 · 点右上角设置'; return; }
  const mode = c.mode === 'platform' ? '平台额度' : '自配 API';
  s.textContent = mode + ' · ' + (c.model || '') + ' · ' + (c.provider_label || '');
}

function switchTab(t) {
  STATE.tab = t;
  if (location.hash !== '#' + t) history.replaceState(null, '', '#' + t);
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.v === t));
  if (t === 'today') viewToday(false);
  if (t === 'weight') viewWeight(false);
  if (t === 'chat') renderChat();
}

document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => switchTab(b.dataset.v));
$('#gear').onclick = openSheet;
$('#sheet').onclick = e => { if (e.target.id === 'sheet') closeSheet(); };
document.querySelectorAll('#modeSeg button').forEach(b => b.onclick = () => {
  document.querySelectorAll('#modeSeg button').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  shown('#ownBox', b.dataset.m === 'own');
  shown('#platBox', b.dataset.m === 'platform');
  shown('#mainAct', !(b.dataset.m === 'platform' &&
                      STATE.config && !STATE.config.has_platform_token));
});
$('#provider').onchange = () => {
  const c = STATE.config;
  const p = ((c && c.providers) || []).find(x => x.id === $('#provider').value);
  if (p) {
    $('#provNote').textContent = p.note + (p.key_url ? ' · 申请地址 ' + p.key_url : '');
    $('#baseUrl').value = ''; $('#model').value = '';
    $('#baseUrl').placeholder = p.base_url || '';
    $('#model').placeholder = p.model || '';
  }
};
const platCustom = $('#platCustom');
if (platCustom) platCustom.onchange = () => shown('#platCustomBox', platCustom.checked);
$('#saveBtn').onclick = save;
$('#testBtn').onclick = test;
$('#smsBtn').onclick = sendSms;
$('#loginBtn').onclick = doLogin;
$('#logoutBtn').onclick = doLogout;
$('#balBtn').onclick = loadBalance;
$('#redeemBtn').onclick = doRedeem;
document.querySelectorAll('[data-pay]').forEach(b => b.onclick = () => pay(b.dataset.pay));

(async function boot() {
  const c = await api('config.get', {});
  if (c.ok) STATE.config = c;
  refreshSub();
  const h = (location.hash || '').replace('#', '');
  const start = ['today', 'weight', 'chat'].includes(h) ? h : 'today';
  switchTab(start);
  if (h === 'settings') setTimeout(openSheet, 200);
  // 首次没配好，直接引导到设置
  if (STATE.config && !STATE.config.configured && !h) setTimeout(openSheet, 700);
})();
"""


def render(state: dict | None = None) -> str:
    """出整页。`state` 预留：将来可以把首屏状态内联，省掉一次往返。"""
    today = dt.date.today().isoformat()
    title = "AI 教练 · RunnerCoach"
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#070B12">
<title>{_h.escape(title)}</title>
<style>{CSS}</style>
</head><body>
{BODY}
<div style="display:none" id="boot">{_h.escape(json.dumps({"date": today}, ensure_ascii=False))}</div>
<script>{JS}</script>
</body></html>"""
