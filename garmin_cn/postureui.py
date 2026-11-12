# -*- coding: utf-8 -*-
"""姿态评估页面（/posture）：选动作 → 拍照 → 判读 → 4 周力量方案。

为什么是独立页面而不是弹层
------------------------
这一步要拍照、要等模型、要读一份方案，是有前后顺序的三件事。塞进弹层或
对话流里，用户拍完就找不到结果了。独立页面有稳定的「返回/重来」位置。

为什么照片走 `<input type=file>` 而不是 data-act 桥接
----------------------------------------------------
赛事附件那套 `data-act="attach:"` 只能传进赛事目录，姿态评估要把字节交给
`posture.assess()` 再调模型，路径完全不同。input file 浏览器和 WebView 都能用
（WebView 侧补了 onShowFileChooser），也不用为每个动作写一套桥接分支。
"""
from __future__ import annotations

import json

from . import posture as P

CSS = """<style>
.pt-lead{font-size:13px;color:var(--muted,#7F8A97);line-height:1.7;margin:0 0 14px}
.pt-mv{display:block;background:var(--card,#141C27);border:1px solid var(--line,#22303F);
  border-radius:14px;padding:14px;margin-bottom:12px}
.pt-mv b{display:block;font-size:15px;margin-bottom:6px}
.pt-mv span{display:block;font-size:12.5px;color:var(--ink-2,#C6D2DE);line-height:1.6}
.pt-mv em{display:block;font-size:11.5px;color:var(--muted,#7F8A97);font-style:normal;
  margin-top:6px;line-height:1.55}
.pt-mv .go{margin-top:10px;display:inline-block;padding:7px 14px;border-radius:9px;
  background:linear-gradient(135deg,#3B82F6,#22D3EE);color:#04121F;font-size:12.5px;
  font-weight:650;cursor:pointer}
.pt-mv.busy{opacity:.55;pointer-events:none}
.pt-res{margin-top:6px}
.pt-tb{width:100%;border-collapse:collapse;font-size:12.5px}
.pt-tb th,.pt-tb td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line,#22303F)}
.pt-tb th{color:var(--muted,#7F8A97);font-weight:500;font-size:11.5px}
.pt-tb td.s-ok{color:#4FD6A6}.pt-tb td.s-mild{color:#D6A75A}
.pt-tb td.s-clear{color:#E06A5F}.pt-tb td.s-unclear{color:var(--muted,#7F8A97)}
.pt-h{font-size:13px;margin:16px 0 8px;color:var(--ink-2,#C6D2DE)}
.pt-why{font-size:11.5px;color:var(--muted,#7F8A97);line-height:1.55}
.fold2{border:1px solid var(--line,#22303F);border-radius:10px;padding:8px 10px;margin:8px 0}
.fold2 summary{font-size:12px;color:var(--muted,#7F8A97);cursor:pointer}
.fold2[open]{padding-bottom:12px}
</style>"""

JS = """<script>
(function(){
  /* 多张判读各自保留，互不覆盖：以前每张都 innerHTML 覆盖整块，
     第二张失败时连第一张的成功结果一起冲掉了（用户 2026-11-12 反馈）。
     现在按动作存，任何一张成功都只是更新自己那一格，失败只落错误提示。 */
  const box = document.getElementById('pt-res');
  const err = document.getElementById('pt-err');
  const RES = __INIT_RES__;
  const ORDER = __INIT_ORDER__;

  async function refresh() {
    const list = ORDER.map(k => RES[k]).filter(Boolean);
    try {
      const r = await fetch('/__posture/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({results: list})
      });
      const j = await r.json();
      if (j.ok) box.innerHTML = j.html;
      else err.innerHTML = '<div class="pt-lead">汇总失败：' + (j.error || '未知') + '</div>';
    } catch (e) {
      err.innerHTML = '<div class="pt-lead">汇总异常：' + e + '</div>';
    }
  }

  document.querySelectorAll('.pt-mv').forEach(function(card){
    const move = card.dataset.move;
    const inp = card.querySelector('input[type=file]');
    const go = card.querySelector('.go');
    go.addEventListener('click', function(e){ e.preventDefault(); inp.click(); });
    inp.addEventListener('change', async function(){
      const f = inp.files && inp.files[0];
      if (!f) return;
      card.classList.add('busy');
      go.textContent = '判读中…';
      err.innerHTML = '';
      try {
        const r = await fetch('/__posture/assess?move=' + encodeURIComponent(move),
                              {method:'POST', body:f,
                               headers:{'Content-Type': f.type || 'image/jpeg'}});
        const j = await r.json();
        if (j.ok) {
          RES[move] = j.result;
          if (ORDER.indexOf(move) < 0) ORDER.push(move);
          go.textContent = '重拍 / 换一张';
          await refresh();
          box.scrollIntoView({behavior:'smooth', block:'start'});
        } else {
          err.innerHTML = '<div class="pt-lead">' + (j.error || '评估失败') + '</div>';
        }
      } catch (e2) {
        err.innerHTML = '<div class="pt-lead">网络异常：' + e2 + '</div>';
      } finally {
        card.classList.remove('busy');
        if (!RES[move]) go.textContent = '拍照 / 选图';
        inp.value = '';
      }
    });
  });

  const clr = document.getElementById('ptClear');
  if (clr) {
    let arm = 0;
    clr.addEventListener('click', async function(){
      if (Date.now() > arm) {
        arm = Date.now() + 4000;
        clr.textContent = '再点一次确认清空';
        clr.style.color = '#E06A5F';
        setTimeout(function(){
          if (Date.now() > arm) { clr.textContent = '清空全部记录'; clr.style.color = ''; }
        }, 4200);
        return;
      }
      try { await fetch('/__posture/clear', {method:'POST'}); } catch (e) {}
      location.reload();
    });
  }
})();
</script>"""


def _cards() -> str:
    out = []
    for mid, m in P.MOVES.items():
        out.append(
            f'<div class="pt-mv" data-move="{mid}">'
            f'<b>{m["name"]}</b>'
            f'<span>{m["how"]}</span>'
            f'<em>怎么拍：{m["shot"]}</em>'
            f'<em>看什么：{"、".join(P.CHECKS[c]["label"] for c in m["checks"])}</em>'
            f'<input type="file" accept="image/*" capture="environment" hidden>'
            f'<i class="go">拍照 / 选图</i></div>')
    return "".join(out)


def _plan_html(results: list[dict]) -> str:
    """所有判读 → 一份统一方案。

    多张照片各给一份方案是最省事的写法，但同一块薄弱项会被重复安排三遍，
    用户还得自己合并。P.plan 已经按「命中次数」排过序，这里直接用。
    """
    p = P.plan(results)
    if p["empty"]:
        return ('<div class="pt-h">结论</div><div class="pt-lead">'
                '这些照片里没发现需要专项补的代偿。看不清的项会在下面列出来，'
                '补拍一张更清楚的角度能提高覆盖度。</div>')
    weak = "".join(
        f'<div class="pt-mv"><b>{w["label"]}</b><span>{w["why"]}</span>'
        f'<em>依据：{"、".join(w["from"])}</em></div>' for w in p["weak"])
    moves = "".join(
        f'<tr><td>{m["name"]}</td><td>{m["sets"]} × {m["reps"]}</td>'
        f'<td>{m["cue"]}</td></tr>' for m in p["moves"])
    weeks = "".join(
        f'<tr><td>W{w["wk"]}</td><td>{w["sets"]} × {w["reps"]}</td><td>{w["note"]}</td></tr>'
        for w in p["weeks"])
    return (
        '<div class="pt-h">薄弱项</div>' + weak +
        '<div class="pt-h">4 周方案 · 每周 2 次</div>'
        '<table class="pt-tb"><tr><th>动作</th><th>组数 × 次数</th><th>要点</th></tr>'
        + moves + '</table>'
        '<div class="pt-h">周期安排</div>'
        '<table class="pt-tb"><tr><th>周</th><th>组数 × 次数</th><th>说明</th></tr>'
        + weeks + '</table>')


def _brief(r: dict) -> tuple[str, str]:
    """一张判读压成一句话 + 颜色类。摘要行和历史列表都复用它。"""
    checks = r.get("checks") or {}
    bad = [(c, it) for c, it in checks.items() if it.get("state") in ("mild", "clear")]
    un = [c for c, it in checks.items() if it.get("state") == "unclear"]
    if bad:
        txt = "、".join(f'{P.CHECKS[c]["label"]}{P.STATES[it["state"]]}' for c, it in bad)
        cls = "s-clear" if any(it["state"] == "clear" for _, it in bad) else "s-mild"
    elif un:
        txt = "看不清：" + "、".join(P.CHECKS[c]["label"] for c in un)
        cls = "s-unclear"
    else:
        txt = "未发现明显代偿"
        cls = "s-ok"
    return txt, cls


def _summary_row(r: dict) -> str:
    """一行：动作 + 主要问题。细节折进下面的 details。"""
    txt, cls = _brief(r)
    return f'<tr><td>{r.get("name") or r.get("move")}</td><td class="{cls}">{txt}</td></tr>'


def summary_html(results: list[dict]) -> str:
    """所有已完成的判读 → 一页汇总（不是每张各一份方案）。"""
    if not results:
        return '<div class="pt-lead">还没有判读结果。上面选一个动作拍一张就行。</div>'
    rows = "".join(_summary_row(r) for r in results)
    detail = "".join(
        f'<details class="fold2"><summary>{r["name"]}</summary>'
        '<table class="pt-tb"><tr><th>观察点</th><th>结果</th><th>依据</th></tr>'
        + "".join(f'<tr><td>{P.CHECKS[c]["label"]}</td>'
                  f'<td class="s-{it["state"]}">{P.STATES[it["state"]]}</td>'
                  f'<td>{it["evidence"] or "—"}</td></tr>'
                  for c, it in r["checks"].items())
        + '</table></details>' for r in results)
    return ('<div class="pt-h">已评 ' + str(len(results)) + ' 项</div>'
            '<table class="pt-tb"><tr><th>动作</th><th>判读</th></tr>' + rows + '</table>'
            + _plan_html(results)
            + '<details class="fold2"><summary>展开每张的详细依据</summary>'
            + detail + '</details>')


def _history_html() -> str:
    """历史记录：每次判读的时间、动作、结论。

    只留 current 是回答不了「我上个月拍的是什么样」的，所以每次评估都追加。
    默认折叠，不占版面。
    """
    from . import posturestore as PS

    hist = PS.load()["history"][:12]
    if not hist:
        return ""
    rows = "".join(
        f'<tr><td>{h.get("at") or ""}</td><td>{h.get("name") or h.get("move") or ""}</td>'
        f'<td class="{_brief(h.get("result") or {})[1]}">'
        f'{_brief(h.get("result") or {})[0]}</td></tr>' for h in hist)
    return ('<details class="fold2"><summary>历史记录（最近 '
            + str(len(hist)) + ' 次）</summary>'
            '<table class="pt-tb"><tr><th>时间</th><th>动作</th><th>判读</th></tr>'
            + rows + '</table>'
            '<div style="margin-top:12px"><button class="btn" type="button" id="ptClear">'
            '清空全部记录</button></div>'
            '<p class="pt-why" style="margin-top:6px">清空只删本机的判读记录，'
            '照片本来就没有存下来。</p></details>')


def render() -> str:
    from . import ui, posturestore as PS

    # 当前这一轮直接渲染进页面：切出去再回来（WebView 重建）不用重拍，
    # 汇总方案也在。以前结果只活在前端 RES 里，一离开就没了。
    cur = PS.current()
    order = [r.get("move") for r in cur if r.get("move")]
    init_html = summary_html(cur) if cur else ""
    js = (JS.replace("__INIT_RES__",
                     json.dumps({r["move"]: r for r in cur if r.get("move")},
                                ensure_ascii=False))
            .replace("__INIT_ORDER__", json.dumps(order, ensure_ascii=False)))

    body = (CSS + '<p class="pt-lead">按提示做动作、拍一张全身照。'
                  '一张照片只评一个动作，正面和侧面分开拍。</p>'
            + _cards()
            + '<div id="pt-err"></div>'
            + f'<div class="pt-res" id="pt-res">{init_html}</div>'
            + js + _history_html())
    return ui.page("COACH · 力量", "姿态评估",
                   "拍一张 → 找薄弱项 → 4 周方案",
                   body,
                   # 这句必须改：默认文案是「数据只保存在本机，不上传任何第三方」，
                   # 但判读要把照片发给用户自己配的模型服务商。页面不能骗人。
                   foot="照片会发给你自己配置的模型服务商做判读（不经过 RunnerCoach "
                        "的服务器），照片本机不落盘；判读结果保存在本机，方便回看。",
                   back=("AI 教练", "/ai"))
