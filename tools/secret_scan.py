# -*- coding: utf-8 -*-
"""凭据扫描：把「不该进仓库的东西」在提交前就拦下来。

    python tools/secret_scan.py            # 扫全库（跳过数据/构建产物）
    python tools/secret_scan.py <路径…>     # 只扫这几个

为什么要有这个
--------------
2026-11 的 `coros-local.bat` 把高驰账号和口令直接写进了仓库，公开镜像上挂了
几周才发现。这类事故的共同点不是「不小心」，而是**没有任何一步会因为它而
变红**：代码能跑、测试能过、页面正常，账号在别人手里。

所以把它做进 selftest：以后谁再往里写明文口令，`cli.py selftest` 当场失败。
宁可偶尔误报让人看一眼，也不要再一次「挂几周才发现」。

只认「真凭据」的形状
--------------------
误报多了就没人看，所以这里刻意窄：
1. **赋值**：只认 `键 = 字面量` 且键名是口令类（password / pwd / secret /
   admin_token / smtp_pass / api_key…）。值是代码（`cfg.get("password")`、
   `$TOKEN`、`%X%`）、占位符（`YOUR_...`、`<…>`、`xxx`）一律放过。
   代码文件里只认**带引号的字面量**——`password=password` 这种变量传参不算。
2. **令牌前缀**：`AIza…`、`ghp_…`、`sk-…`、`xox?-`、`-----BEGIN … PRIVATE KEY-----`、
   `eyJ…`.`eyJ…` 三段式 JWT。这些形状出现即真，没有误报空间。

误报怎么办
----------
把值换成占位符，或者把整行挪到 `*.private.*` / `.env`（都在 `.gitignore`）。
**不要加白名单**：白名单只会在真正需要它的那天被人随手放宽。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 不扫的目录：数据、构建产物、依赖、版本库本身
SKIP_DIRS = {".git", "node_modules", "data", "output", "dist", "site",
             "_archive", "__pycache__", ".venv", "venv", ".idea", ".vscode",
             "keys", "spaweb", "spa", "fixtures"}

# 只扫文本：二进制里就算有字节也读不成凭据，扫了只是浪费时间和误报
TEXT_EXT = {".py", ".bat", ".cmd", ".sh", ".ps1", ".json", ".kt", ".java",
            ".ts", ".tsx", ".js", ".jsx", ".md", ".yml", ".yaml", ".toml",
            ".ini", ".cfg", ".properties", ".gradle", ".kts", ".txt", ""}

# 配置/脚本类：允许 `KEY=value` 裸写
SCRIPT_EXT = {".bat", ".cmd", ".sh", ".ps1", ".env", ".ini", ".cfg",
              ".properties", ".yml", ".yaml", ".txt", ""}

# 占位符：出现这些就不是真凭据
PLACEHOLDER = re.compile(
    r"""^(?:
        |<[^>]*>           # <你的令牌>
        |YOUR[A-Z_]*       # YOUR_COROS_PASSWORD
        |your[_a-z]*       |xxx+|XXX+|\.+|…+
        |\*+               # ***
        |\$\{?[A-Za-z_]    # ${TOKEN} / $TOKEN
        |%[A-Za-z_]+%?     # %TOKEN%
        |change[_-]?me     |placeholder|example|sample|dummy|test
        |none|null|None|nil|空
    )$""",
    re.X)

# 值里出现这些字符，基本就是代码/模板而不是字面量
CODEISH = re.compile(r"[()\[\]{}$%`+=;|&<>\\]")

# 测试夹具里的假值：`sk-test`、`oc_sk_demo`、`"SECRET"`。真实口令里出现
# 「test」的概率远低于「夹具里全是 test」，认这一条比让人天天看红字划算。
FAKE = re.compile(r"(?i)(demo|test|fake|dummy|example|sample|placeholder|"
                  r"invalid|secret|abcdef|123456)")

# 口令类键名。**刻意不含 token**：代码里 `token=token` 太多，一加就全是误报
KEYISH = (r"(?:password|passwd|pwd|smtp_pass|admin_token|coros_password|"
          r"secret|secret_key|api_key|apikey|access_key|private_key)")

# 1) 脚本/配置：行首 `set "KEY=value"` / `KEY=value` / `export KEY=value`
#    不用一条大正则：`set "K=V"` 里既有前缀引号又有后缀引号，正则很容易只吃
#    一半（第一版就在这儿漏掉了一条真口令）。手工切更好懂也好改。
_SCRIPT_PREFIX = re.compile(r"^(?:export\s+|set\s+|setx\s+|--[a-z-]+\s+)+",
                            re.I)
_SCRIPT_KEY = re.compile(r"^[A-Za-z0-9_.\-]*%s[A-Za-z0-9_.\-]*$" % KEYISH,
                         re.I)


def _script_assign(line: str):
    """`KEY=value` 这种行 → (key, value, quoted)；不是赋值就返回 None。"""
    s = line.strip()
    s = _SCRIPT_PREFIX.sub("", s)
    if not s or s.startswith(("#", "rem ", "REM ")):
        return None
    if "=" not in s:
        return None
    key, _, val = s.partition("=")
    key = key.strip().strip('"').strip("'")
    if not _SCRIPT_KEY.match(key):
        return None
    val = val.strip()
    # `set "K=V"` 是前引号在键前面、后引号在值后面；`K="V"` 是两边都在值里。
    # 两种都得剥，否则占位符会被尾引号挡住（第一版就是这么漏判的）。
    quoted = val[:1] in "\"'" or val[-1:] in "\"'"
    if val[:1] in "\"'":
        val = val[1:]
    if val[-1:] in "\"'":
        val = val[:-1]
    return key, val, quoted

# 2) JSON / 代码：只认带引号的字面量
_ASSIGN_LITERAL = re.compile(
    r"""(?ix)
    ["']?(?P<key>[A-Za-z0-9_.\-]*%s[A-Za-z0-9_.\-]*)["']?
    \s*[:=]\s*
    (?P<q>["'])(?P<val>[^"'\r\n]{6,})(?P=q)
    """ % KEYISH)

# 3) 常见高熵令牌前缀 / 私钥
_TOKENS = (
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}")),
    ("OpenAI key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("Slack token", re.compile(r"xox[abprs]-[0-9A-Za-z\-]{10,}")),
    ("私钥", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.")),
)


def _tracked(root: Path) -> set[Path] | None:
    """git 跟踪的文件集合。没有 git（手机上的 selftest）就返回 None。"""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return {(root / x).resolve()
            for x in r.stdout.decode("utf-8", "replace").split("\0") if x}


def _files(root: Path, only: list[str] | None = None) -> list[Path]:
    if only:
        return [Path(p) for p in only if Path(p).is_file()]
    tracked = _tracked(root)
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_EXT:
            continue
        if ".example" in p.name or ".example." in p.name:
            continue        # .env.example / secrets.example.json 就是给人抄的
        if tracked is not None and p.resolve() not in tracked:
            continue        # 没入库的（本机配置、私密脚本）不用管
        try:
            if p.stat().st_size > 2 * 1024 * 1024:      # 2MB 以上不扫
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _bad_value(val: str, *, quoted: bool) -> bool:
    v = val.strip()
    if len(v) < 6:
        return False
    if PLACEHOLDER.match(v) or FAKE.search(v):
        return False
    if not quoted and CODEISH.search(v):
        return False
    return True


def scan(root: Path, only: list[str] | None = None) -> list[str]:
    """返回 `路径:行号: 原因`。空列表＝没发现问题。"""
    root = Path(root)
    hits: list[str] = []
    for p in _files(root, only):
        try:
            rel = p.resolve().relative_to(root.resolve())
        except ValueError:
            rel = p
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        ext = p.suffix.lower()
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith(("#", "rem ", "REM ", "//", "*",
                                       "<!--", "--")):
                continue        # 注释里写「password=xxx 这样配」不算泄露
            if ext in SCRIPT_EXT:
                got = _script_assign(line)
                if got and _bad_value(got[1], quoted=got[2]):
                    hits.append(f"{rel}:{i}: 明文口令（{got[0]}）")
                    continue
            if ext not in (".md",):     # 文档里的示例不按赋值看，只看令牌形状
                m = _ASSIGN_LITERAL.search(line)
                if m and _bad_value(m.group("val"), quoted=True):
                    hits.append(f"{rel}:{i}: 明文口令（{m.group('key')}）")
                    continue
            for name, rx in _TOKENS:
                if rx.search(line):
                    hits.append(f"{rel}:{i}: 疑似{name}")
                    break
    return hits


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    only = argv[1:] or None
    hits = scan(root, only)
    if not hits:
        print(f"· 凭据扫描通过（{len(_files(root, only))} 个文件）")
        return 0
    print(f"× 发现 {len(hits)} 处疑似凭据：")
    for h in hits:
        print("   ", h)
    print("\n处理办法：换成占位符，或挪进 *.private.* / .env（见 .gitignore）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
