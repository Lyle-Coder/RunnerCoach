# -*- coding: utf-8 -*-
"""赛事数据的检查与更新。

为什么要有这个脚本
------------------
「只展示报名窗口内的赛事」这句话，落到数据上就两个字段：`reg_open` /
`reg_close`。而这两个值没有统一接口可拿（见 docs/赛事数据源与自动更新.md），
所以这里做成「少量赛事 + 两个 adapter」：

  manual  窗口由人填，脚本只做归一化与校验（默认，最可靠）
  page    抓官方页面，按关键词（报名/截止）+ 日期找窗口；两个日期都找到才采信

用法
----
    python tools/races_sync.py --check      # 只看现状：过期、缺字段、多久没核对
    python tools/races_sync.py --dry-run    # 打印将要发生的变化，不落盘
    python tools/races_sync.py              # 抓取 + 校验 + 原子写入

输入  data/races_sources.json（模板见 tools/races_sources.example.json）
输出  data/content/races.json（App 优先读这份，覆盖包内基线）

任何一条抓失败 / 解析不出窗口，都写 `unknown` 而不是猜——App 侧看到
`unknown` 就不展示，宁可比实际少一场。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 直接 `python tools/races_sync.py` 时 cwd 未必是项目根，解析器在包内
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_SOURCES = ROOT / "data" / "races_sources.json"
DEFAULT_OUT = ROOT / "data" / "content" / "races.json"

UA = "RunnerCoach-races/1.0 (+local; contact: app owner)"
TIMEOUT = 10

# 日期：2026-11-20 / 2026.11.20 / 2026/11/20 / 11月20日 / 11-20
_DT = (
    re.compile(r"(20\d{2})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})\s*日?"),
    re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
)
_OPEN_WORDS = ("报名开始", "报名开启", "开放报名", "开始报名", "报名时间")
_CLOSE_WORDS = ("报名截止", "截止时间", "截至", "报名结束", "关闭报名")


def _dates(text: str, year: int) -> list[dt.date]:
    out = []
    for rx in _DT:
        for m in rx.finditer(text):
            g = m.groups()
            try:
                if len(g) == 3:
                    out.append(dt.date(int(g[0]), int(g[1]), int(g[2])))
                else:
                    out.append(dt.date(year, int(g[0]), int(g[1])))
            except ValueError:
                continue
    return out


def parse_window(text: str, year: int) -> tuple[dt.date, dt.date] | None:
    """从一段公告文字里找报名窗口。两个都找到才返回，否则 None。

    只认「同一句里有报名关键词 + 日期」的行：公告里日期很多（比赛日、领物、
    检录），跨行配对几乎必错。
    """
    opened = closed = None
    for line in str(text or "").splitlines():
        hits = _dates(line, year)
        if not hits:
            continue
        has_open = any(w in line for w in _OPEN_WORDS)
        has_close = any(w in line for w in _CLOSE_WORDS)
        if has_open and not has_close and len(hits) >= 2 and opened is None:
            # 「报名时间：2026-11-20 至 2026-12-31」——一行给全的最常见
            opened, closed = hits[0], hits[-1]
        elif has_open and not has_close and opened is None:
            opened = hits[0]
        elif has_close and not has_open and closed is None:
            closed = hits[-1]
        elif has_open and has_close and len(hits) >= 2:
            opened, closed = hits[0], hits[-1]
    if opened and closed and closed >= opened:
        return opened, closed
    return None


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read(600_000)
    return raw.decode("utf-8", "replace")


def build_record(src: dict, today: dt.date) -> tuple[dict | None, str]:
    """一条源 → 一条记录。返回 (record, note)；record=None 表示这条不可用。"""
    mode = str(src.get("mode") or "manual").lower()
    name = str(src.get("name") or "").strip()
    if not name:
        return None, "缺 name"
    try:
        race_day = dt.date.fromisoformat(str(src.get("date") or "")[:10])
    except ValueError:
        return None, "比赛日不可解析"

    reg_open = str(src.get("reg_open") or "")[:10] or None
    reg_close = str(src.get("reg_close") or "")[:10] or None
    note = "manual"

    if mode == "page":
        url = str(src.get("page") or "").strip()
        if not re.match(r"^https?://", url):
            return None, "page 模式缺合法 URL"
        try:
            text = fetch(url)
        except Exception as e:                      # noqa: BLE001 网络错误一律降级
            return None, f"抓取失败：{e}"
        win = parse_window(text, race_day.year)
        if win:
            reg_open, reg_close = win[0].isoformat(), win[1].isoformat()
            note = "page"
        else:
            reg_open = reg_close = None
            note = "page 未解析出窗口"
        time.sleep(1)                               # 同域名串行，不压人家服务器

    if not (reg_open and reg_close):
        # 窗口不明：照样写进文件，但 App 不会展示（比瞎填安全）
        return {
            "id": str(src.get("id") or name)[:40], "name": name,
            "date": race_day.isoformat(), "dist_km": src.get("dist_km"),
            "ascent_m": src.get("ascent_m"), "place": src.get("place") or "",
            "reg_open": None, "reg_close": None, "status": "unknown",
            "url": str(src.get("page") or src.get("url") or ""),
            "source": str(src.get("source") or "赛事官方"),
            "checked_at": today.isoformat(), "via": note,
            "note": str(src.get("note") or ""),
        }, note

    try:
        o, c = dt.date.fromisoformat(reg_open), dt.date.fromisoformat(reg_close)
    except ValueError:
        return None, "窗口日期不可解析"
    if c < o:
        return None, "截止早于开始"
    if race_day < c:
        return None, "比赛日早于报名截止"
    status = "open" if o <= today <= c else ("upcoming" if today < o else "closed")
    return {
        "id": str(src.get("id") or name)[:40], "name": name,
        "date": race_day.isoformat(), "dist_km": src.get("dist_km"),
        "ascent_m": src.get("ascent_m"), "place": src.get("place") or "",
        "reg_open": reg_open, "reg_close": reg_close, "status": status,
        "url": str(src.get("page") or src.get("url") or ""),
        "source": str(src.get("source") or "赛事官方"),
        "checked_at": today.isoformat(), "via": note,
        "note": str(src.get("note") or ""),
    }, note


# ---------------------------------------------------------------- 最酷 zuicool
#
# 解析与抓取放在 garmin_cn/races_fetch.py（App 里也要用同一份），这里只适配
# sources 配置并合并输出。两处各写一遍必然漂移，改一处忘一处最难查。


def fetch_zuicool(src: dict, today: dt.date) -> tuple[list[dict], str]:
    from garmin_cn import races_fetch as RF

    rows, st = RF.fetch(pages=int(src.get("pages") or RF.PAGES),
                        per_page=int(src.get("per_page") or RF.PER_PAGE),
                        today=today)
    msg = (f"zuicool: {len(rows)} 场在报名中（跳过 {st['upcoming']} 场未开报 / "
           f"{st['skipped']} 场解析不了，抓了 {st['pages']} 页）")
    if st.get("error"):
        msg += "；" + str(st["error"])
    return rows, msg


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cmd_check(out: Path) -> int:
    data = load_json(out, {})
    items = data.get("items") or []
    if not items:
        print(f"· {out} 还没有数据（先跑一次 races_sync.py）")
        return 0
    today = dt.date.today()
    print(f"· {out}  version={data.get('version')}  updated={data.get('updated')}"
          f"  共 {len(items)} 场")

    expired, unknown, stale, ok = [], [], [], 0
    for it in items:
        try:
            close = dt.date.fromisoformat(str(it.get("reg_close") or "")[:10])
        except ValueError:
            unknown.append(it)
            continue
        if close < today:
            expired.append(it)
            continue
        ok += 1
        try:
            age = (today - dt.date.fromisoformat(str(it.get("checked_at") or "")[:10])).days
        except ValueError:
            age = 999
        if age > 7:
            stale.append((it, age))

    print(f"· 报名中 {ok} 场 · 已过期 {len(expired)} · 窗口不明 {len(unknown)}"
          f" · 超 7 天未核对 {len(stale)}")
    # 只列需要看的，不把 165 场全刷一遍
    for it in expired[:5]:
        print(f"  [过期] {it.get('reg_close')} {it.get('name')}")
    if len(expired) > 5:
        print(f"  [过期] …另有 {len(expired) - 5} 场")
    for it in unknown[:5]:
        print(f"  [窗口不明] {it.get('name')}")
    for it, age in stale[:5]:
        print(f"  [{age} 天未核对] {it.get('name')}")
    if expired or unknown:
        print("\n  跑 `--prune` 可以把过期/坏窗口的条目从文件里清掉")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="赛事数据检查与更新")
    ap.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="只检查现有输出")
    ap.add_argument("--prune", action="store_true",
                    help="只清理：删掉已出报名窗口的条目，不联网")
    ap.add_argument("--dry-run", action="store_true", help="不写盘，只打印变化")
    args = ap.parse_args(argv)

    if args.check:
        return cmd_check(args.out)

    if args.prune:
        from garmin_cn import races_fetch as RF

        old_out = RF.OUT
        try:
            RF.OUT = args.out
            r = RF.prune()
        finally:
            RF.OUT = old_out
        if not r.get("ok"):
            print(f"× 清理失败：{r.get('error')}")
            return 1
        if not r.get("removed"):
            print(f"· 没有过期的条目（保留 {r.get('kept', 0)} 场）")
        else:
            print(f"√ 清掉 {r['removed']} 场过期条目，保留 {r['kept']} 场")
        return 0

    srcs = load_json(args.sources, {}).get("sources") or []
    if not srcs:
        print(f"× 没有可用的源：{args.sources}\n"
              f"  照着 tools/races_sources.example.json 建一份再来。")
        return 2

    today = dt.date.today()
    records, problems, failed_via = [], [], set()
    for src in srcs:
        if not isinstance(src, dict):
            continue
        if str(src.get("mode") or "").lower() == "zuicool":
            rows, msg = fetch_zuicool(src, today)
            print(f"  {'✓' if rows else '×'} {msg}")
            if rows:
                records.extend(rows)
            else:
                # 一条都没拿到：别让「抓取失败」变成「赛事全被删掉」
                failed_via.add("zuicool")
                problems.append(msg)
            continue
        rec, note = build_record(src, today)
        if rec:
            records.append(rec)
            print(f"  ✓ {rec['name']}  {rec.get('status')}  ({note})")
        else:
            problems.append(f"{src.get('name') or src.get('id')}: {note}")
            print(f"  × {src.get('name')}: {note}")

    # 两页之间可能重复（运营把同一场挂了两次），按 id 去重，先到的为准
    seen, uniq = set(), []
    for r in records:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    records = uniq

    old = load_json(args.out, {})
    if failed_via:
        kept = [i for i in old.get("items") or []
                if isinstance(i, dict) and i.get("via") in failed_via
                and str(i.get("id")) not in seen]
        if kept:
            records.extend(kept)
            print(f"! {len(kept)} 场沿用上次抓到的数据（这次没抓成功）")

    if not records:
        print("× 一条都没建成，保留旧数据不写盘")
        return 1

    old_by = {str(i.get("id")): i for i in old.get("items") or [] if isinstance(i, dict)}
    added = [r["id"] for r in records if r["id"] not in old_by]
    # 抓来的赛事 reg_open 是用抓取日推的，每天都变；checked_at 同理。
    # 不算进「变化」，否则每次都是「165 场全变了」，等于没报。
    _volatile = {"checked_at", "reg_open"}

    def _sig(r: dict) -> dict:
        # 值为 None 的键视同缺失（不同 adapter 产出的字段集合本来就不一样）
        return {k: v for k, v in r.items() if k not in _volatile and v is not None}

    changed = [r["id"] for r in records
               if r["id"] in old_by and _sig(r) != _sig(old_by[r["id"]])]
    gone = [i for i in old_by if i not in {r["id"] for r in records}]
    print(f"\n新增 {len(added)} · 变化 {len(changed)} · 源里已移除 {len(gone)}")
    if changed:
        print("  变化：" + ", ".join(changed))
    if gone:
        print("  移除：" + ", ".join(gone) + "（源里没有这条了，输出里也不再保留）")
    if problems:
        print("  未建成：" + "；".join(problems))

    data = {"version": int(old.get("version") or 0) + 1,
            "updated": today.isoformat(), "items": records}
    if args.dry_run:
        print("\n· dry-run：没有写盘")
        return 0
    write_atomic(args.out, data)
    print(f"\n√ 已写入 {args.out}（{len(records)} 场，version={data['version']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
