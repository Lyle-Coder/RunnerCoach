# -*- coding: utf-8 -*-
"""AI 教练页面回归：防止「点设置毫无反应」这类空引用再溜进 APK。

v0.4.3 的真事故：fillSheet 里写了 `$('#mainAct').style.display = ...`，
但 HTML 那排按钮压根没加 id="mainAct"。openSheet 先调 fillSheet 再显示抽屉，
于是一次 TypeError 就把整个点击吃掉了——页面不报错、按钮也不动。

两层防线：
1. 静态：JS 里所有 `$('#id')` / `shown('#id')` 必须能在 BODY 找到同名节点；
2. 运行时：用最小假 DOM 在 node 里真跑一遍 openSheet / fillSheet，
   断言抽屉被打开、未登录时按钮行被隐藏。

用法：py -3 tools/test_aiui_dom.py   （没有 node 时自动跳过第二层）
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AIUI = ROOT / "garmin_cn" / "aiui.py"


def sections() -> tuple[str, str]:
    text = AIUI.read_text(encoding="utf-8")
    body = text.split('BODY = """', 1)[1].split('"""', 1)[0]
    js = text.split('JS = r"""', 1)[1].split('"""', 1)[0]
    return body, js


def static_checks(body: str, js: str) -> list[str]:
    ids = re.findall(r'\bid="([A-Za-z0-9_-]+)"', body)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise AssertionError("BODY 里出现重复 id：" + ", ".join(dup))
    known = set(ids)

    used = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", js))
    used |= set(re.findall(r"\bshown\('#([A-Za-z0-9_-]+)'", js))
    used |= set(re.findall(r"querySelector\('#([A-Za-z0-9_-]+)'\)", js))
    missing = sorted(used - known)
    if missing:
        raise AssertionError("JS 引用了 BODY 里不存在的 id：" + ", ".join(missing))

    # 抽屉必须先亮出来，再填内容；填表出错不能变成「点了没反应」。
    m = re.search(r"function openSheet\(\)\s*\{(.*?)\n\}", js, re.S)
    if not m or "classList.add('on')" not in m.group(1):
        raise AssertionError("openSheet 没有先把 #sheet 显示出来")
    body_of = m.group(1)
    if body_of.index("classList.add('on')") > body_of.index("fillSheet"):
        raise AssertionError("openSheet 里 fillSheet 必须排在显示抽屉之后")

    # v0.4.5：平台地址不该再出现在主界面，只能藏在默认隐藏的「自定义」容器里
    if 'id="platBase"' in body and 'id="platCustomBox" style="display:none"' not in body:
        raise AssertionError("平台地址输入框必须放在默认隐藏的自定义容器里")
    m_api = re.search(r"async function api\(.*?\n\}", js, re.S)
    if not m_api or "kind: 'net'" not in m_api.group(0):
        raise AssertionError("api() 必须把 fetch 网络异常翻译成 net 错误，不能静默卡住")
    if "platform_auto" not in js:
        raise AssertionError("collect() 必须提交 platform_auto，否则切不回官方服务")

    return [
        f"√ BODY {len(known)} 个 id，无重复",
        f"√ JS 引用的 {len(used)} 个 id 全部存在",
        "√ openSheet 先显示抽屉再填内容",
        "√ 平台地址只存在于默认隐藏的自定义容器",
        "√ api() 捕获网络异常、collect() 带 platform_auto",
    ]


HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
function klass() {
  return {_s:new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
    contains(c){return this._s.has(c)},
    toggle(c,on){if(on===undefined)on=!this.contains(c); on?this.add(c):this.remove(c)}};
}
function node(id) {
  return {id:id||'', style:{}, dataset:{}, classList:klass(), value:'', textContent:'',
    innerHTML:'', placeholder:'', options:[], disabled:false, rows:1, scrollHeight:10,
    firstChild:null, onclick:null, onchange:null, oninput:null, onkeydown:null,
    appendChild(c){if(!this.firstChild)this.firstChild=c; return c;},
    replaceChildren(){this.firstChild=node()}, remove(){}, focus(){}, setAttribute(){},
    addEventListener(){}, querySelector(){return node()}, querySelectorAll(){return []}};
}
const byId = {};
const doc = {
  querySelector(sel){
    const m = /^#([\w-]+)$/.exec(sel);
    if (m) { if (!byId[m[1]]) byId[m[1]] = node(m[1]); return byId[m[1]]; }
    return node();
  },
  querySelectorAll(){return []},
  createElement(){return node()}, createElementNS(){return node()},
  createDocumentFragment(){return node()}
};
const cfg = {ok:true, configured:true, mode:'own', providers:[], provider:'deepseek'};
const box = {
  document: doc, location:{hash:''}, history:{replaceState(){}},
  console:{log(){}, error(){}},
  fetch:()=>Promise.resolve({status:200, json:()=>Promise.resolve(cfg)}),
  setTimeout:()=>0, clearTimeout(){}, setInterval:()=>0, clearInterval(){},
  Date, Number, Math, JSON, String, Array, Object, Promise, RegExp, Error,
  isNaN, parseInt, parseFloat
};
box.globalThis = box;
vm.runInNewContext(src + '\n;globalThis.__T={openSheet,fillSheet,collect,STATE};', box);
const T = box.__T, sheet = doc.querySelector('#sheet'), main = doc.querySelector('#mainAct');

T.STATE.config = Object.assign({}, cfg, {mode:'platform', has_platform_token:false});
T.openSheet();
if (!sheet.classList.contains('on')) throw new Error('平台未登录：抽屉没有打开');
if (main.style.display !== 'none') throw new Error('平台未登录：测试/保存行应隐藏');
if (doc.querySelector('#platCustomBox').style.display !== 'none')
  throw new Error('官方服务：自定义地址框默认应隐藏');
if (doc.querySelector('#platCustom').checked !== false)
  throw new Error('官方服务：自定义开关默认应关闭');
if (!/无需填写地址/.test(doc.querySelector('#platService').textContent))
  throw new Error('官方服务：应明确提示无需填写地址');
const p1 = T.collect();
if (p1.platform_auto !== true || 'platform_base' in p1)
  throw new Error('官方服务：collect 不应提交平台地址，且 platform_auto 应为 true');

T.STATE.config = Object.assign({}, cfg, {mode:'own', has_platform_token:false});
T.openSheet();
if (main.style.display === 'none') throw new Error('自配 API：测试/保存行应显示');
if (doc.querySelector('#platLogin').style.display === 'none')
  throw new Error('自配 API 模式下登录区不应被状态逻辑误隐藏');

T.STATE.config = Object.assign({}, cfg, {mode:'platform', has_platform_token:true});
T.openSheet();
if (doc.querySelector('#platLogin').style.display !== 'none')
  throw new Error('已登录：三步登录区应隐藏');
if (doc.querySelector('#platAcct').style.display === 'none')
  throw new Error('已登录：账户面板应显示');
if (main.style.display === 'none') throw new Error('已登录：测试/保存行应显示');

/* 自定义地址：勾上开关后 collect 才提交地址，且 platform_auto=false */
const cb = doc.querySelector('#platCustom');
cb.checked = true;
doc.querySelector('#platBase').value = 'https://self.example.com';
const p2 = T.collect();
if (p2.platform_auto !== false || p2.platform_base !== 'https://self.example.com')
  throw new Error('自定义模式：collect 应提交自定义地址并关闭自动模式');
console.log('RUNTIME_OK');
"""


def runtime_check(js: str) -> str:
    node = shutil.which("node")
    if not node:
        return "· 未装 node，跳过运行时检查"
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "page.js").write_text(js, encoding="utf-8", newline="\n")
        (d / "harness.js").write_text(HARNESS, encoding="utf-8", newline="\n")
        p = subprocess.run(
            [node, str(d / "harness.js"), str(d / "page.js")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if p.returncode:
            raise AssertionError("运行时检查失败：\n" + (p.stderr or p.stdout).strip())
        out = (p.stdout or "").strip()
        if "RUNTIME_OK" not in out:
            raise AssertionError("运行时检查没有返回预期结果：" + out)
    return "√ 假 DOM 真跑 openSheet：抽屉打开 + 两态按钮行显隐正确"


def main() -> int:
    body, js = sections()
    try:
        for line in static_checks(body, js):
            print(line)
        print(runtime_check(js))
    except AssertionError as e:
        print("× " + str(e), file=sys.stderr)
        return 1
    print("√ AI 教练页面回归全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
