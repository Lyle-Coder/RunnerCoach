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
  --bg:#F4F6F2; --card:#FFFFFF; --line:#E3E7DF; --line-2:#EDF0EA;
  --ink:#1B1F1A; --ink-2:#4A5147; --muted:#7A8276; --faint:#A5AC9F;
  --green:#1D9E75; --green-soft:#E4F3EC; --amber:#C08A16; --amber-soft:#FBF1DA;
  --red:#C2453F; --red-soft:#FAE7E5; --blue:#3A6EA5; --blue-soft:#E8F0F8;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.6;font-size:14px;
  padding-bottom:env(safe-area-inset-bottom)}
.wrap{max-width:720px;margin:0 auto;padding:16px 14px 24px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.top h1{margin:0;font-size:18px;font-weight:600;letter-spacing:.2px}
.top .sub{font-size:11.5px;color:var(--muted);margin-top:2px}
.gear{width:34px;height:34px;border-radius:10px;border:1px solid var(--line);background:var(--card);
  display:flex;align-items:center;justify-content:center;font-size:16px;color:var(--ink-2);cursor:pointer}
.tabs{display:flex;gap:6px;background:#E9EDE5;border-radius:12px;padding:4px;margin-bottom:14px}
.tabs button{flex:1;border:0;background:transparent;color:var(--ink-2);font-size:13px;font-weight:500;
  padding:8px 0;border-radius:9px;font-family:inherit;cursor:pointer;transition:.15s}
.tabs button.on{background:var(--card);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.06)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:12px}
.card h3{margin:0 0 10px;font-size:11.5px;font-weight:600;color:var(--muted);letter-spacing:1px}
.head{font-size:21px;font-weight:600;line-height:1.35;letter-spacing:.2px}
.focus{font-size:13.5px;color:var(--ink-2);margin-top:6px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.chip{font-size:11.5px;padding:3px 9px;border-radius:999px;background:var(--green-soft);color:#0F6E56}
.chip.gray{background:var(--line-2);color:var(--ink-2)}
.chip.amber{background:var(--amber-soft);color:#8A6410}
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
.btn:active{background:#F1F4EE}
.btn.primary{background:var(--green);border-color:var(--green);color:#fff}
.btn.primary:active{background:#178A66}
.btn:disabled{opacity:.5}
.skel{height:14px;border-radius:6px;background:linear-gradient(90deg,#EEF1EB 25%,#F6F8F4 37%,#EEF1EB 63%);
  background-size:400% 100%;animation:sh 1.2s ease infinite;margin-bottom:9px}
@keyframes sh{0%{background-position:100% 50%}100%{background-position:0 50%}}
.empty{text-align:center;padding:22px 10px;color:var(--muted);font-size:13px;line-height:1.7}
.empty b{display:block;color:var(--ink);font-size:14.5px;font-weight:600;margin-bottom:6px}
.err{border-left:3px solid var(--red);background:var(--red-soft);border-radius:10px;padding:12px 14px;
  font-size:13px;color:#8C2F2A;line-height:1.65}
.err b{display:block;margin-bottom:4px}
.quote{font-size:12px;color:var(--muted);background:#F7F9F5;border-radius:8px;padding:8px 10px;
  margin-top:8px;white-space:pre-wrap;max-height:160px;overflow:auto}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:4px}
.kv div{background:#F7F9F5;border-radius:10px;padding:9px 8px;text-align:center}
.kv .v{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.kv .k{font-size:10.5px;color:var(--muted);margin-top:2px}
/* 聊天 */
.chat{display:flex;flex-direction:column;gap:10px;min-height:40vh}
.msg{max-width:86%;padding:10px 13px;border-radius:14px;font-size:13.5px;line-height:1.65;
  white-space:pre-wrap;word-break:break-word}
.msg.u{align-self:flex-end;background:var(--green);color:#fff;border-bottom-right-radius:5px}
.msg.a{align-self:flex-start;background:var(--card);border:1px solid var(--line);
  border-bottom-left-radius:5px}
.msg.a.think{color:var(--muted);font-style:normal}
.quick{display:flex;gap:7px;flex-wrap:wrap;margin-top:4px}
.quick button{border:1px solid var(--line);background:var(--card);border-radius:999px;
  padding:7px 12px;font-size:12.5px;color:var(--ink-2);font-family:inherit}
.composer{position:sticky;bottom:0;background:linear-gradient(180deg,rgba(244,246,242,0) 0%,var(--bg) 28%);
  padding:10px 0 calc(10px + env(safe-area-inset-bottom))}
.composer .row{display:flex;gap:8px;align-items:flex-end;background:var(--card);border:1px solid var(--line);
  border-radius:14px;padding:7px 7px 7px 12px}
.composer textarea{flex:1;border:0;outline:0;resize:none;font-family:inherit;font-size:14px;
  line-height:1.5;max-height:110px;background:transparent;color:var(--ink);padding:6px 0}
.composer .send{width:34px;height:34px;border-radius:10px;border:0;background:var(--green);color:#fff;
  font-size:15px;flex:0 0 auto;cursor:pointer}
.composer .send:disabled{background:#C9D6CD}
/* 设置抽屉 */
.sheet{position:fixed;inset:0;background:rgba(20,28,22,.36);display:none;z-index:40}
.sheet.on{display:block}
.panel{position:absolute;left:0;right:0;bottom:0;max-height:88vh;overflow:auto;background:var(--bg);
  border-radius:20px 20px 0 0;padding:18px 16px calc(24px + env(safe-area-inset-bottom));
  animation:up .22s ease}
@keyframes up{from{transform:translateY(18px);opacity:.6}to{transform:none;opacity:1}}
.panel h2{margin:0 0 4px;font-size:16px;font-weight:600}
.panel .hint{font-size:12px;color:var(--muted);line-height:1.6;margin-bottom:14px}
.seg{display:flex;gap:6px;background:#E9EDE5;border-radius:11px;padding:4px;margin-bottom:14px}
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
.spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.4);
  border-top-color:#fff;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px}
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
      <label class="f">平台地址</label>
      <input class="f" id="platBase" placeholder="https://ai.你的域名.com" autocapitalize="off" spellcheck="false">
      <div class="note">余额挂在账号上：卸载重装 / 换手机后登录同一账号即可找回。</div>

      <label class="f">手机号 / 邮箱</label>
      <div class="row2">
        <input class="f" id="phoneInput" type="text" placeholder="11 位手机号或邮箱" autocapitalize="off" autocorrect="off" spellcheck="false">
        <button class="btn" id="smsBtn">发验证码</button>
      </div>
      <label class="f">验证码</label>
      <div class="row2">
        <input class="f" id="smsCode" inputmode="numeric" maxlength="6" placeholder="6 位数字" autocapitalize="off" spellcheck="false">
        <button class="btn primary" id="loginBtn">登录 / 注册</button>
      </div>
      <div class="note" id="smsNote">邮箱登录免费直达；未注册的账号自动注册并赠送试用额度。</div>

      <label class="f">设备令牌</label>
      <input class="f" id="platToken" type="password" placeholder="登录后自动获取，也可手动粘贴" autocapitalize="off" spellcheck="false">

      <div class="act">
        <button class="btn" id="balBtn">查余额</button>
        <button class="btn" id="logoutBtn">退出登录</button>
      </div>
      <div class="bal" id="balBox" style="display:none">
        <span class="n" id="balNum">—</span><span class="u" id="balUnit">元</span>
      </div>

      <label class="f">兑换码</label>
      <div class="row2">
        <input class="f" id="codeInput" placeholder="RC-XXXX-XXXX" autocapitalize="characters" spellcheck="false">
        <button class="btn" id="redeemBtn">兑换</button>
      </div>
      <div class="note" id="redeemNote">兑换码由服务端批量生成，一次一用。</div>
    </div>

    <div class="act">
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

let STATE = { config: null, tab: 'today', busy: false };
let CHAT = [];              // {role, content}
const cache = {};           // 每个 tab 的渲染结果，切回来不用重新请求

async function api(action, body) {
  const r = await fetch('/ai/' + action, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
  });
  let j;
  try { j = await r.json(); } catch (e) { j = {ok: false, error: '返回的不是 JSON（HTTP ' + r.status + '）'}; }
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
    net:    ['网络到不了', '检查网络；自配地址要能被手机访问（iOS 需 HTTPS）。', 'retry'],
    data:   ['数据读不到', '先在 App 里同步一次，或确认今天有数据。', 'gap'],
    upstream: ['模型服务报错', '这多半是上游临时故障，稍后重试。', 'retry'],
    request: ['请求被拒绝', '换一个模型试试，或检查模型名是否写错。', 'retry']
  };
  const m = map[j.kind] || ['出错了', '', 'retry'];
  const d = el('div', 'err');
  d.appendChild(el('b', null, esc(m[0])));
  d.appendChild(el('div', null, esc(j.error || '')));
  if (m[1]) d.appendChild(el('div', 'note', esc(m[1])));
  if (j.detail) {
    const q = el('div', 'quote', esc(j.detail));
    d.appendChild(q);
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
    w.style.background = 'var(--green-soft)'; w.style.color = '#0F6E56';
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
    '<stop offset="0%" stop-color="#1D9E75" stop-opacity=".22"/>' +
    '<stop offset="100%" stop-color="#1D9E75" stop-opacity="0"/></linearGradient></defs>' +
    '<path d="' + area + '" fill="url(#g1)"/>' +
    '<path d="' + d + '" fill="none" stroke="#1D9E75" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' +
    '<circle cx="' + x(points.length - 1).toFixed(1) + '" cy="' + y(vs[vs.length - 1]).toFixed(1) +
    '" r="3.2" fill="#1D9E75"/>';
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
  const c = STATE.config;
  if (c) fillSheet(c);
  $('#sheet').classList.add('on');
}
function closeSheet() { $('#sheet').classList.remove('on'); }

function fillSheet(c) {
  const mode = c.mode || 'own';
  document.querySelectorAll('#modeSeg button').forEach(b =>
    b.classList.toggle('on', b.dataset.m === mode));
  $('#ownBox').style.display = mode === 'own' ? '' : 'none';
  $('#platBox').style.display = mode === 'platform' ? '' : 'none';

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
  $('#platBase').value = c.platform_base || '';
  $('#platToken').value = '';
  $('#logoutBtn').style.display = c.has_platform_token ? '' : 'none';
  $('#setHint').textContent = c.mode === 'platform'
    ? (c.has_platform_token
        ? '平台额度：已登录，余额挂在账号上，换机重登不丢。'
        : '平台额度：填好地址后用手机号或邮箱登录，余额挂账号不丢。')
    : '自配 API：用你自己的 Key 直连模型服务商，数据只发给这一家。';

  const p = (c.providers || []).find(x => x.id === sel.value);
  if (p) $('#provNote').textContent = p.note + (p.key_url ? ' · 申请地址 ' + p.key_url : '');

  if (c.platform_base) loadBalance();
}

async function loadBalance() {
  const j = await api('balance', {});
  const box = $('#balBox');
  if (!j.ok) { box.style.display = 'none'; return; }
  box.style.display = '';
  $('#balNum').textContent = (j.balance == null ? '—' : Number(j.balance).toFixed(2));
  $('#balUnit').textContent = (j.currency || 'CNY') + (j.name ? ' · ' + j.name : '');
}

function collect() {
  const mode = (document.querySelector('#modeSeg button.on') || {}).dataset?.m || 'own';
  const p = {
    mode,
    provider: $('#provider').value,
    base_url: $('#baseUrl').value.trim(),
    model: $('#model').value.trim(),
    platform_base: $('#platBase').value.trim()
  };
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
  await api('config.set', collect());       // 先存地址，验证码请求才知道打到哪
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
  $('#ownBox').style.display = b.dataset.m === 'own' ? '' : 'none';
  $('#platBox').style.display = b.dataset.m === 'platform' ? '' : 'none';
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
$('#saveBtn').onclick = save;
$('#testBtn').onclick = test;
$('#smsBtn').onclick = sendSms;
$('#loginBtn').onclick = doLogin;
$('#logoutBtn').onclick = doLogout;
$('#balBtn').onclick = loadBalance;
$('#redeemBtn').onclick = doRedeem;

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
<meta name="theme-color" content="#F4F6F2">
<title>{_h.escape(title)}</title>
<style>{CSS}</style>
</head><body>
{BODY}
<div style="display:none" id="boot">{_h.escape(json.dumps({"date": today}, ensure_ascii=False))}</div>
<script>{JS}</script>
</body></html>"""
