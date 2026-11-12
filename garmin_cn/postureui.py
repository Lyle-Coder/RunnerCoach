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
</style>"""

JS = """<script>
(function(){
  const box = document.getElementById('pt-res');
  document.querySelectorAll('.pt-mv').forEach(function(card){
    const inp = card.querySelector('input[type=file]');
    const go = card.querySelector('.go');
    go.addEventListener('click', function(e){ e.preventDefault(); inp.click(); });
    inp.addEventListener('change', async function(){
      const f = inp.files && inp.files[0];
      if (!f) return;
      card.classList.add('busy');
      go.textContent = '判读中…';
      try {
        const r = await fetch('/__posture/assess?move=' + encodeURIComponent(card.dataset.move),
                              {method:'POST', body:f, headers:{'Content-Type': f.type || 'image/jpeg'}});
        const j = await r.json();
        if (j.ok) { box.innerHTML = j.html; box.scrollIntoView({behavior:'smooth', block:'start'}); }
        else { box.innerHTML = '<div class="pt-lead">评估失败：' + (j.error || '未知') + '</div>'; }
      } catch (err) {
        box.innerHTML = '<div class="pt-lead">网络异常：' + err + '</div>';
      } finally {
        card.classList.remove('busy');
        go.textContent = '重新拍 / 换一张';
        inp.value = '';
      }
    });
  });
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


def _plan_html(res: dict) -> str:
    p = P.plan([res])
    if p["empty"]:
        return ('<div class="pt-h">结论</div><div class="pt-lead">'
                '这张照片没发现需要专项补的代偿。看不清的项会在下面列出来，'
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


def result_html(res: dict) -> str:
    """一次判读 → 判读表 + 方案。给 /__posture/assess 直接返回。"""
    rows = []
    for cid, it in res["checks"].items():
        c = P.CHECKS[cid]
        rows.append(
            f'<tr><td>{c["label"]}</td>'
            f'<td class="s-{it["state"]}">{P.STATES[it["state"]]}</td>'
            f'<td>{it["evidence"] or "—"}</td></tr>')
    head = f'<div class="pt-h">{res["name"]} 判读</div>'
    if res.get("shot_quality") == "bad":
        head += '<div class="pt-lead">照片质量不够，判读可能不准，建议按上面的拍摄要点重拍。</div>'
    return (head + '<table class="pt-tb"><tr><th>观察点</th><th>结果</th><th>依据</th></tr>'
            + "".join(rows) + '</table>' + _plan_html(res))


def render() -> str:
    from . import ui

    body = (CSS + '<p class="pt-lead">按提示做动作、拍一张全身照。'
                  '一张照片只评一个动作，正面和侧面分开拍。</p>'
            + _cards() + '<div class="pt-res" id="pt-res"></div>' + JS)
    return ui.page("COACH · 力量", "姿态评估",
                   "拍一张 → 找薄弱项 → 4 周方案",
                   body,
                   # 这句必须改：默认文案是「数据只保存在本机，不上传任何第三方」，
                   # 但判读要把照片发给用户自己配的模型服务商。页面不能骗人。
                   foot="照片会发给你自己配置的模型服务商做判读（不经过 RunnerCoach "
                        "的服务器），本机不留副本。不放心就跳过这页。",
                   back=("AI 教练", "/ai"))
