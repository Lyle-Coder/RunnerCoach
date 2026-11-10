"""间歇训练展示：判定权交给佳明的训练模式，不再靠速度猜。

为什么改
--------
以前用「快慢交替」的速度模式判定间歇，问题是有快慢交替的匀速长跑（等红灯、
爬坡、起跑热身与结束放松）会被误判成间歇，而真正的间歇课里若组间恢复是
原地站立，速度曲线又可能判不出来。真机对比 63 条跑步明细：速度判定多认了
10 条佳明未标记间歇的活动。

现在的口径
----------
佳明活动自带 `hasIntensityIntervals`——设备/训练课执行时写入的「本次训练
包含强度间歇」标记，是官方口径，等价于「训练里设置了间歇」。规则只有一条：

* 标记为间歇 → 用速度把全程切成快 / 慢两组，走底部分双栏版式；
* 没标记 → 一律默认版式（分圈表），即使速度看起来快慢交替也不切。

也就是说，这里仍然需要「怎么切」，但不再需要「猜是不是间歇」。切不出
快慢两组（全程匀速）时返回 `None`，退回默认版式——空的双栏没有意义。

纯标准库、零 IO。输入是 `activity.py` 产出的明细，输出是给活动图底部
双栏用的纯数据。
"""
from __future__ import annotations

# 以下门槛只用于「切得干不干净」（短段并回、快慢差不至于太小），
# 不再用于判断「是不是间歇」——那由佳明标记决定。
MIN_SEG_S = 20.0
MIN_SEG_M = 80.0
# 慢段同样要有实际长度。只按时长判、不按距离判：真实的组间恢复常常是
# 原地站立（dist≈0），用距离过滤会把它们全删掉。
MIN_SLOW_SEG_S = 15.0
# 快组平均速度 / 慢组平均速度 的下限（逐点序列）
MIN_RATIO = 1.25
# 分圈是每公里平均值，快慢早已被抹平，判定更粗糙；作为兜底路径宁可更严，
# 免得把「几圈爬坡 / 等红灯」当成间歇。
MIN_RATIO_LAPS = 1.35
MIN_SETS = 3
MIN_SWITCHES = 5
# 慢段（含组间恢复）占整段时长的下限
MIN_SLOW_SHARE = 0.15
# Otsu 两组均值的相对落差下限：(hi - lo) / hi
MIN_SPLIT_GAP = 0.18
# 标记模式下也要求的最低落差：已确认是间歇课，但全程匀速（GPS 抖动只有
# 百分之几）时切出来的「快慢」是假的，宁可不显示双栏。
MIN_FORCED_GAP = 0.05
# 每栏最多列几行（常见的 400 m 间歇是 12~15 组，行距压紧后能全部列全），
# 再多才折叠成「其余 N 组」。
MAX_GROUPS = 16
# 强度带（快/中/慢）最多分几档。真实的间歇课超过四档就只剩「配速曲线」可看，
# 再切档对读者没有意义。
BAND_MAX = 4
# 相邻两档的相对速度差下限：低于它就算不上「不同的档」，不拆。
# 8% 大致是「同一次课里两个不同配速目标」的最小可辨差异。
MIN_BAND_GAP = 0.08
# 拆分后每档至少几段（含 2 档时）。一段的「档」只是噪声。
BAND_MIN_SETS = 2
# 低于这个速度视为站立/走路型恢复；慢跑恢复仍算恢复，只是展示配速。
REST_SPD = 1.2
# 间歇段的「组距」离散度：低于它就认为所有组同一距离（15×400 m 这种），
# 只出一行「间歇」，不因配速自然漂移（负分段、后程掉速）切出快/慢档。
WORK_DIST_TOL = 0.2

BAND_NAMES = {2: ("快", "慢"), 3: ("快", "中", "慢"),
              4: ("快", "中快", "中慢", "慢")}


def _split(values: list[float]) -> tuple[float, float, float]:
    """一维 Otsu：返回 (阈值, 低组均值, 高组均值)。样本不足时退化为中位数。"""
    s = sorted(v for v in values if v is not None and v > 0)
    n = len(s)
    if n < 4:
        m = s[n // 2] if s else 0.0
        return m, m, m
    prefix = [0.0]
    for v in s:
        prefix.append(prefix[-1] + v)
    best, k = -1.0, n // 2
    for i in range(2, n - 1):
        m0 = prefix[i] / i
        m1 = (prefix[n] - prefix[i]) / (n - i)
        var = i * (n - i) * (m0 - m1) ** 2
        if var > best:
            best, k = var, i
    thr = (s[k - 1] + s[k]) / 2
    return thr, prefix[k] / k, (prefix[n] - prefix[k]) / (n - k)


def _wmean(vals: list[float], weights: list[float]) -> float | None:
    pairs = [(v, w) for v, w in zip(vals, weights) if v and w and w > 0]
    if not pairs:
        return None
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw if tw else None


def _runs(flags: list[bool]) -> list[tuple[bool, int, int]]:
    """连续同值段 → [(值, 起, 止)]，止为开区间。"""
    out: list[tuple[bool, int, int]] = []
    i = 0
    while i < len(flags):
        j = i + 1
        while j < len(flags) and flags[j] == flags[i]:
            j += 1
        out.append((flags[i], i, j))
        i = j
    return out


def _seg(chunk: list[dict], fast: bool, smooth: list[float] | None = None,
         a: int = 0, b: int = 0) -> dict | None:
    """把一段连续采样点折成一行展示数据。距离用速度梯形积分，不依赖累积距离字段。"""
    if len(chunk) < 2:
        return None
    dur = (chunk[-1].get("t") or 0) - (chunk[0].get("t") or 0)
    if dur <= 0:
        return None
    dist = 0.0
    for i in range(1, len(chunk)):
        dt = (chunk[i].get("t") or 0) - (chunk[i - 1].get("t") or 0)
        if dt <= 0:
            continue
        s0 = chunk[i - 1].get("spd") or 0.0
        s1 = chunk[i].get("spd") or 0.0
        dist += (s0 + s1) / 2 * dt
    spd = dist / dur
    if smooth and b > a:
        spd = sum(smooth[a:b]) / (b - a) or spd
    hrs = [p.get("hr") for p in chunk if p.get("hr")]
    cads = [p.get("cad") for p in chunk if p.get("cad")]
    return {
        "fast": fast,
        "t0": chunk[0].get("t") or 0,     # 时序结构条按它排序
        "dist_m": round(dist, 1),
        "dur_s": round(dur, 1),
        "spd": spd,
        "pace_s": round(1000 / spd) if spd > 0.5 else None,
        "hr_avg": round(sum(hrs) / len(hrs)) if hrs else None,
        "cad": round(sum(cads) / len(cads), 1) if cads else None,
    }


def _agg(segs: list[dict], fast: bool) -> dict:
    picked = [s for s in segs if s["fast"] is fast]
    dist = sum(s["dist_m"] for s in picked)
    dur = sum(s["dur_s"] for s in picked)
    avg_spd = dist / dur if dur > 0 else 0.0
    pace = round(dur / (dist / 1000)) if dist > 50 else None
    return {
        "sets": len(picked),
        "dist_m": round(dist, 1),
        "dur_s": round(dur, 1),
        "pace_s": pace,
        "spd": round(avg_spd, 2),
        # 组间是原地站立/走动的恢复时，配速会变成几十分钟每公里，
        # 这个数字没有意义；展示层应改用「平均恢复时长」。
        "rest": bool(avg_spd < 1.2 or (pace or 0) > 600),
        "recover_s": round(dur / len(picked)) if picked else None,
        "hr_avg": _wmean([s["hr_avg"] or 0 for s in picked],
                         [s["dur_s"] for s in picked]),
        "cad": _wmean([s["cad"] or 0 for s in picked],
                      [s["dur_s"] for s in picked]),
        "segs": picked[:MAX_GROUPS],
        "omitted": max(0, len(picked) - MAX_GROUPS),
    }


def _kmeans(values: list[float], weights: list[float], k: int,
            iters: int = 12) -> tuple[list[int], list[float]]:
    """一维加权 k-means（分位数初始化，确定性；不用随机种子，回归可复现）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    centers = [values[order[min(len(order) - 1,
                               int((j + 0.5) * len(order) / k))]] for j in range(k)]
    assign = [0] * len(values)
    for _ in range(iters):
        changed = False
        for i, v in enumerate(values):
            best = min(range(k), key=lambda c: abs(v - centers[c]))
            if best != assign[i]:
                assign[i], changed = best, True
        for c in range(k):
            tw = sum(weights[i] for i, a in enumerate(assign) if a == c)
            if tw > 0:
                centers[c] = sum(values[i] * weights[i]
                                 for i, a in enumerate(assign) if a == c) / tw
        if not changed:
            break
    return assign, centers


def _pack(label: str, picked: list[dict], rest: bool = False) -> dict:
    """把一组分段折成一行汇总（队/恢复通用）。"""
    dist = sum(s["dist_m"] for s in picked)
    dur = sum(s["dur_s"] for s in picked)
    spd = dist / dur if dur else 0.0
    return {
        "label": label, "sets": len(picked),
        "dist_m": round(dist, 1), "dur_s": round(dur, 1),
        "spd": spd,
        "pace_s": round(dur / (dist / 1000)) if dist > 50 else None,
        "recover_s": round(dur / len(picked)) if picked else None,
        "rest": rest,
        # 慢跑恢复（spd 不算静止）展示配速；站桩恢复展示时长
        "jog": bool(rest and spd >= REST_SPD),
        "hr_avg": _wmean([s["hr_avg"] or 0 for s in picked],
                         [s["dur_s"] for s in picked]),
        "cad": _wmean([s["cad"] or 0 for s in picked],
                      [s["dur_s"] for s in picked]),
        "segs": picked,
    }


def work_rest(order: list[dict] | None) -> dict | None:
    """按时序把段拆成「间歇段 / 恢复段」，恢复**不按速度高低判定**。

    为什么不能靠速度分档
    --------------------
    8×800 m + 400 m 慢跑恢复是全马备赛最常见的课，恢复段 5'30"/km 很容易
    被聚成「慢组」，于是一个标准间歇被读成「快组 / 慢组」两种强度——这是
    语义错误：恢复就是恢复，慢跑恢复也不构成一个强度档。分档只该用在
    **间歇段之间配速不同**的课（金字塔、变速、法特莱克）。

    判据是结构：段必须「快-慢-快-慢…」交替，且奇数位（恢复）确实慢于
    偶数位。交替不成立（三/四段课）时返回 None，交回 band_split 处理。
    """
    segs = [o for o in (order or []) if isinstance(o, dict)]
    if len(segs) < 4:
        return None
    work: list[dict] = []
    rest: list[dict] = []
    k = 0
    while k + 1 < len(segs):
        a, b = segs[k], segs[k + 1]
        if (a.get("spd") or 0) <= (b.get("spd") or 0):
            return None
        work.append(a)
        rest.append(b)
        k += 2
    if k < len(segs) and (segs[k].get("spd") or 0) > (rest[-1].get("spd") or 0):
        work.append(segs[k])        # 末尾多跑了一组、没跟恢复
    if len(work) < 3 or len(rest) < 2:
        return None
    return {"work": work, "rest": rest}


def attach_work_rest(order: list[dict]) -> tuple[list[dict], list[dict]] | None:
    """交替结构 → (bands, order_with_band)。标准间歇只给「间歇 / 恢复」两行。"""
    wr = work_rest(order)
    if not wr:
        return None
    work, rest = wr["work"], wr["rest"]
    # **分档的判据是「组距不同」而不是「配速不同」**：同距多组（15×400 m）
    # 就算配速从 3'45" 漂到 3'16"（负分段、后程顶不住），那也是同一节课的
    # 体力分配，不是两种强度；只有 400/200/200、400/800/1200 这种组距本身
    # 不同的课（金字塔、阶梯、变速）才需要在间歇段内再分档。
    dists = [s["dist_m"] for s in work if (s.get("dist_m") or 0) > 0]
    d_spread = ((max(dists) - min(dists)) / min(dists)
                if dists and min(dists) > 0 else 0.0)
    work_bands: list[dict] = []
    band_of: dict[int, int] = {}
    if d_spread > WORK_DIST_TOL:
        sub = band_split(work, kmax=3)
        if sub:
            work_bands = sub["bands"]
            for i, b in enumerate(work_bands):
                for sg in b["segs"]:
                    band_of[id(sg)] = i
    if not work_bands:
        work_bands = [_pack("间歇", work)]
        band_of = {id(s): 0 for s in work}
    rest_band = _pack("恢复", rest, True)
    bands = work_bands + [rest_band]
    rest_idx = len(bands) - 1
    ordered = sorted(order, key=lambda o: o.get("t0") or 0)
    out = []
    for o in ordered:
        is_rest = o in rest
        out.append({**o, "rest": is_rest,
                    "band": rest_idx if is_rest else band_of.get(id(o), 0)})
    return bands, out


def band_split(segs: list[dict] | None, kmax: int = BAND_MAX) -> dict | None:
    """把分段按速度聚成 2~kmax 档强度带（快 / 中 / 慢…）。

    为什么不是「快慢两组」
    ----------------------
    两组是 Otsu 二分的结果，遇到三段课（400 m 快 + 200 m 中 + 200 m 慢，
    金字塔、变速跑、法特莱克）时中段会被硬塞进快或慢，结构就丢了。
    这里按**分段速度**做一维加权 k-means，k 从 2 试到 kmax，只接受
    「每档≥2 段」且「相邻档相对差 ≥8%」的最大 k——多出来的档必须真的
    分得开，否则退回少一档，保证不会把匀速课切出一堆假档。

    `segs` 里的休息段（spd < REST_SPD）单独成「恢复」档，不参与聚类：
    原地站着没有「配速档」可言。
    """
    segs = [s for s in (segs or []) if (s.get("dur_s") or 0) >= 5]
    if len(segs) < 4:
        return None
    # 用下标而不是 dict 本身做标识：内容相同的两段是相等的，按值查找会串位。
    moving = [(i, s) for i, s in enumerate(segs)
              if (s.get("spd") or 0) >= REST_SPD]
    if len(moving) < 4:
        return None
    vals = [s["spd"] for _, s in moving]
    wts = [max(s["dur_s"], 1.0) for _, s in moving]

    best: tuple[int, list[int], list[float]] | None = None
    for k in range(2, min(kmax, len(moving) // BAND_MIN_SETS) + 1):
        assign, centers = _kmeans(vals, wts, k)
        counts = [sum(1 for a in assign if a == c) for c in range(k)]
        if min(counts) < BAND_MIN_SETS:
            break
        order_c = sorted(range(k), key=lambda c: -centers[c])   # 快 → 慢
        gap_ok = True
        for j in range(k - 1):
            hi, lo = centers[order_c[j]], centers[order_c[j + 1]]
            if hi <= 0 or (hi - lo) / hi < MIN_BAND_GAP:
                gap_ok = False
                break
        if not gap_ok:
            break
        best = (k, assign, centers)

    if not best:
        # 速度均匀（例如「快跑 + 原地站」只有一种跑动强度）时聚不出两档，
        # 但只要有休息段，结构仍然是「跑动 / 恢复」两段式——直接按这个分，
        # 不要为了凑档数把匀速跑硬劈成快慢两档。
        rest_only = [s for s in segs if (s.get("spd") or 0) < REST_SPD]
        if not rest_only or len(moving) < 3:
            return None
        moving_segs = [s for _, s in moving]
        bands = [_pack("间歇", moving_segs), _pack("恢复", rest_only, True)]
        order = []
        for i in sorted(range(len(segs)), key=lambda i: segs[i].get("t0") or 0):
            s = segs[i]
            is_rest = (s.get("spd") or 0) < REST_SPD
            order.append({**s, "band": 1 if is_rest else 0, "rest": is_rest})
        return {"k": 2, "bands": bands, "order": order}

    k, assign, centers = best
    names = BAND_NAMES.get(k, tuple(f"档{i + 1}" for i in range(k)))
    order_c = sorted(range(k), key=lambda c: -centers[c])
    rank = {c: i for i, c in enumerate(order_c)}

    bands: list[dict] = []
    for c in order_c:
        picked = [s for j, (_, s) in enumerate(moving) if assign[j] == c]
        if picked:
            bands.append(_pack(names[rank[c]] if rank[c] < len(names)
                               else f"档{rank[c] + 1}", picked))
    # 休息段单独一档放最慢：原地站着没有「配速档」可言
    rest = [s for s in segs if (s.get("spd") or 0) < REST_SPD]
    if rest:
        bands.append(_pack("恢复", rest, True))

    # 时序结构条：按开始时间排，每段标出属于哪一档（供 N 档版式画柱）
    seg_band: dict[int, int] = {}
    for j, (i, _) in enumerate(moving):
        seg_band[i] = rank[assign[j]]
    rest_band = len(bands) - 1 if rest else 0
    order = [{**segs[i], "band": seg_band.get(i, rest_band),
              "rest": (segs[i].get("spd") or 0) < REST_SPD}
             for i in sorted(range(len(segs)), key=lambda i: segs[i].get("t0") or 0)]
    return {"k": k, "bands": bands, "order": order}


def _finish(segs: list[dict], source: str, min_ratio: float = MIN_RATIO,
            forced: bool = False) -> dict | None:
    """汇总成快 / 慢两组。

    `forced=True` 表示「佳明已经确认这是间歇课」，这里只负责切得能看：
    必须有快有慢、至少两组快跑、快组确实比慢组快一点。原来的四重门禁
    （组数 / 切换次数 / 慢段占比 / 速度差）是给「靠速度猜间歇」用的，
    标记模式下不再需要——真实的间歇课哪怕恢复段很短也要如实展示。
    """
    fasts = [s for s in segs if s["fast"]]
    slows = [s for s in segs if not s["fast"]]
    if forced:
        if not fasts or not slows or len(fasts) < 2:
            return None
        total = sum(s["dur_s"] for s in segs)
        share = sum(s["dur_s"] for s in slows) / total if total > 0 else 0.0
        vf = _wmean([s["spd"] for s in fasts], [s["dur_s"] for s in fasts]) or 0
        vs = _wmean([s["spd"] for s in slows], [s["dur_s"] for s in slows]) or 0
        if vs <= 0 or vf <= vs:
            return None
        return {"source": source, "sets": len(fasts), "ratio": round(vf / vs, 2),
                "slow_share": round(share, 3), "forced": True,
                "fast": _agg(segs, True), "slow": _agg(segs, False)}
    if len(fasts) < MIN_SETS or len(slows) < MIN_SETS:
        return None
    switches = sum(1 for i in range(1, len(segs))
                   if segs[i]["fast"] != segs[i - 1]["fast"])
    if switches < MIN_SWITCHES:
        return None
    total = sum(s["dur_s"] for s in segs)
    share = sum(s["dur_s"] for s in slows) / total if total > 0 else 0.0
    if share < MIN_SLOW_SHARE:
        return None
    vf = _wmean([s["spd"] for s in fasts], [s["dur_s"] for s in fasts]) or 0
    vs = _wmean([s["spd"] for s in slows], [s["dur_s"] for s in slows]) or 0
    if vs <= 0 or vf / vs < min_ratio:
        return None
    return {
        "source": source,
        "sets": len(fasts),
        "ratio": round(vf / vs, 2),
        "slow_share": round(share, 3),
        "fast": _agg(segs, True),
        "slow": _agg(segs, False),
    }


def _split_ok(lo: float, hi: float) -> bool:
    """Otsu 切出来的两组必须真的分开，而不是把匀速抖动硬切成两半。"""
    return hi > 0 and (hi - lo) / hi >= MIN_SPLIT_GAP


def speed_runs(points: list[dict] | None, tol: float = 0.14,
               min_s: float = 20.0, max_s: float = 300.0) -> list[dict]:
    """把逐点速度按「稳定段」切开，而不是二分快慢。

    为强度分档服务：三段课（快 400 + 中 200 + 慢 200）若先做快慢二分，
    中段会被并进快组、之后再也分不出来。这里以「速度相对当前段均值偏移
    不超过 tol」为界自适应切段，切完再把过短的段并回速度更接近的邻居。
    """
    pts = [p for p in (points or []) if isinstance(p.get("t"), (int, float))]
    if len(pts) < 8:
        return []
    pts.sort(key=lambda p: p["t"])
    smooth: list[float] = []
    for i in range(len(pts)):
        a, b = max(0, i - 2), min(len(pts), i + 3)
        w = [pts[j].get("spd") or 0.0 for j in range(a, b)]
        smooth.append(sum(w) / len(w))

    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(pts):
        j, mean = i + 1, smooth[i]
        while j < len(pts):
            v = smooth[j]
            n = j - i
            if mean > 0.3 and abs(v - mean) / max(mean, 0.5) > tol:
                break
            if (pts[j]["t"] - pts[i]["t"]) > max_s:
                break
            mean = (mean * n + v) / (n + 1)
            j += 1
        spans.append((i, j))
        i = j

    def fold(a: int, b: int) -> dict | None:
        return _seg(pts[a:b], False, smooth, a, b)

    segs = [s for s in (fold(a, b) for a, b in spans) if s]
    # 过短的段（加速过渡、GPS 抖动）并回速度更接近的邻居
    changed = True
    while changed and len(segs) > 2:
        changed = False
        for k, s in enumerate(segs):
            if s["dur_s"] >= min_s:
                continue
            left = segs[k - 1] if k > 0 else None
            right = segs[k + 1] if k + 1 < len(segs) else None
            pick = None
            if left and right:
                pick = k - 1 if abs(left["spd"] - s["spd"]) <= abs(right["spd"] - s["spd"]) else k + 1
            else:
                pick = k - 1 if left else k + 1
            if pick is None:
                break
            a = spans[pick][0] if pick < k else spans[k][0]
            b = spans[k][1] if pick < k else spans[pick][1]
            merged = fold(a, b)
            if not merged:
                break
            spans[min(k, pick):max(k, pick) + 1] = [(a, b)]
            segs[min(k, pick):max(k, pick) + 1] = [merged]
            changed = True
            break
    return segs


def lap_segs(laps: list[dict] | None) -> list[dict]:
    """分圈 → 分段（供强度分档用）。手动计圈的圈就是一组，粒度比逐点粗但更准。"""
    out: list[dict] = []
    for i, l in enumerate(laps or []):
        dist = l.get("dist_m") or 0
        dur = l.get("dur_s") or 0
        if dist <= 0 or dur <= 0:
            continue
        spd = dist / dur
        out.append({"fast": False, "t0": float(i), "dist_m": round(dist, 1),
                    "dur_s": round(dur, 1), "spd": spd,
                    "pace_s": round(dur / (dist / 1000)) if dist > 50 else None,
                    "hr_avg": l.get("hr_avg"), "cad": l.get("cad")})
    return out


def manual_laps(laps: list[dict] | None, lap_m: float | None = None) -> bool:
    """这组圈是不是「手动计圈」（间歇课的组），而不是 1 km 自动圈。"""
    laps = laps or []
    if len(laps) < 4:
        return False
    if lap_m and abs(lap_m - 1000) <= 60:
        return False
    km_like = sum(1 for l in laps if abs((l.get("dist_m") or 0) - 1000) <= 60)
    return km_like < len(laps) * 0.6


def attach_bands(got: dict | None, detail: dict | None,
                 laps: list[dict] | None = None) -> dict | None:
    """给判定结果补 `bands`（N 档强度带）与 `order`（时序结构条）。

    分档优先用手动计圈：圈就是一组，快/中/慢三段课的各段速度不会被
    「二分阈值」抹平。没有手动计圈时退回快慢两组的段序列（此时其实只有
    两档，双栏版式仍适用）。
    """
    if not got:
        return got
    d = detail or {}
    src_laps = laps if laps is not None else (d.get("laps") or [])
    segs = None
    if manual_laps(src_laps, d.get("lap_distance_m")):
        segs = lap_segs(src_laps)
    if not segs:
        # 逐点数据按「速度稳定段」重切：不能复用快慢二分的结果，
        # 否则三段课的中段在二分时就被并进快组，永远分不出「中」。
        segs = speed_runs(((d.get("series") or {}).get("points")))
    if not segs:
        segs = ([dict(s) for s in got["fast"]["segs"]]
                + [dict(s) for s in got["slow"]["segs"]])
    ordered = sorted(segs, key=lambda s: s.get("t0") or 0)
    # 先看有没有「间歇-恢复」交替结构：有就只分「间歇 / 恢复」，恢复不当强度档。
    # 只有间歇段之间配速不同（金字塔、变速）时才在间歇段内部再分 2~3 档。
    wr = attach_work_rest(ordered)
    if wr:
        got["bands"], got["order"] = wr
        return got
    bs = band_split(segs)
    if bs:
        got["bands"] = bs["bands"]
        got["order"] = bs["order"]
    return got


def from_series(points: list[dict] | None, forced: bool = False) -> dict | None:
    """逐点速度识别。点已由 `activity._thin()` 抽稀，量级几百个，O(n log n) 足够。

    **不能丢掉低速/静止点**：间歇的组间恢复常常是原地站立（spd≈0），
    过滤掉它们会让时间轴出现几十秒的空洞，`_seg()` 的梯形积分把这段按
    两端速度算成几百米，于是快组距离虚高、慢组凭空多出「1 km 慢跑」。
    真实样本里 600 个点有 242 个是静止点，正是这个坑。
    """
    pts = [p for p in (points or []) if isinstance(p.get("t"), (int, float))]
    if len(pts) < 8:
        return None
    pts.sort(key=lambda p: p["t"])
    # 5 点滑动平均，压掉 GPS 毛刺造成的假切换
    smooth: list[float] = []
    for i in range(len(pts)):
        a, b = max(0, i - 2), min(len(pts), i + 3)
        w = [pts[j].get("spd") or 0.0 for j in range(a, b)]
        smooth.append(sum(w) / len(w))
    thr, lo, hi = _split(smooth)
    if not _split_ok(lo, hi) and not (forced and hi > lo > 0
                                     and (hi - lo) / hi >= MIN_FORCED_GAP):
        return None
    flags = [v >= thr for v in smooth]
    # 过短的段不是一组间歇：短快段并回慢段、短慢段并回快段。两轮足够让
    # 合并后的相邻同类段接上。慢段只看时长——原地休息的距离本来就近乎 0。
    for _ in range(2):
        changed = False
        for is_fast, a, b in _runs(flags):
            dur = (pts[b - 1]["t"] - pts[a]["t"])
            dist = sum(smooth[a:b]) * dur / max(1, b - a)
            too_short = (dur < MIN_SEG_S or dist < MIN_SEG_M) if is_fast \
                else (dur < MIN_SLOW_SEG_S)
            if too_short:
                for k in range(a, b):
                    flags[k] = not is_fast
                changed = True
        if not changed:
            break
    segs: list[dict] = []
    for is_fast, a, b in _runs(flags):
        s = _seg(pts[a:b], is_fast, smooth, a, b)
        if s and s["dur_s"] >= 5:
            segs.append(s)
    return _finish(segs, "series", forced=forced)


def from_laps(laps: list[dict] | None, forced: bool = False) -> dict | None:
    """分圈兜底：用「距离 / 时长」算速度，不依赖 `pace_s`。

    佳明对极短的休息圈（原地站立 90 秒只挪 30~60 m）经常不给 `pace_s`，
    按配速字段过滤会把所有慢圈丢掉、只剩一串快圈，自然认不出交替。
    """
    real = [l for l in (laps or [])
            if (l.get("dist_m") or 0) >= 20 and (l.get("dur_s") or 0) > 5]
    if len(real) < 4:
        return None
    spds = [l["dist_m"] / l["dur_s"] for l in real]
    thr, lo, hi = _split(spds)
    if not _split_ok(lo, hi) and not (forced and hi > lo > 0
                                     and (hi - lo) / hi >= MIN_FORCED_GAP):
        return None
    kinds = [v >= thr for v in spds]           # True = 快
    switches = sum(1 for i in range(1, len(kinds)) if kinds[i] != kinds[i - 1])
    # 识别模式下这里要防「10 公里里 2 圈爬坡就当成间歇」；标记模式下佳明已
    # 确认，仍需快慢都有，但不再要求重复次数达标。
    if not forced and (switches < MIN_SWITCHES or sum(kinds) < MIN_SETS
                       or len(kinds) - sum(kinds) < MIN_SETS):
        return None
    # 相邻同档圈合成一组（热身/放松通常连续好几圈）
    segs: list[dict] = []
    for is_fast, a, b in _runs(kinds):
        chunk = real[a:b]
        dist = sum(l["dist_m"] for l in chunk)
        dur = sum(l.get("dur_s") or 0 for l in chunk)
        if dur <= 0:
            continue
        spd = dist / dur
        hrs = [l["hr_avg"] for l in chunk if l.get("hr_avg")]
        cads = [l["cad"] for l in chunk if l.get("cad")]
        segs.append({
            "fast": is_fast, "dist_m": round(dist, 1), "dur_s": round(dur, 1),
            "spd": spd, "pace_s": round(dur / (dist / 1000)) if dist > 50 else None,
            "hr_avg": round(sum(hrs) / len(hrs)) if hrs else None,
            "cad": round(sum(cads) / len(cads), 1) if cads else None,
        })
    return _finish(segs, "laps", MIN_RATIO_LAPS, forced=forced)


# 只有跑步类活动才谈得上间歇版式；力量训练、骑行等的 hasIntensityIntervals
# 语义不同（力量训练也可能是 True），不能套同一套双栏。
RUN_TYPES = {"running", "trail_running", "track_running", "treadmill_running",
             "indoor_running", "virtual_run", "ultra_run"}


def training_mode(detail: dict | None) -> dict:
    """取 `activity.build_training()` 写进明细的训练模式。旧缓存没有这个键。"""
    t = ((detail or {}).get("training") or {})
    return t if isinstance(t, dict) else {}


def detect(detail: dict | None) -> dict | None:
    """活动明细 → 间歇结构或 None。

    唯一判据是佳明的训练模式标记（`hasIntensityIntervals`）：
    标记为间歇才切快慢双栏，否则一律返回 None（走默认分圈版式）。
    标记为间歇但切不出快慢（全程匀速）时同样返回 None。
    """
    d = detail or {}
    t = training_mode(d)
    if not t.get("interval"):
        return None
    atype = str(t.get("type") or "").lower()
    if atype and atype not in RUN_TYPES:
        return None
    points = ((d.get("series") or {}).get("points")) or []
    got = from_series(points, forced=True) or from_laps(d.get("laps") or [], forced=True)
    if got:
        got["source"] = "garmin:" + str(got.get("source") or "series")
        got["mode"] = "garmin-interval"
    return attach_bands(got, d)


def detect_coros(detail: dict | None) -> dict | None:
    """高驰版：没有官方「间歇标记」，改看**手动计圈结构**。

    高驰不会下发 `hasIntensityIntervals`（官方文档也没有等价字段，真机
    明细里只有分圈、没有训练模式），所以不能用佳明那套「只认标记」。
    但间歇课几乎必然留下结构痕迹：**表盘上按组计圈**——整组圈距不是
    1 km 自动圈（400 m / 800 m / 200 m 恢复圈都算），且快慢交替明显。

    判定取两组证据的交集，宁可不出双栏也不误报（越野、等红灯的匀速跑
    分圈仍是整齐的公里圈，进不来）：

    1. 有一组「手动计圈」（整组多数圈距明显不是 1 km）；
    2. 该组通过严格门槛的快慢交替（`from_laps` 非 forced 模式：
       快慢各 ≥3 组、切换 ≥5 次、慢段占比 ≥15%、速度差 ≥1.35×）。

    达标后仍按佳明口径输出，只是 `source` 标成 `coros:laps`、`mode`
    标成 `coros-laps`，方便出问题和日志里区分。
    """
    d = detail or {}
    groups = d.get("lap_groups") or []

    cands: list[list[dict]] = []
    for g in groups:
        if isinstance(g, dict) and manual_laps(g.get("laps") or [], g.get("lap_m")):
            cands.append(g["laps"])
    # 没带 lap_groups 的旧缓存 / 只有选中一组时，用展示用的分圈表兜底
    laps = d.get("laps") or []
    if manual_laps(laps, d.get("lap_distance_m")):
        cands.append(laps)

    best: dict | None = None
    best_laps: list[dict] = []
    for laps_ in cands:
        got = from_laps(laps_)
        if got and (best is None or got["sets"] > best["sets"]):
            best, best_laps = got, laps_
    if best:
        best["source"] = "coros:" + str(best.get("source") or "laps")
        best["mode"] = "coros-laps"
    return attach_bands(best, d, best_laps)


def check() -> None:
    """离线自检：典型间歇要认出，匀速 / 偶发减速 / 单调加速都不能误报。"""
    assert abs(_split([1, 1, 1, 1, 4, 4, 4, 4])[0] - 2.5) < 1e-9
    # 6 组：快 400 m @ 4.2 m/s + 慢 200 m @ 2.6 m/s，采样间隔 5 s
    pts: list[dict] = []
    t, dist = 0.0, 0.0
    for _ in range(6):
        for spd, secs in ((4.2, 95.0), (2.6, 77.0)):
            n = int(secs / 5)
            for _ in range(n):
                pts.append({"t": t, "spd": spd, "hr": 165 if spd > 3 else 140,
                            "cad": 182 if spd > 3 else 168, "dist": dist})
                t += 5.0
                dist += spd * 5.0
    iv = from_series(pts)
    assert iv and iv["sets"] >= 4, iv
    assert iv["fast"]["dist_m"] > iv["slow"]["dist_m"] * 0.8, iv
    assert iv["fast"]["pace_s"] < iv["slow"]["pace_s"], iv
    assert iv["ratio"] >= MIN_RATIO
    assert all("segs" in iv[k] for k in ("fast", "slow"))

    # ---- 新口径：是不是间歇只看佳明训练模式，不再看速度 ----------------
    marked = {"series": {"points": pts}, "training": {"interval": True, "type": "running"}}
    assert detect(marked) and detect(marked)["mode"] == "garmin-interval"
    assert detect(marked)["source"].startswith("garmin:")
    # 佳明没标记：速度再像间歇也不切（这正是旧实现误报的 10 条活动）
    assert detect({"series": {"points": pts},
                   "training": {"interval": False, "type": "running"}}) is None
    assert detect({"series": {"points": pts}}) is None, "旧缓存没有标记时必须走默认版式"
    # 力量训练的 hasIntensityIntervals 语义不同，不能套跑步双栏
    assert detect({"series": {"points": pts},
                   "training": {"interval": True, "type": "strength_training"}}) is None
    # 标记为间歇但全程匀速、切不出快慢：退回默认版式（空双栏没意义）
    _even = [{"t": i * 5.0, "spd": 3.0 + (0.05 if i % 3 == 0 else -0.03),
              "hr": 145, "cad": 176, "dist": i * 15.0} for i in range(120)]
    assert detect({"series": {"points": _even},
                   "training": {"interval": True, "type": "running"}}) is None

    # 匀速跑：围绕 3.0 m/s 的小抖动，不该判成间歇
    even = [{"t": i * 5.0, "spd": 3.0 + (0.06 if i % 3 == 0 else -0.04),
             "hr": 145, "cad": 176, "dist": i * 15.0} for i in range(120)]
    assert from_series(even) is None

    # 单调加速（越跑越快，没有快慢交替）
    ramp = [{"t": i * 5.0, "spd": 2.4 + i * 0.02, "hr": 140, "cad": 170,
             "dist": i * 15.0} for i in range(120)]
    assert from_series(ramp) is None

    # 分圈兜底：每公里一圈，快慢交替
    laps = []
    for i in range(8):
        fast = i % 2 == 1
        pace = 250 if fast else 400
        laps.append({"idx": i + 1, "dist_m": 1000.0, "dur_s": float(pace),
                     "pace_s": pace, "hr_avg": 168 if fast else 142,
                     "cad": 182 if fast else 168})
    iv2 = from_laps(laps)
    assert iv2 and iv2["sets"] >= 3 and iv2["source"] == "laps", iv2
    assert iv2["fast"]["pace_s"] < iv2["slow"]["pace_s"]
    assert from_laps([]) is None
    assert detect({}) is None

    # 真实样本形态（2026-10-21 21:06，15×400 m + 90 s 原地休息）：
    # 约四成采样点 spd=0，绝不能因过滤低速点打断时间轴、把距离算虚高。
    rest_pts: list[dict] = []
    t = 0.0
    for _ in range(15):
        for spd, secs in ((4.6, 86.0), (0.0, 90.0)):
            for _ in range(int(secs / 4)):
                rest_pts.append({"t": t, "spd": spd,
                                 "hr": 165 if spd else 150,
                                 "cad": 200 if spd else 0, "dist": 0.0})
                t += 4.0
    riv = from_series(rest_pts)
    assert riv and riv["sets"] == 15, riv
    assert riv["slow"]["rest"] and riv["slow"]["dist_m"] < 500, riv["slow"]
    # 两组距离相加不得超过全程（旧实现会算出 8 km > 实际 6.6 km）
    assert riv["fast"]["dist_m"] + riv["slow"]["dist_m"] < 6500, riv
    assert riv["slow"]["recover_s"] and riv["slow"]["recover_s"] > 60

    # 分圈兜底：极短休息圈常常没有 pace_s，也要认出交替
    rest_laps = []
    for i in range(30):
        fast = i % 2 == 0
        rest_laps.append({
            "idx": i + 1, "dist_m": 400.0 if fast else 40.0,
            "dur_s": 86.0 if fast else 90.0,
            "pace_s": 215 if fast else None,
            "hr_avg": 165 if fast else 190, "cad": 200 if fast else 0,
        })
    liv = from_laps(rest_laps)
    assert liv and liv["sets"] == 15 and liv["slow"]["rest"], liv

    # ---- 防误报回归：正常跑步中的「减速」不是间歇 ------------------------
    def blocks(seq: list[tuple[float, float]], dt: float = 5.0) -> list[dict]:
        out: list[dict] = []
        t, d = 0.0, 0.0
        for spd, secs in seq:
            for _ in range(max(1, int(secs / dt))):
                out.append({"t": t, "spd": spd, "hr": 150, "cad": 175,
                            "dist": d})
                t += dt
                d += spd * dt
        return out

    # 60 min 匀速跑里只有 70 s 的等红灯 / 上坡（占 1.9%）。旧实现把它
    # 报成「3 组间歇、速度差 2.86×」并换成双栏，正是要修的误报。
    cruise = [(3.0, 900.0), (0.2, 30.0), (3.0, 900.0), (2.4, 120.0),
              (3.0, 900.0), (0.2, 30.0), (3.0, 900.0)]
    assert from_series(blocks(cruise)) is None
    # 热身慢 + 匀速 + 放松慢：慢段只出现两次，构不成循环
    assert from_series(blocks([(2.3, 300.0), (3.1, 2400.0),
                               (2.2, 300.0)])) is None
    # 5 秒级的 GPS 毛刺 / 短停顿要并回相邻段，不能凑出「快慢交替」
    assert from_series(blocks(sum(([(3.2, 120.0), (0.4, 5.0)]
                                   for _ in range(6)), []))) is None
    # 抖动幅度稍大的匀速跑，也不能被 Otsu 硬切成两组
    jitter = [{"t": i * 5.0, "spd": 3.0 + (0.12 if i % 3 == 0 else -0.06),
               "hr": 145, "cad": 176, "dist": i * 15.0} for i in range(120)]
    assert from_series(jitter) is None
    # 分圈兜底：10 km 里 2 圈 / 4 圈偏慢（爬坡、等灯）都不该换双栏
    for slow_idx in ((3, 7), (1, 3, 6, 8)):
        bumpy = [{"idx": i + 1, "dist_m": 1000.0,
                  "dur_s": float(430 if i in slow_idx else 333)}
                 for i in range(10)]
        assert from_laps(bumpy) is None, slow_idx
    # series 被拒后，分圈兜底不能把误报再捡回来
    even_laps = [{"idx": i + 1, "dist_m": 1000.0, "dur_s": 333.0}
                 for i in range(10)]
    assert detect({"series": {"points": blocks(cruise)},
                   "laps": even_laps}) is None

    # ---- 收紧之后仍要保住召回 ------------------------------------------
    # 最小合法组数（3 组）
    siv = from_series(blocks(sum(([(4.2, 90.0), (2.6, 70.0)]
                                  for _ in range(3)), [])))
    assert siv and siv["sets"] == 3, siv
    # 温和速度差：名义 1.35×，平滑后约 1.29×，逐点路径要能认出
    miv = from_series(blocks(sum(([(4.0, 90.0), (2.96, 80.0)]
                                  for _ in range(6)), [])))
    assert miv and miv["sets"] >= 5 and miv["ratio"] >= MIN_RATIO, miv
    # 法特莱克：快慢时长不齐、无严格周期，但确实是重复的快慢循环
    fartlek = blocks([(3.9, 70.0), (2.4, 95.0), (4.3, 45.0), (2.2, 130.0),
                      (4.0, 110.0), (2.5, 60.0), (4.4, 55.0), (2.3, 100.0),
                      (4.1, 80.0)])
    assert from_series(fartlek) is not None
