"""文案体检：找出界面上的「AI 味」表达。

    python tools/text_audit.py            # 列命中
    python tools/text_audit.py --file X   # 只看一个文件

只看**字符串字面量**（docstring 与注释不算），因为只有它们会渲染给用户。
规则是经验性的：破折号解释、不是…而是、说白了、怎么来的、一句话……
不是为了「一律不许」，而是把需要人看一遍的地方摆出来。
"""
from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RULES = [
    ("破折号解释", r"——"),
    ("不是…而是", r"不是[^。；]{0,30}而是"),
    ("说白了/其实/本质上", r"说白了|实际上|本质上|换句话说|一句话"),
    ("怎么来的/是怎么回事", r"怎么来的|是怎么|是怎么回事|何为"),
    ("这件事/这一点", r"这件事|这一点|这类|这种|这是个"),
    ("因为/所以", r"因为|所以"),
    ("对…来说", r"对[^。；]{0,12}来说"),
    ("请/注意", r"请注意|请先|务必"),
    ("自问自答", r"？[^<]{0,20}。"),
]

SKIP = ("__doc__",)


def _is_docstring(node: ast.Expression) -> bool:
    v = node.value
    return isinstance(v, ast.Constant) and isinstance(v.value, str)


def scan(path: Path) -> list[tuple[int, str, list[str]]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and _is_docstring(body[0]):
                docs.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docs:
            continue
        s = node.value
        if not re.search(r"[\u4e00-\u9fa5]", s):
            continue
        # 整段 HTML 里含中文才算；纯代码片段跳过
        # 只有渲染给用户看的文本才看：SQL / CSS / 纯代码跳过
        if re.search(r"CREATE TABLE|SELECT |INSERT |UPDATE |box-sizing|@media", s):
            continue
        hits = [name for name, pat in RULES if re.search(pat, s)]
        if hits:
            out.append((node.lineno, s, hits))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    a = ap.parse_args()
    files = ([Path(a.file)] if a.file
             else sorted(p for p in (ROOT / "garmin_cn").glob("*.py"))
             + sorted(p for p in (ROOT / "server").glob("*.py")))
    total = 0
    for f in files:
        try:
            rows = scan(f)
        except SyntaxError:
            continue
        if not rows:
            continue
        print(f"\n== {f.relative_to(ROOT)}  {len(rows)} 处")
        for ln, s, hits in rows:
            txt = re.sub(r"\s+", " ", s).strip()
            print(f"  {ln:5d} [{'/'.join(hits)}] {txt[:110]}")
            total += 1
    print(f"\n合计 {total} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
