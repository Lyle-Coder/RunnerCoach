"""线性图标（`docs/设计语言.md` §4.12）。

一个 24 网格的 path 字典 + `ico()` 输出 `<svg>`。**页面里不再内联手写
`<svg>`**（活动类型的 `_act_icon` 是既有例外，迁移时一起并进来）。

规格：画板 24、描边 1.7、圆头圆角、不用 fill、颜色 `currentColor`。
命中区不靠 svg 撑，按钮/行自己保证 ≥44px。

命名新增先加字典再引用，不要在页面里写一次性 path。
"""

ICONS: dict[str, str] = {
    # 铃铛：赛事提醒开 / 关。关掉时加一道斜杠，形状不变只加线，
    # 和「只换色不改形状」的状态规则一致。
    "bell": (
        '<path d="M12 4a5 5 0 0 0-5 5v3l-1.6 3.2a1 1 0 0 0 .9 1.4h11.4a1 1 0 0 0 '
        '.9-1.4L17 12V9a5 5 0 0 0-5-5Z"/>'
        '<path d="M10 20a2.2 2.2 0 0 0 4 0"/>'
    ),
    "bell-off": (
        '<path d="M12 4a5 5 0 0 0-5 5v3l-1.6 3.2a1 1 0 0 0 .9 1.4h11.4a1 1 0 0 0 '
        '.9-1.4L17 12V9a5 5 0 0 0-5-5Z"/>'
        '<path d="M10 20a2.2 2.2 0 0 0 4 0"/>'
        '<path d="M4 4l16 16"/>'
    ),
}


def ico(name: str, size: int = 18, cls: str = "") -> str:
    """返回一个线性图标。未知 key 返回空串，不让页面因为拼错名字崩掉。"""
    path = ICONS.get(name)
    if not path:
        return ""
    c = f' class="{cls}"' if cls else ""
    return (
        f'<svg{c} width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true">' + path + "</svg>"
    )
