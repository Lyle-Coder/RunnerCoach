# -*- coding: utf-8 -*-
"""构建正文用的中文衬线字体（子集化）。

为什么要自己构建
----------------
远征的叙事正文用衬线，数据用无衬线——这是「故事」与「仪表盘」最省力的区分。
但 Android 的 WebView 里 `font-family: serif` 对中文基本没有可用字体，
最终会 fallback 到无衬线，区分就失效了。所以必须内嵌。

内嵌中文体的唯一问题是体积。全量思源宋体 24 MB，直接打进 APK 不可能。
子集化是标准解法，但要选对字符集：

* **只按当前剧情子集化**（701 字，约 300 KB）——听着诱人，但一加新事件就出方框。
* **按 GB2312 一级**（3755 字，覆盖日常 99.7%）——加内容不会缺字，代价是大约两倍体积。

选后者，再叠加剧情实际用字兜底（「垭」「砾」这类一级之外的必须补上，否则
正文里会出现方框——这种 bug 上线后才会有人报）。

字体来源
--------
`C:\\Windows\\Fonts\\NotoSerifSC-VF.ttf`（思源宋体，SIL Open Font License 1.1，
可商用、可嵌入、可再分发）。**不要**用系统自带的宋体（SimSun/STSong）——
那是商业字体，打进 APK 是侵权。

用法
----
    python tools/build_font.py

产物：`assets/fonts/expedition-serif.woff2`

**加剧情之后要重跑一次**。忘了跑的表现是新增正文里个别字变成方框。
`--check` 只做覆盖检查（不重建），可以挂进构建流程。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_FONT = Path(r"C:\Windows\Fonts\NotoSerifSC-VF.ttf")
# 产物放 server/assets/：字体**不再进包**，由 /v1/content 按需下发。
# 以前它跟着客户端走，1.6 MB 里还都是没压过的 TTF（见下面 flavor 那段）。
OUT_DIR = ROOT / "server" / "assets"
OUT_FONT = OUT_DIR / "expedition-serif.woff2"
POOL = ROOT / "server" / "content" / "expedition_events.json"

WEIGHT = 400          # 正文只用常规体；标题走无衬线，不需要第二个字重


def base_charset() -> set[str]:
    """GB2312 一级汉字 + 标点 + ASCII。

    直接从 GB2312 编码反解，不依赖外部字表文件——少一个能丢的依赖。
    """
    chars: set[str] = set()
    for hi in range(0xB0, 0xD8):                 # 一级汉字 3755
        for lo in range(0xA1, 0xFF):
            try:
                chars.add(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    for hi in (0xA1, 0xA3, 0xA4, 0xA5, 0xA8, 0xA9):   # 标点与符号区
        for lo in range(0xA1, 0xFF):
            try:
                chars.add(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    chars |= {chr(c) for c in range(0x20, 0x7F)}
    return chars


def story_text() -> str:
    """事件池里所有会显示给用户的中文。"""
    if not POOL.is_file():
        return ""
    pool = json.loads(POOL.read_text(encoding="utf-8"))
    out = []
    for e in pool.get("events") or []:
        for k in ("title", "summary", "text"):
            out.append(str(e.get(k) or ""))
        for o in e.get("options") or []:
            out.append(str(o.get("label") or ""))
            out.append(str(o.get("brief") or ""))
        for v in (e.get("result_brief") or {}).values():
            out.append(str(v))
        q = e.get("quality") or {}
        out.append(str(q.get("desc") or ""))
        out.append(str(q.get("hint") or ""))
    return "".join(out)


def check_coverage(font_path: Path, text: str) -> list[str]:
    """检查字体是否覆盖文案里的每个字。返回缺字的列表。"""
    from fontTools.ttLib import TTFont

    f = TTFont(font_path, lazy=True)
    cmap = set()
    for table in f["cmap"].tables:
        cmap |= set(table.cmap.keys())
    f.close()
    need = {c for c in text if c.strip()}
    return sorted(c for c in need if ord(c) not in cmap)


def build() -> int:
    if not SRC_FONT.is_file():
        print(f"找不到源字体：{SRC_FONT}")
        print("需要思源宋体（Noto Serif SC，OFL 协议）。不要用系统自带宋体——版权问题。")
        return 1

    from fontTools.subset import Options, Subsetter
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    text = story_text()
    chars = base_charset() | {c for c in text if c.strip()}
    print(f"字符集 {len(chars)} 个（一级 3755 + 标点 + ASCII + 剧情补充）")

    font = TTFont(SRC_FONT)
    if "fvar" in font:
        font = instantiateVariableFont(font, {"wght": WEIGHT}, inplace=True, updateFontNames=True)
        print(f"可变字体已固定到 wght={WEIGHT}")

    opts = Options()
    opts.flavor = "woff2"
    opts.desubroutinize = True
    # 中文正文用不到连字/上下文替换，也别留 glyph 名。实测这几项只省 8 KB，
    # 但既然不用就该去掉。
    opts.layout_features = []
    opts.glyph_names = False
    opts.hinting = False
    opts.drop_tables += ["DSIG", "Meta", "SVG ", "GSUB", "GPOS", "GDEF",
                         "post", "vhea", "vmtx"]

    sub = Subsetter(options=opts)
    sub.populate(text="".join(sorted(chars)))
    sub.subset(font)

    # 压缩格式必须设在 font 上，不能只写在 subsetter 的 Options 里：
    # `opts.flavor` 只影响子集化过程，`font.save()` 看的是 `font.flavor`。
    # 之前漏了这一行，产物其实是**未压缩的 TTF**（魔数 00 01 00 00），
    # 却顶着 .woff2 的名字、用 format("woff2") 声明——浏览器解码失败会
    # 静默回落到系统衬线，页面看着「还行」，其实字体从来没生效过。
    font.flavor = "woff2"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    font.save(OUT_FONT)
    font.close()

    size = OUT_FONT.stat().st_size
    print(f"写出 {OUT_FONT.relative_to(ROOT)}：{size / 1024:.0f} KB")

    missing = check_coverage(OUT_FONT, text)
    if missing:
        print(f"[x] 剧情里有 {len(missing)} 个字不在字体里：{''.join(missing)}")
        return 1
    print("[ok] 剧情用字全部覆盖")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="构建远征正文衬线字体")
    ap.add_argument("--check", action="store_true", help="只检查覆盖，不重建")
    args = ap.parse_args()

    if args.check:
        if not OUT_FONT.is_file():
            print(f"字体还没构建：{OUT_FONT}")
            return 1
        text = story_text()
        missing = check_coverage(OUT_FONT, text)
        if missing:
            print(f"[x] {len(missing)} 个字不在字体里，需要重跑 build_font.py：")
            print("   ", "".join(missing))
            return 1
        print(f"[ok] 字体覆盖全部剧情用字（{OUT_FONT.stat().st_size / 1024:.0f} KB）")
        return 0
    return build()


if __name__ == "__main__":
    sys.exit(main())
