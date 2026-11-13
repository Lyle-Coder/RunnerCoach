"""Local training preview and explicit COROS import, shared by desktop and Android."""
from __future__ import annotations

import datetime as dt
import threading

from . import ui

_AUTH_LOCK = threading.Lock()
_AUTH_STATE = {"running": False, "error": ""}


def start_auth():
    """Desktop only: browser login and loopback callback; never uploads a workout."""
    from . import appapi as A
    from .coros_mcp import login_local
    with _AUTH_LOCK:
        if _AUTH_STATE["running"]:
            return {"ok": True, "running": True}
        _AUTH_STATE.update(running=True, error="")
    def work():
        try:
            login_local(A.HOME, A._coros_region(), timeout=300, say=lambda *a: None)
        except Exception as e:
            with _AUTH_LOCK:
                _AUTH_STATE["error"] = str(e)
        finally:
            with _AUTH_LOCK:
                _AUTH_STATE["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "running": True}


def auth_status():
    from . import appapi as A
    with _AUTH_LOCK:
        return {**A.coros_mcp_status(), **_AUTH_STATE}


def _step(s):
    esc = ui.esc
    if s.get("intervalGroup"):
        return (f'<div class="card"><b>重复 {s["repeats"]} 组</b>' +
                ''.join(_step(x) for x in s["sets"]) + '</div>')
    name = {1: "热身", 2: "训练", 3: "恢复", 4: "放松"}[s["sectionType"]]
    target = {1: lambda v: f"{v / 1000:g} km", 2: lambda v: f"{v / 60:g} 分钟",
              3: lambda v: f"爬升 {v} m", 4: lambda v: "按手表计圈键结束"}[s["targetType"]](s.get("targetValue", 0))
    if "intensityType" not in s:
        intensity = "不设置强度提醒"
    else:
        a, b = s["intensityValueStart"], s["intensityValueEnd"]
        if s["intensityType"] == 2:
            intensity = f"{a // 60}:{a % 60:02d}–{b // 60}:{b % 60:02d} /km"
        else:
            intensity = f'{a}–{b} ' + ("bpm" if s["intensityType"] == 1 else "W")
    return f'<div class="kvline"><span>{esc(name)} · {esc(target)}</span><b>{esc(intensity)}</b></div>'


def render(qs=None, token=""):
    from . import appapi as A
    esc = ui.esc
    qs = qs or {}
    def value(key, default=""):
        return str((qs.get(key) or [default])[0])
    today = dt.date.today().isoformat()
    source_date = value("source_date", today)
    date = value("date", source_date)
    sport = value("sport", "auto")
    destination = value("destination", "calendar")
    no_intensity = value("no_intensity") == "1"
    def options(items, selected):
        return ''.join(f'<option value="{k}"{" selected" if k == selected else ""}>{v}</option>' for k, v in items)
    form = ('<form method="get" action="/training" class="card" data-nopush>'
            '<input type="hidden" name="preview" value="1">'
            '<label>训练项目<select name="sport">' + options((("auto", "跟随运动设置"), ("run", "跑步"), ("bike", "骑行")), sport) + '</select></label>'
            f'<label>本地课表日期<input type="date" name="source_date" value="{esc(source_date)}" required></label>'
            '<label>导入位置<select name="destination">' + options((("calendar", "指定日期的训练日历"), ("library", "训练库（不指定日期）")), destination) + '</select></label>'
            f'<label id="date-label">安排日期<input type="date" name="date" value="{esc(date)}" min="{today}" max="{(dt.date.today() + dt.timedelta(days=90)).isoformat()}"></label>'
            '<label class="check"><input type="checkbox" name="no_intensity" value="1"' + (' checked' if no_intensity else '') + '>不设置强度提醒，仅保留时间/距离及文字说明</label>'
            '<p class="note">今日建议根据恢复状态生成；其它日期仅支持已配置的本地备赛课。导入不会覆盖高驰原有课程。</p>'
            '<button class="btn primary" type="submit">预览训练</button></form>')
    status = A.coros_mcp_status()
    authorized = bool(status.get("authorized"))
    history = ""
    if authorized:
        from .coros_workout import recent_imports
        from .coros_mcp import CorosMcp
        for row in recent_imports(A.HOME, CorosMcp(A.HOME, A._coros_region())):
            state = "已核验" if row["state"] == "verified" else "结果待核对（不会自动重发）"
            date_label = row["date"] or "训练库"
            history += (f'<div class="card"><b>{esc(row["name"])}</b><p class="note">{esc(date_label)} · {state}</p>'
                        + (f'<button class="btn recheck" data-import-key="{row["import_key"]}" type="button">核对这条记录</button>' if row["state"] != "verified" else '') + '</div>')
    auth = ('<div class="card"><b>高驰官方授权</b><p class="note">' +
            ('已授权。训练写入高驰账户后，仍需通过高驰 App 同步到设备。' if authorized else '请先授权高驰账户；预览不需要联网，点击导入后才会发送训练。') +
            '</p><button class="btn" id="coros-auth" type="button">' + ('重新授权' if authorized else '授权高驰') + '</button></div>')
    preview, content, preview_id = {}, "", ""
    if value("preview") == "1":
        preview = A.coros_workout_preview(source_date, sport, destination, date, no_intensity)
        if not preview.get("ok"):
            content = '<div class="card"><p role="alert">' + esc(preview.get("error")) + '</p></div>'
        else:
            preview_id = preview["preview_id"]
            for course in preview["courses"]:
                content += ('<div class="card"><h3>' + esc(course["courseName"]) + '</h3>' +
                            ''.join(_step(s) for s in course["sections"]) +
                            '<p class="note" style="white-space:pre-wrap">' + esc(course["courseDescription"]) + '</p></div>')
            content += ''.join('<div class="card note">' + esc(w) + '</div>' for w in preview["warnings"])
            content += ('<div class="card"><b>' + esc('安排到 ' + preview["date"] if destination == "calendar" else '仅存入高驰训练库') +
                        '</b><p class="note">请核对课程内容。高驰同日原有训练会保留，请考虑当天总训练量。训练导入与路线导航分别管理。</p>'
                        '<div class="pager"><button class="btn primary" id="coros-submit" type="button"' + ('' if authorized else ' disabled') +
                        '>导入高驰</button><button class="btn" id="coros-check" type="button">检查导入结果</button></div></div>')
    style = '<style>.training form label{display:block;margin:12px 0}.training input:not([type=checkbox]),.training select{display:block;box-sizing:border-box;width:100%;padding:10px;margin-top:6px;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px}.training .check{display:flex;gap:10px;align-items:start}.training .kvline{gap:12px;flex-wrap:wrap}.training .card{overflow-wrap:anywhere}.training button:disabled{opacity:.45}</style>'
    # Values are escaped into data attributes, never interpolated into JavaScript.
    script = '''<script>(function(){
var root=document.getElementById('training'), msg=document.getElementById('training-result');
function show(s){msg.textContent=s;}
function post(path,data){data=data||{};data.token=root.dataset.token;
return fetch('/__training/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(function(r){return r.json();});}
var auth=document.getElementById('coros-auth');
auth.onclick=function(){
if(window.GC&&GC.action){GC.action('corosTrainingAuth');return;}
auth.disabled=true;show('正在打开高驰官方授权页…');
post('auth').then(function(r){if(!r.ok)throw Error(r.error||'无法打开授权页');
var timer=setInterval(function(){fetch('/__training/status').then(function(x){return x.json();}).then(function(s){
if(!s.running){clearInterval(timer);auth.disabled=false;if(s.error){show(s.error);}else if(s.authorized){location.reload();}}});},2000);
}).catch(function(e){auth.disabled=false;show(e.message);});};
function send(check){var b=document.getElementById('coros-submit'),c=document.getElementById('coros-check');
if(!root.dataset.preview){show('请先预览训练');return;}
b.disabled=true;c.disabled=true;show(check?'正在核对高驰账户…':'正在导入，请稍候…');
post(check?'check':'submit',{preview_id:root.dataset.preview}).then(function(r){
var lines=[r.message||r.error||'操作结束'];(r.items||[]).forEach(function(i){lines.push((i.name||'训练')+'：'+(i.ok?(i.duplicate?'已导入，未重复创建':'已核验'):i.error));});
show(lines.join('\\n'));if(!r.ok&&!r.uncertain)b.disabled=false;c.disabled=false;
}).catch(function(){show('连接中断，结果尚未确认。请先检查导入结果，避免重复创建。');c.disabled=false;});}
var b=document.getElementById('coros-submit'),c=document.getElementById('coros-check');
if(b)b.onclick=function(){send(false);};if(c)c.onclick=function(){send(true);};
document.querySelectorAll('.recheck').forEach(function(button){button.onclick=function(){
button.disabled=true;show('正在核对导入记录…');post('recheck',{import_key:button.dataset.importKey}).then(function(r){
show(r.message||r.error||'核对完成');button.disabled=false;if(r.ok){button.parentNode.querySelector('.note').textContent='已核验，可在高驰 App 同步到设备';button.hidden=true;}
}).catch(function(){button.disabled=false;show('暂时无法核对，请稍后再试；不会自动重发训练。');});};});
var dest=document.querySelector('[name=destination]');function toggle(){var input=document.querySelector('[name=date]');input.disabled=dest.value==='library';document.getElementById('date-label').hidden=input.disabled;}dest.onchange=toggle;toggle();
})();</script>'''
    body = (style + f'<div class="training" id="training" data-token="{esc(token)}" data-preview="{esc(preview_id)}">' +
            ui.sec("选择本地训练", form) + ui.sec("连接高驰", auth) +
            (ui.sec("导入预览", content) if content else '') +
            (ui.sec("最近导入", history) if history else '') +
            '<div class="card" id="training-result" role="status" aria-live="polite" style="white-space:pre-wrap">预览后点击「导入高驰」才会提交。</div></div>' + script)
    return ui.page("TRAINING · 高驰", "导入跑步与骑行训练", "本地生成 · 预览后导入", body,
                   foot="仅在点击导入后，将所选训练发送至高驰账户。设备同步由高驰 App 完成。", back=("返回训练计划", "/plan"))
