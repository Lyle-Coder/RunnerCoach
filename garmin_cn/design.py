"""设计 token 的唯一来源。

对应 `docs/设计语言.md` §3。**页面里不写裸色值和魔法数字**：所有颜色、字号、
间距、圆角都从这里取。原生壳（`MainActivity.kt`）写的是 ARGB 常量，改这里
时必须手工同步那几行，否则底栏会和页面出现「第三种黑」。

两条使用约定：

1. 新代码用 `--surface-2` / `--ink-2` / `--r-3` 这套名字（带连字符、带序号）。
2. 老模块还在用 `--surface2` / `--muted` / `--r`，`ALIASES` 里给了映射，
   一个版本内保留；迁移完就删，别让两套名字长期并存。
"""

# ---------------------------------------------------------------------------
# 核心 token（§3.5 原样落地）
TOKENS = """
:root{
  /* 底色：4 级。页面底不用纯黑，留一点蓝，屏幕上才有层次 */
  --bg:#070B12; --surface:#0D141F; --surface-2:#111B29; --surface-3:#16233A;
  /* 发丝线：**白色透明，不用蓝**。蓝色描边会让整页像线框图 */
  --line:rgba(255,255,255,.065); --line-brand:rgba(96,165,250,.22);
  /* 比 --line 再实一档的白：开关槽、输入框底这类「需要摸得到」的控件 */
  --line-strong:rgba(255,255,255,.12);
  /* 文字：3 级。ink-3 也要过 4.5:1，暗屏下才读得清 */
  --ink:#E9F0F8; --ink-2:#A6B8CD; --ink-3:#7A8DA4; --ink-inv:#F2F7FF;
  /* 品牌 / 语义：主色只做关键动作、当前选中、数据强调，不做大面积背景 */
  --brand:#3B82F6; --brand-soft:rgba(59,130,246,.12); --brand-ink:#9CC6FF;
  --cyan:#22D3EE; --cyan-soft:rgba(34,211,238,.13);
  --ok:#3FD39A; --ok-soft:rgba(63,211,154,.10);
  --warn:#F5A524; --warn-soft:rgba(245,165,36,.10);
  --bad:#F2555A;  --bad-soft:rgba(242,85,90,.10);
  --blue:#5EA0FF; --blue-soft:rgba(94,160,255,.14);
  /* 地图专用：多人共同跑过的格子（白）与波点衫的底色。
     这两个不是界面色，是「图上的数据」，所以单列，别和 --surface 混用。 */
  --map-shared:#F4F7FC;
  /* 字阶：8 档，一档一职责，相邻至少差 2px */
  --fs-dsp:32px; --fs-h1:24px; --fs-h2:20px; --fs-title:16px;
  --fs-lead:15px; --fs-body:14px; --fs-cap:12px; --fs-micro:11px;
  /* 间距：4pt 栅格 */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:20px;
  --sp-6:24px; --sp-8:32px;
  /* 圆角：4 档，按组件分，不再「一张卡一个值」 */
  --r-1:10px; --r-2:14px; --r-3:18px; --r-pill:999px;
  /* 阴影：e1 普通卡（黑影 + 发丝线），e2 hero（品牌描边 + 蓝影，一页一处） */
  --e1:0 1px 0 rgba(255,255,255,.03), 0 6px 18px -12px rgba(0,0,0,.9);
  --e2:0 0 0 1px rgba(96,165,250,.20), 0 20px 44px -28px rgba(59,130,246,.45);
}
"""

# ---------------------------------------------------------------------------
# 旧名别名：老模块（aiui / eventui / history / runsui / report）还在用这套，
# 一个版本内保留。**新写的样式不要用右边这列的名字。**
ALIASES = """
:root{
  --surface2:var(--surface-2);
  --line2:var(--line);
  --ink2:var(--ink-2);
  --muted:var(--ink-2);
  --faint:var(--ink-3);
  --amber:var(--warn); --amber-soft:var(--warn-soft);
  --red:var(--bad); --red-soft:var(--bad-soft);
  --r:var(--r-3);
  --shadow:var(--e1);
  /* 旧 --glow 是「品牌描边 + 蓝影」，现在只留给 hero（--e2）。
     普通卡片一律用 --e1，所以这里指向 e1，误用也不会满屏发光。 */
  --glow:var(--e1);
}
"""

# 字号白名单（test/audit 用）。文档 §7：只允许这 8 档
FONT_SIZES = (11, 12, 14, 15, 16, 20, 24, 32)
# 圆角白名单。50% / 999 之外只允许这四档
RADII = (10, 14, 18, 999)


def font(step: str) -> str:
    """按字阶名取字号，避免页面里写裸 px：`design.font("title")` → 16px。"""
    return f"var(--fs-{step})"


def radius(step: int = 3) -> str:
    return f"var(--r-{step})"
