# -*- coding: utf-8 -*-
"""大模型接入内核：一套 OpenAI 兼容协议，两种凭据来源。

为什么只做「OpenAI 兼容」
------------------------
国内能用的 DeepSeek / 通义 / Kimi / 智谱，海外 OpenAI，本地 Ollama，
全都提供 `/chat/completions` 这个入口。只写这一套协议，换供应商只是改三行配置，
不用为每家写一遍适配器——也不会因为某家改了私有签名就把整个功能拖死。

两种凭据
--------
1. **own（自配 API）**：用户自己填 base_url + api_key，请求直连上游。
   key 落 `data/llm_config.json`（0600），不上传、不写日志、错误信息里也不回显。
2. **platform（平台额度）**：请求发到 RunnerCoach 网关，用平台令牌换额度。
   网关持有真正的上游 key，客户端只认令牌和余额——这样充值才有意义。

为什么不用 requests
-------------------
APK 里是 Chaquopy，每加一个三方包就打进去几 MB，还要处理 ABI 兼容。
标准库 urllib 足够，流式也能读（`resp.readline()` 就是 SSE 的一行）。

失败要能说人话
--------------
模型服务报错分四类，界面上要区分开，否则用户不知道是该换 key、等一会儿、
还是去充钱：`auth`（key 无效）、`quota`（余额不足/额度用完）、
`rate`（限流，等一会儿再试）、`net`（网络到不了）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# 项目根目录：桌面/Termux 是代码所在目录；APK 里由 GARMIN_HOME 指到 filesDir
import os as _os

HOME = (Path(_os.environ["GARMIN_HOME"]).expanduser()
        if _os.environ.get("GARMIN_HOME") else Path(__file__).resolve().parent.parent)
CONFIG_PATH = HOME / "data" / "llm_config.json"

# ---------------------------------------------------------------- 供应商预设
# base_url 一律写到 /v1 这一级（Ollama 也是），拼路径时只加 /chat/completions，
# 免得每处调用各自判断「要不要补 v1」。
PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "key_url": "https://platform.deepseek.com/api_keys",
        "note": "中文分析质量好、价格低，国内直连稳定",
    },
    "qwen": {
        "label": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus", "key_url": "https://bailian.console.aliyun.com/",
        "note": "阿里百炼，新用户有免费额度",
    },
    "kimi": {
        "label": "Kimi", "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k", "key_url": "https://platform.moonshot.cn/console/api-keys",
        "note": "长上下文，适合把整月数据一起喂",
    },
    "zhipu": {
        "label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash", "key_url": "https://bigmodel.cn/usercenter/apikeys",
        "note": "glm-4-flash 有免费档",
    },
    "openai": {
        "label": "OpenAI", "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini", "key_url": "https://platform.openai.com/api-keys",
        "note": "需要可用的网络出口",
    },
    "ollama": {
        "label": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b", "key_url": "https://ollama.com",
        "note": "完全离线，数据不出本机；质量取决于本机算力",
    },
    "opencode_go": {
        "label": "OpenCode Go", "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "deepseek-v4.1-flash", "key_url": "https://opencode.ai/console",
        "note": ("订阅制出口，地址必须是 /inference/go/openai/v1；每个请求要带稳定的 "
                 "x-opencode-session（本 App 自动补）。拍照识热量可用 deepseek-v4.1-flash"
                 "（支持图片），mimo-v2.5 / kimi-k3 也可以"),
        "session": "opencode",
    },
    "opencode_payg": {
        "label": "OpenCode 按量", "base_url": "https://opencode.ai/inference/openai/v1",
        "model": "deepseek-v4.1-flash", "key_url": "https://opencode.ai/console",
        "note": ("同一个 Console key，走按量计费（地址不带 /go）。"
                 "Go 订阅请选上面那个预设"),
        "session": "opencode",
    },
    "custom": {
        "label": "自定义", "base_url": "", "model": "", "key_url": "",
        "note": "任意 OpenAI 兼容服务，自己填地址与模型名",
    },
}
DEFAULT_PROVIDER = "deepseek"

# ---------------------------------------------------------------- 官方平台入口
# 「平台地址」不该让用户填：它由部署方决定，出厂即预置。
# 但平台层的分享门禁要求每个请求（实测连 GET /health 也一样）都带 X-Share-Code，
# 首次请求没法凭空变出凭证，所以 App 必须内置一个引导入口。
#
# 注意区分：分享码不是密钥，它本来就要发给每一位用户（写在公开仓库里没有
# 额外风险）；真正的密钥——上游 API Key、隧道共享密钥、SMTP 授权码——
# 只存在于服务端 .env / secrets，既不进仓库也不进 APK。
#
# 业务地址优先由服务端 GET /v1/client/config 下发（见 sync_platform_config），
# 值写在网关的 .env：RC_PUBLIC_BASE / RC_SHARE_CODE。换域名、轮换分享码
# 只改服务端并重启容器即可，不必重新发版。
BUILTIN_PLATFORM_BASE = "https://garmin-daily-dashboard.app.workbuddy.host"
BUILTIN_PLATFORM_SHARE_CODE = "ORT7NXCECR"
CLIENT_CONFIG_PATH = "/v1/client/config"
CLIENT_CONFIG_TTL = 6 * 3600    # 下发结果的缓存时长（秒）

DEFAULTS = {
    "mode": "own",              # own=自配 API / platform=平台额度
    "provider": DEFAULT_PROVIDER,
    "base_url": "",             # 留空则用预设
    "model": "",                # 留空则用预设
    "api_key": "",
    "platform_auto": True,      # True=用官方服务（内置/服务端下发）；False=自定义地址
    "platform_base": "",        # 当前生效的平台地址（由 _resolve_platform 计算）
    "platform_share_code": "",  # 当前生效的分享码
    "platform_token": "",
    # 服务端下发结果（内部字段，界面白名单不含，防止被 config.set 篡改）
    "platform_served_base": "",
    "platform_served_share_code": "",
    "platform_sync_at": 0,      # 上次尝试下发的时刻（成功失败都记，避免反复等超时）
    "temperature": 0.6,
    "auto_advice": True,        # 打开看板时自动生成今日建议
    # 鉴权头：空=自动识别（先 Bearer，401/403 再换 x-api-key）；也可强制
    # bearer / x-api-key / both。OpenAI 兼容只约定了请求体，没约定 key 放哪个头。
    "auth_style": "",
    "auth_map": {},             # {域名: 实际生效的方式}，自动探测命中后记下来
    "session_id": "",           # OpenCode 网关要求的稳定会话号（ses_…）
}

CONNECT_TIMEOUT = 12
READ_TIMEOUT = 180


class LLMError(RuntimeError):
    """带分类的模型调用错误。`kind` 决定界面给什么建议。"""

    def __init__(self, kind: str, message: str, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict:
        return {"ok": False, "kind": self.kind, "error": self.message,
                "detail": self.detail[:600] if self.detail else ""}


# ---------------------------------------------------------------- 配置读写
def _resolve_platform(cfg: dict) -> dict:
    """决定当前生效的平台地址。

    优先级：用户在「高级」里手填的自定义地址 > 服务端下发的地址 > 出厂内置地址。
    老配置文件没有 platform_auto 字段，默认 True——升级后会自动从「之前填错
    的地址」切回官方入口，这正是 v0.4.5 修掉收不到验证码的关键。
    """
    if not cfg.get("platform_auto", True):
        cfg["platform_base"] = (cfg.get("platform_base") or "").rstrip("/")
        return cfg
    served = (cfg.get("platform_served_base") or "").rstrip("/")
    cfg["platform_base"] = served or BUILTIN_PLATFORM_BASE
    cfg["platform_share_code"] = (
        (cfg.get("platform_served_share_code") or "") if served
        else BUILTIN_PLATFORM_SHARE_CODE)
    return cfg


def load_config() -> dict:
    """读配置，缺项用默认值补齐（老配置文件不会因为新增字段就读不出来）。"""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            # 兼容早期只有 base_url/api_key/model 三个字段的版本
            if raw.get("base_url") and not raw.get("provider"):
                raw["provider"] = "custom"
            cfg.update({k: v for k, v in raw.items() if k in DEFAULTS or k in ("updated_at",)})
    except Exception:
        pass
    # 平台额度模式的地址兼容字段名
    if not cfg.get("platform_base") and cfg.get("gateway"):
        cfg["platform_base"] = cfg["gateway"]
    return _resolve_platform(cfg)


def save_config(**patch) -> dict:
    """合并写入。只接受已知字段，防止界面传错键把配置写脏。"""
    cfg = load_config()
    for k, v in patch.items():
        if k in DEFAULTS:
            cfg[k] = v
    # 自定义地址允许直接粘贴带 ?sharecode= 的分享链接：拆出分享码，地址只留域名
    # （门禁要求 X-Share-Code 请求头，App 拼路径时没法带查询串）。
    # 官方模式下忽略界面传来的 base，避免旧版前端或手滑把内置值覆盖掉。
    if not cfg.get("platform_auto", True):
        base = cfg.get("platform_base") or ""
        if "?" in base:
            head, _, query = base.partition("?")
            for seg in query.split("&"):
                k, _, v = seg.partition("=")
                if k.lower() == "sharecode" and v:
                    cfg["platform_share_code"] = v
            cfg["platform_base"] = head
    # 地址在保存时就纠成可用形态（少 /openai/v1、多贴 /chat/completions 都会 404），
    # 界面上也能立刻看到真正会被请求的地址。
    cfg["base_url"] = normalize_base(cfg.get("base_url") or "", cfg.get("provider") or "")
    _resolve_platform(cfg)
    cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)      # 里面有 key，别让同机其它账号读到
    except Exception:
        pass
    return cfg


def public_config() -> dict:
    """给界面看的配置：key 只回显前 4 位 + 长度，够确认「配没配对」。"""
    cfg = load_config()
    key = cfg.get("api_key") or ""
    token = cfg.get("platform_token") or ""
    prov = PROVIDERS.get(cfg.get("provider") or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])
    return {
        "ok": True,
        "mode": cfg["mode"],
        "provider": cfg["provider"],
        "provider_label": prov["label"],
        "base_url": normalize_base(cfg["base_url"] or prov["base_url"], cfg["provider"]),
        "base_url_custom": bool(cfg["base_url"]),
        "auth_style": cfg.get("auth_style") or "",
        "auth_effective": (str((cfg.get("auth_map") or {}).get(
            _host_of(effective_base(cfg))) or "") or "bearer"),
        "session_on": (_provider_flag(cfg) == "opencode"
                       or "opencode.ai" in _host_of(effective_base(cfg))),
        "model": cfg["model"] or prov["model"],
        "model_custom": bool(cfg["model"]),
        "api_key_hint": (key[:4] + "…（" + str(len(key)) + " 位）") if key else "",
        "has_key": bool(key),
        "platform_base": cfg["platform_base"],
        "platform_auto": bool(cfg.get("platform_auto", True)),
        "platform_source": ("custom" if not cfg.get("platform_auto", True)
                            else ("server" if cfg.get("platform_served_base")
                                  else "builtin")),
        "platform_hint": (token[:4] + "…（" + str(len(token)) + " 位）") if token else "",
        "has_platform_token": bool(token),
        "temperature": cfg["temperature"],
        "auto_advice": cfg["auto_advice"],
        "configured": is_configured(cfg),
        "providers": [
            {"id": k, "label": v["label"], "base_url": v["base_url"],
             "model": v["model"], "key_url": v["key_url"], "note": v["note"],
             "session": bool(v.get("session"))}
            for k, v in PROVIDERS.items()
        ],
    }


def is_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    if cfg.get("mode") == "platform":
        return bool(cfg.get("platform_base") and cfg.get("platform_token"))
    return bool(effective_base(cfg) and cfg.get("api_key"))


# 用户手填地址时最常见的两种错法：整条接口地址粘进来，或者只填到网关根。
_ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/responses", "/messages")

# OpenCode 的各条入口路径 —— 它把「计划」和「协议」都编进了路径里，
# 少一段就是 404，所以这里统一纠成 <base>/openai/v1 这一级。
_OPENCODE_PATHS = {
    "": "/inference/openai/v1",
    "/inference": "/inference/openai/v1",
    "/inference/go": "/inference/go/openai/v1",
    "/zen": "/inference/openai/v1",
    "/zen/go": "/inference/go/openai/v1",
}


def normalize_base(url: str, provider: str = "") -> str:
    """把地址收拾成「以 /v1 结尾、可以直接拼 /chat/completions」的样子。

    纠三类输入：粘贴了完整接口路径、只填到网关根、OpenCode 那种多段路径。
    非 OpenCode 的地址原样保留（Ollama 的 /v1、智谱的 /api/paas/v4 都不能动）。
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    low = u.lower()
    for suf in _ENDPOINT_SUFFIXES:
        if low.endswith(suf):
            u = u[: -len(suf)].rstrip("/")
            low = u.lower()
            break
    sp = urllib.parse.urlsplit(u)
    if not sp.netloc:
        return u
    host, path = sp.netloc.lower(), sp.path.rstrip("/")
    if host == "opencode.ai" or host.endswith(".opencode.ai"):
        if path in _OPENCODE_PATHS:
            path = _OPENCODE_PATHS[path]
        elif path.endswith("/openai/v1"):
            pass
        elif path.endswith("/openai"):
            path += "/v1"
        else:
            path = _OPENCODE_PATHS[""]
            if provider == "opencode_go":
                path = _OPENCODE_PATHS["/inference/go"]
        return urllib.parse.urlunsplit((sp.scheme or "https", sp.netloc, path, "", ""))
    return u


def effective_base(cfg: dict) -> str:
    prov = PROVIDERS.get(cfg.get("provider") or DEFAULT_PROVIDER) or {}
    base = cfg.get("base_url") or prov.get("base_url") or ""
    return normalize_base(base, cfg.get("provider") or "")


def effective_model(cfg: dict) -> str:
    if cfg.get("model"):
        return cfg["model"]
    prov = PROVIDERS.get(cfg.get("provider") or DEFAULT_PROVIDER) or {}
    return prov.get("model") or ""


def _endpoint(cfg: dict) -> tuple[str, str]:
    """(chat 完整地址, 鉴权 token)。平台模式与自配模式只是地址和 token 不同。"""
    if cfg.get("mode") == "platform":
        base = (cfg.get("platform_base") or "").rstrip("/")
        if not base:
            raise LLMError("config", "还没填平台服务地址",
                           "在「模型服务」里填入网关地址，或切换成自配 API")
        if not base.endswith("/v1"):
            base += "/v1"
        return base + "/chat/completions", cfg.get("platform_token") or ""
    base = effective_base(cfg)
    if not base:
        raise LLMError("config", "还没填 API 地址", "选择供应商预设，或手填兼容地址")
    return base + "/chat/completions", cfg.get("api_key") or ""


# ---------------------------------------------------------------- HTTP
# 一定要带一个「像客户端」的 User-Agent。
# urllib 默认发的是 `Python-urllib/3.13`，OpenCode 前面的 Cloudflare 会直接
# 回 `403 error code: 1010`（浏览器完整性校验），跟 key 对不对完全无关——
# 之前这个 403 被归到了「密钥无效」，把排查方向带偏了很久。
from . import __version__ as _VERSION

USER_AGENT = (f"RunnerCoach/{_VERSION} "
              "(Android; +https://gitee.com/liyufei-lye/RunnerCoach)")

AUTH_STYLES = ("bearer", "x-api-key", "both")


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return ""


def _auth_headers(style: str, token: str) -> dict:
    if not token or style == "none":
        return {}
    if style == "x-api-key":
        return {"x-api-key": token}
    if style == "both":
        return {"Authorization": f"Bearer {token}", "x-api-key": token}
    return {"Authorization": f"Bearer {token}"}


def _auth_styles(cfg: dict | None, token: str, url: str) -> list[str]:
    """按顺序尝试的鉴权方式。

    OpenAI 兼容只约定了「请求体长什么样」，没约定 key 放哪个头：OpenCode 新的
    /inference 网关认 `Authorization: Bearer`，老的 zen 网关只认 `x-api-key`，
    还有的读 `api-key`。用户不该被要求知道这些差异——默认先用主流的 Bearer，
    401/403 再换头重试，命中后记到 auth_map，下次直接用对的那个。
    """
    if not token:
        return ["none"]
    if cfg is not None and cfg.get("mode") == "platform":
        return ["bearer"]
    forced = (cfg.get("auth_style") or "").strip().lower()
    if forced in AUTH_STYLES:
        return [forced]
    order = ["bearer", "x-api-key"]
    learned = str((cfg.get("auth_map") or {}).get(_host_of(url)) or "").strip()
    if learned in order:
        order.remove(learned)
        order.insert(0, learned)
    return order


def _remember_auth(host: str, style: str) -> None:
    """记下某个域名真正吃哪套头。只在不等于当前记录时写盘，避免每次请求都 I/O。"""
    if not host or style in ("", "none"):
        return
    try:
        cfg = load_config()
        amap = dict(cfg.get("auth_map") or {})
        if amap.get(host) == style:
            return
        amap[host] = style
        save_config(auth_map=amap)
    except Exception:
        pass


def session_id(cfg: dict | None = None) -> str:
    """稳定的会话号。OpenCode 的 Go 网关要求每个请求都带（用于路由与提示缓存），
    每次换新的会让缓存全失效，所以生成一次就落盘复用。"""
    cfg = cfg if cfg is not None else load_config()
    sid = str(cfg.get("session_id") or "").strip()
    if sid.startswith("ses_") and len(sid) > 8:
        return sid
    sid = "ses_" + uuid.uuid4().hex[:24]
    cfg["session_id"] = sid
    try:
        save_config(session_id=sid)
    except Exception:
        pass
    return sid


def _provider_flag(cfg: dict | None) -> str:
    prov = PROVIDERS.get((cfg or {}).get("provider") or "") or {}
    return str(prov.get("session") or "")


def _session_headers(cfg: dict | None, url: str) -> dict:
    """OpenCode 专用会话头。别的服务收到未知头一般会忽略，但没必要发。"""
    if not cfg or cfg.get("mode") == "platform":
        return {}
    if "opencode.ai" in _host_of(url) or _provider_flag(cfg) == "opencode":
        return {"x-opencode-session": session_id(cfg)}
    return {}


def _auth_retryable(raw: str) -> bool:
    """401/403 是不是「头放错了」。平台分享门禁回 HTML，换头也没用，别瞎试。"""
    low = (raw or "").lower()
    return "<html" not in low and "链接不完整" not in low


# 上游明说「没收到 key」，才值得换一种头重试。像 OpenCode 的 "Invalid credential"
# 是「key 不对」，换头不会变好，只会把真正的原因盖掉。
_HDR_HINTS = ("requires an api key", "missing api key", "no api key",
              "api key required", "missing x-api-key", "缺少 api key",
              "未提供 api key", "no credential provided")


def _should_retry_auth(raw: str) -> bool:
    if not _auth_retryable(raw):
        return False
    low = (raw or "").lower().strip()
    if not low:                     # 空报文：说不清原因，换一种试试
        return True
    return any(k in low for k in _HDR_HINTS)


# 两种头都失败时，优先报「有信息量」的那条（invalid/expired…），
# 而不是把 "requires an api key" 这种误导性文案给用户。
_AUTH_INFORMATIVE = ("invalid", "expired", "revoked", "forbidden",
                     "not allowed", "disabled", "incorrect")


def _pick_auth_error(errs: list[tuple[int, str]]) -> tuple[int, str]:
    for code, raw in errs:
        low = (raw or "").lower()
        if any(k in low for k in _AUTH_INFORMATIVE):
            return code, raw
    return errs[-1] if errs else (0, "")


def _post(url: str, token: str, payload: dict, timeout: int = READ_TIMEOUT,
          stream: bool = False, extra_headers: dict | None = None,
          cfg: dict | None = None):
    """POST 到上游，失败时按需换鉴权头重试一次。

    返回 urlopen 的响应；所有失败都抛 LLMError（不再让调用方猜 HTTPError）。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "User-Agent": USER_AGENT,
               "Accept": "text/event-stream" if stream else "application/json"}
    headers.update(_session_headers(cfg, url))
    headers.update(extra_headers or {})
    styles = _auth_styles(cfg, token, url)
    host = _host_of(url)
    errs: list[tuple[int, str]] = []
    for idx, style in enumerate(styles):
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        for k, v in _auth_headers(style, token).items():
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            errs.append((e.code, raw))
            if (e.code in (401, 403) and idx < len(styles) - 1
                    and _should_retry_auth(raw)):
                continue          # 只在这一种情况下换头：上游说没收到 key
            code, text = ((_pick_auth_error(errs) if e.code in (401, 403)
                           else (e.code, raw)))
            raise _http_error_raw(code, text) from None
        if idx > 0:
            _remember_auth(host, style)      # 探测成功，记住这个域名要哪种头
        return resp
    code, text = _pick_auth_error(errs) if errs else (0, "")
    raise _http_error_raw(code, text)


def _ready_config() -> dict:
    """平台模式先同步一次服务端下发的地址（结果缓存 6 小时），自配模式直接读。

    下发失败不能连累问答：sync_platform_config 内部已静默兜底，这里再兜一层。
    """
    cfg = load_config()
    if cfg.get("mode") != "platform":
        return cfg
    try:
        return sync_platform_config()
    except Exception:
        return cfg


def _platform_headers(cfg: dict, token: str = "") -> dict:
    """平台入口要带两类凭证：分享门禁码 + 设备令牌。

    历史 bug 一：chat()/chat_stream() 复用 _post() 却没带 X-Share-Code，请求被
    403 挡在中转站，网关日志里一条 /v1/chat/completions 都没有。
    历史 bug 二：公网分享层会改写 Authorization，所有带令牌的接口都 401，
    所以令牌另外放一份在 X-Platform-Token，由网关优先采信。
    """
    if cfg.get("mode") != "platform":
        return {}
    out: dict = {}
    code = (cfg.get("platform_share_code") or "").strip()
    if code:
        out["X-Share-Code"] = code
    if token:
        out["X-Platform-Token"] = token
    return out


def _http_error(e: urllib.error.HTTPError) -> LLMError:
    """兼容老调用点：先把报文读出来，再交给 _http_error_raw 分类。"""
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    return _http_error_raw(e.code, raw)


def _http_error_raw(code: int, raw: str) -> LLMError:
    """把上游的状态码翻成「该怎么办」，原始报文留在 detail 里备查。"""
    msg = raw
    try:
        obj = json.loads(raw)
        err = obj.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or raw
        elif isinstance(err, str):
            msg = err
        elif obj.get("message"):
            msg = obj["message"]
    except Exception:
        pass

    low = (raw or "").lower()
    if code in (401, 403):
        # Cloudflare 的浏览器完整性校验：403 + `error code: 1010`（text/plain）。
        # 这跟 key 一点关系都没有，是客户端标识被当成脚本了（urllib 默认 UA）。
        # 必须排在「密钥无效」前面，否则会把用户带去找 key 的问题。
        if ("error code: 1010" in low or "error code: 1020" in low
                or "attention required" in low or "cf-error-details" in low):
            return LLMError(
                "blocked",
                "请求被网关的 Cloudflare 风控挡下（客户端标识被判定为脚本），升级到最新版 App 再试",
                raw)
        # 平台分享门禁返回的是 HTML「链接不完整」，不是 JSON——这和 key 无效
        # 完全是两回事，别再让用户去检查 API Key。
        if "<html" in low or "链接不完整" in raw or "share" in low:
            return LLMError("auth", "平台访问凭证无效（分享链接不完整），请升级到最新版 App", raw)
        # 403 不全是 key 的问题：免费模型只能在官方客户端里用、账号没开这个模型，
        # 都会回 403。这两种换 key 也没用，必须说清楚。
        if "freetier" in low or "free tier" in low or "free-tier" in low:
            return LLMError("model", "这个免费模型只能在 OpenCode 客户端里用，换付费或 Go 模型", raw)
        if "model access" in low or "not enabled" in low or "disabled" in low:
            return LLMError("model", "这个 key 没有该模型的权限，去 Console 开通或换个模型", raw)
        # 把上游原话带上：光说「密钥无效」分不清是 key 过期、被撤销、
        # 还是这个 key 不属于 Go 订阅的 workspace。
        up = str(msg).strip()
        tip = ""
        if up and up != raw and "<" not in up and len(up) <= 80:
            tip = f"（上游：{up}）"
        return LLMError("auth", "密钥无效或没有权限，检查 API Key / 平台令牌" + tip, raw)
    if code == 402:
        return LLMError("quota", "额度不足，充值或换一个 key", raw)
    if code == 429:
        return LLMError("rate", "请求太频繁或额度用尽，等一会儿再试", raw)
    if code in (400, 422):
        # OpenCode Go 的会话头缺失是 400 + MissingSessionID，和「参数写错」不是一回事
        if "missingsessionid" in low or "x-opencode-session" in low:
            return LLMError(
                "session",
                "网关没收到 x-opencode-session 会话头（OpenCode Go 强制要求），请升级 App 后重试",
                raw)
        # 图片被拒和「参数写错」是两件事：前者要换多模态模型，后者要改请求。
        if any(k in low for k in ("image", "vision", "multimodal", "modality",
                                  "unsupported content", "content type")):
            return LLMError(
                "vision",
                "当前模型不支持图片输入，换一个多模态模型（如 deepseek-v4.1-flash / mimo-v2.5 / kimi-k3）",
                raw)
        return LLMError("request", f"请求被拒绝：{str(msg)[:160]}", raw)
    if code >= 500:
        # 中转站 504 表示本机隧道没应答，不是上游模型故障——提示要指向「电脑没开」
        if code == 504 or "隧道未连通" in raw or "网关响应超时" in raw:
            return LLMError("net", "平台服务暂时连不上（电脑离线或隧道客户端没启动），稍后再试", raw)
        # 下单接口的 503 是「支付通道没开通」，不是模型故障；把服务端原话交给用户
        if "在线支付尚未开通" in str(msg) or "PAY_PROVIDER" in str(msg):
            return LLMError("pay_off", str(msg)[:200], raw)
        return LLMError("upstream", "模型服务暂时不可用，稍后再试", raw)
    return LLMError("http", f"HTTP {code}：{str(msg)[:160]}", raw)


def _net_error(e: Exception) -> LLMError:
    text = str(e)
    if "timed out" in text.lower() or "timeout" in text.lower():
        return LLMError("net", "连接超时，检查网络或服务地址", text)
    return LLMError("net", f"连不上模型服务：{text[:120]}", text)


# ---------------------------------------------------------------- 调用
def chat(messages: list[dict], *, temperature: float | None = None,
         max_tokens: int = 1200, json_mode: bool = False,
         timeout: int = READ_TIMEOUT) -> dict:
    """一问一答。返回 `{ok, text, usage, model, elapsed}`，失败抛 LLMError。

    `json_mode`：要求上游返回 JSON 对象（DeepSeek / OpenAI / 通义都支持）。
    不支持的服务会忽略这个字段，所以业务层仍要能容忍带 ```json 围栏的文本。
    """
    cfg = _ready_config()
    if not is_configured(cfg):
        raise LLMError("config", "还没配置模型服务", "打开「模型服务」填自己的 API Key，或切到平台额度")
    url, token = _endpoint(cfg)
    extra = _platform_headers(cfg, token)
    payload = {
        "model": effective_model(cfg),
        "messages": messages,
        "temperature": cfg.get("temperature", 0.6) if temperature is None else temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    t0 = time.time()
    try:
        resp = _post(url, token, payload, timeout=timeout, extra_headers=extra, cfg=cfg)
        raw = resp.read().decode("utf-8", "replace")
    except LLMError:
        raise                      # _post 已经分好类，别再包一层「网络错误」
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None
    except Exception as e:
        raise _net_error(e) from None

    try:
        obj = json.loads(raw)
    except Exception:
        raise LLMError("upstream", "上游返回的不是 JSON", raw[:600]) from None

    if isinstance(obj.get("error"), (dict, str)):
        err = obj["error"]
        text = err.get("message") if isinstance(err, dict) else err
        raise LLMError("upstream", f"上游报错：{str(text)[:160]}", raw[:600])

    choices = obj.get("choices") or []
    if not choices:
        raise LLMError("upstream", "上游没有返回内容", raw[:600])
    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        # 有的服务把推理内容放 reasoning_content，content 为空
        text = (msg.get("reasoning_content") or "").strip()
    return {"ok": True, "text": text, "usage": obj.get("usage") or {},
            "model": obj.get("model") or payload["model"],
            "elapsed": round(time.time() - t0, 1)}


def chat_stream(messages: list[dict], *, temperature: float | None = None,
                max_tokens: int = 1200):
    """流式输出：逐段 yield 文本增量。界面用 SSE 转发给浏览器。

    上游的 SSE 形如 `data: {"choices":[{"delta":{"content":"你"}}]}`，
    最后一行是 `data: [DONE]`。这里只取 content 增量，其余（role/usage）忽略。
    """
    cfg = _ready_config()
    if not is_configured(cfg):
        raise LLMError("config", "还没配置模型服务", "打开「模型服务」填自己的 API Key，或切到平台额度")
    url, token = _endpoint(cfg)
    extra = _platform_headers(cfg, token)
    payload = {
        "model": effective_model(cfg),
        "messages": messages,
        "temperature": cfg.get("temperature", 0.6) if temperature is None else temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    try:
        resp = _post(url, token, payload, stream=True, extra_headers=extra, cfg=cfg)
    except LLMError:
        raise
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None
    except Exception as e:
        raise _net_error(e) from None

    with resp:
        for line in resp:
            line = line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    yield piece


def ping() -> dict:
    """连通性测试：发一句最短的问候，把耗时和模型名带回去。"""
    t0 = time.time()
    try:
        r = chat([{"role": "user", "content": "只回复两个字：正常"}], max_tokens=16,
                 temperature=0, timeout=45)
    except LLMError as e:
        return e.as_dict()
    return {"ok": True, "reply": r["text"][:40], "model": r["model"],
            "elapsed": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- 平台额度
def _valid_public_base(url: str) -> bool:
    """服务端下发的地址只接受 https 且不带查询串，避免被脏配置带到别处。"""
    try:
        p = urllib.parse.urlparse(url)
        return (p.scheme == "https" and bool(p.netloc)
                and not p.query and not p.fragment
                and not any(c.isspace() for c in url))
    except Exception:
        return False


def sync_platform_config(force: bool = False) -> dict:
    """向网关问一次「现在该连哪个地址」，结果缓存 6 小时。

    引导请求固定走出厂内置入口（带内置分享码）；整个同步失败静默——
    隧道没开、断网、网关升级中都不影响用内置值继续登录。
    """
    cfg = load_config()
    if not cfg.get("platform_auto", True):
        return cfg
    now = time.time()
    if not force and now - float(cfg.get("platform_sync_at") or 0) < CLIENT_CONFIG_TTL:
        return cfg
    patch: dict = {"platform_sync_at": int(now)}
    try:
        req = urllib.request.Request(
            BUILTIN_PLATFORM_BASE + CLIENT_CONFIG_PATH, method="GET")
        req.add_header("X-Share-Code", BUILTIN_PLATFORM_SHARE_CODE)
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        base = str(data.get("platform_base") or "").strip().rstrip("/")
        code = str(data.get("platform_share_code") or "").strip()
        if (_valid_public_base(base)
                and (not code or re.fullmatch(r"[A-Za-z0-9_-]{4,64}", code))):
            patch.update(platform_served_base=base,
                         platform_served_share_code=code)
        else:
            # 服务端没配（或配错）：清掉旧下发值，回落出厂内置
            patch.update(platform_served_base="", platform_served_share_code="")
    except Exception:
        pass
    return save_config(**patch)


def _platform_get(path: str, token: str) -> dict:
    cfg = sync_platform_config()
    base = (cfg.get("platform_base") or "").rstrip("/")
    if not base:
        raise LLMError("config", "还没填平台服务地址", "")
    req = urllib.request.Request(base + path, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    if cfg.get("platform_share_code"):
        req.add_header("X-Share-Code", cfg["platform_share_code"])
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-Platform-Token", token)   # 公网分享层会改写 Authorization
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT + 8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None
    except Exception as e:
        raise _net_error(e) from None


def _platform_post(path: str, body: dict, token: str) -> dict:
    cfg = sync_platform_config()
    base = (cfg.get("platform_base") or "").rstrip("/")
    if not base:
        raise LLMError("config", "还没填平台服务地址", "")
    req = urllib.request.Request(base + path, method="POST",
                                 data=json.dumps(body).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if cfg.get("platform_share_code"):
        req.add_header("X-Share-Code", cfg["platform_share_code"])
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-Platform-Token", token)   # 公网分享层会改写 Authorization
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT + 8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None
    except Exception as e:
        raise _net_error(e) from None


def balance() -> dict:
    """查平台余额。未配置平台地址时返回 ok=false，界面据此隐藏余额区。"""
    cfg = load_config()
    token = cfg.get("platform_token") or ""
    if not (cfg.get("platform_base") and token):
        return {"ok": False, "kind": "config", "error": "还没接入平台额度"}
    try:
        data = _platform_get("/v1/me", token)
    except LLMError as e:
        return e.as_dict()
    return {"ok": True,
            "balance": data.get("balance"),
            "currency": data.get("currency") or "CNY",
            "used_month": data.get("used_month"),
            "phone_masked": data.get("phone_masked") or "",
            "devices": data.get("devices") or [],
            "name": data.get("phone_masked") or data.get("name") or ""}


def redeem(code: str) -> dict:
    """用兑换码加额。码由服务端 `/admin/codes` 批量生成。"""
    code = (code or "").strip().upper()
    if not code:
        return {"ok": False, "kind": "input", "error": "请输入兑换码"}
    cfg = load_config()
    token = cfg.get("platform_token") or ""
    if not (cfg.get("platform_base") and token):
        return {"ok": False, "kind": "config", "error": "还没接入平台额度"}
    try:
        data = _platform_post("/v1/redeem", {"code": code}, token)
    except LLMError as e:
        return e.as_dict()
    return {"ok": True, "added": data.get("added"), "balance": data.get("balance")}


def order_create(amount_yuan: int) -> dict:
    """创建充值订单。金额整数（元）；支付通道由网关 PAY_PROVIDER 决定。

    返回 order_id / status；wechat_native 时带 code_url（二维码链接）。
    App 拿 order_id 轮询 order_query，status 变 paid 即到账。
    """
    try:
        yuan = int(amount_yuan)
    except (TypeError, ValueError):
        return {"ok": False, "kind": "input", "error": "金额不对"}
    if not 1 <= yuan <= 500:
        return {"ok": False, "kind": "input", "error": "单笔限额 1~500 元"}
    cfg = load_config()
    token = cfg.get("platform_token") or ""
    if not (cfg.get("platform_base") and token):
        return {"ok": False, "kind": "config", "error": "还没接入平台额度"}
    try:
        data = _platform_post("/v1/orders", {"amount": yuan}, token)
    except LLMError as e:
        return e.as_dict()
    return {"ok": True, "order_id": data.get("order_id"),
            "out_trade_no": data.get("out_trade_no"), "amount": data.get("amount"),
            "status": data.get("status"), "channel": data.get("channel"),
            "code_url": data.get("code_url", ""), "notice": data.get("notice", "")}


def order_query(order_id: int) -> dict:
    """查询充值订单状态；网关侧会把过期订单翻成 expired。"""
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return {"ok": False, "kind": "input", "error": "order_id 不对"}
    cfg = load_config()
    token = cfg.get("platform_token") or ""
    if not (cfg.get("platform_base") and token):
        return {"ok": False, "kind": "config", "error": "还没接入平台额度"}
    try:
        data = _platform_post("/v1/order/query", {"order_id": oid}, token)
    except LLMError as e:
        return e.as_dict()
    return {"ok": True, "order_id": oid, "status": data.get("status"),
            "amount": data.get("amount"), "balance": data.get("balance")}


def sms_send(account: str) -> dict:
    """请求平台发送验证码（手机号或邮箱，60 秒限频由网关把控）。"""
    account = (account or "").strip()
    if "@" in account:
        if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", account):
            return {"ok": False, "kind": "input", "error": "邮箱格式不对"}
    elif not re.fullmatch(r"1\d{10}", account):
        return {"ok": False, "kind": "input", "error": "请输入正确的手机号或邮箱"}
    cfg = load_config()
    if not cfg.get("platform_base"):
        return {"ok": False, "kind": "config", "error": "先填平台服务地址"}
    try:
        return _platform_post("/v1/sms/send", {"account": account}, "")
    except LLMError as e:
        return e.as_dict()


def auth_login(account: str, code: str, device_name: str = "") -> dict:
    """验证码登录（手机号或邮箱）：新号自动注册并领赠送额度，老号直接登录。

    令牌只是登录态，余额挂在账号上——卸载重装 / 换手机后
    重新登录即可找回，这是「充值不丢」的关键。
    """
    account = (account or "").strip()
    code = (code or "").strip()
    if "@" not in account and not re.fullmatch(r"1\d{10}", account):
        return {"ok": False, "kind": "input", "error": "请输入正确的手机号或邮箱"}
    if not code:
        return {"ok": False, "kind": "input", "error": "请输入验证码"}
    cfg = load_config()
    base = (cfg.get("platform_base") or "").rstrip("/")
    if not base:
        return {"ok": False, "kind": "config", "error": "先填平台服务地址"}
    try:
        data = _platform_post("/v1/auth", {"account": account, "code": code,
                                           "device_name": device_name or "RunnerCoach"}, "")
    except LLMError as e:
        return e.as_dict()
    if data.get("token"):
        save_config(platform_token=data["token"])
    return {"ok": True, "created": bool(data.get("created")),
            "phone_masked": data.get("phone_masked") or "", "balance": data.get("balance")}


def device_login(device_name: str = "") -> dict:
    """（旧版兼容）匿名设备令牌。新界面请用 sms_send + auth_login。

    匿名设备的余额没有账号兜底，卸载即失——网关保留此接口只为
    已发布的 v0.4.0 还能连上；后台可把匿名账号并入手机号账号。
    """
    cfg = load_config()
    base = (cfg.get("platform_base") or "").rstrip("/")
    if not base:
        return {"ok": False, "kind": "config", "error": "先填平台服务地址"}
    try:
        data = _platform_post("/v1/device", {"name": device_name or "RunnerCoach"}, "")
    except LLMError as e:
        return e.as_dict()
    if data.get("token"):
        save_config(platform_token=data["token"])
    return {"ok": True, "token_hint": (data.get("token") or "")[:4] + "…",
            "balance": data.get("balance")}
