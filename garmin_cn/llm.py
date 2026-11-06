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
import urllib.request
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
    "custom": {
        "label": "自定义", "base_url": "", "model": "", "key_url": "",
        "note": "任意 OpenAI 兼容服务，自己填地址与模型名",
    },
}
DEFAULT_PROVIDER = "deepseek"

DEFAULTS = {
    "mode": "own",              # own=自配 API / platform=平台额度
    "provider": DEFAULT_PROVIDER,
    "base_url": "",             # 留空则用预设
    "model": "",                # 留空则用预设
    "api_key": "",
    "platform_base": "",        # 平台网关地址，如 https://ai.example.com
    "platform_token": "",
    "temperature": 0.6,
    "auto_advice": True,        # 打开看板时自动生成今日建议
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
    return cfg


def save_config(**patch) -> dict:
    """合并写入。只接受已知字段，防止界面传错键把配置写脏。"""
    cfg = load_config()
    for k, v in patch.items():
        if k in DEFAULTS:
            cfg[k] = v
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
        "base_url": cfg["base_url"] or prov["base_url"],
        "base_url_custom": bool(cfg["base_url"]),
        "model": cfg["model"] or prov["model"],
        "model_custom": bool(cfg["model"]),
        "api_key_hint": (key[:4] + "…（" + str(len(key)) + " 位）") if key else "",
        "has_key": bool(key),
        "platform_base": cfg["platform_base"],
        "platform_hint": (token[:4] + "…（" + str(len(token)) + " 位）") if token else "",
        "has_platform_token": bool(token),
        "temperature": cfg["temperature"],
        "auto_advice": cfg["auto_advice"],
        "configured": is_configured(cfg),
        "providers": [
            {"id": k, "label": v["label"], "base_url": v["base_url"],
             "model": v["model"], "key_url": v["key_url"], "note": v["note"]}
            for k, v in PROVIDERS.items()
        ],
    }


def is_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    if cfg.get("mode") == "platform":
        return bool(cfg.get("platform_base") and cfg.get("platform_token"))
    return bool(effective_base(cfg) and cfg.get("api_key"))


def effective_base(cfg: dict) -> str:
    if cfg.get("base_url"):
        return cfg["base_url"].rstrip("/")
    prov = PROVIDERS.get(cfg.get("provider") or DEFAULT_PROVIDER) or {}
    return (prov.get("base_url") or "").rstrip("/")


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
def _post(url: str, token: str, payload: dict, timeout: int = READ_TIMEOUT,
          stream: bool = False):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream" if stream else "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout)


def _http_error(e: urllib.error.HTTPError) -> LLMError:
    """把上游的状态码翻成「该怎么办」，原始报文留在 detail 里备查。"""
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
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

    code = e.code
    if code in (401, 403):
        return LLMError("auth", "密钥无效或没有权限，检查 API Key / 平台令牌", raw)
    if code == 402:
        return LLMError("quota", "额度不足，充值或换一个 key", raw)
    if code == 429:
        return LLMError("rate", "请求太频繁或额度用尽，等一会儿再试", raw)
    if code in (400, 422):
        return LLMError("request", f"请求被拒绝：{str(msg)[:160]}", raw)
    if code >= 500:
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
    cfg = load_config()
    if not is_configured(cfg):
        raise LLMError("config", "还没配置模型服务", "打开「模型服务」填自己的 API Key，或充额度")
    url, token = _endpoint(cfg)
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
        resp = _post(url, token, payload, timeout=timeout)
        raw = resp.read().decode("utf-8", "replace")
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
    cfg = load_config()
    if not is_configured(cfg):
        raise LLMError("config", "还没配置模型服务", "打开「模型服务」填自己的 API Key，或充额度")
    url, token = _endpoint(cfg)
    payload = {
        "model": effective_model(cfg),
        "messages": messages,
        "temperature": cfg.get("temperature", 0.6) if temperature is None else temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    try:
        resp = _post(url, token, payload, stream=True)
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
def _platform_get(path: str, token: str) -> dict:
    cfg = load_config()
    base = (cfg.get("platform_base") or "").rstrip("/")
    if not base:
        raise LLMError("config", "还没填平台服务地址", "")
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT + 8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None
    except Exception as e:
        raise _net_error(e) from None


def _platform_post(path: str, body: dict, token: str) -> dict:
    cfg = load_config()
    base = (cfg.get("platform_base") or "").rstrip("/")
    if not base:
        raise LLMError("config", "还没填平台服务地址", "")
    req = urllib.request.Request(base + path, method="POST",
                                 data=json.dumps(body).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
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


def sms_send(phone: str) -> dict:
    """请求平台发送短信验证码（60 秒限频由网关把控）。"""
    phone = (phone or "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        return {"ok": False, "kind": "input", "error": "手机号格式不对（大陆 11 位）"}
    cfg = load_config()
    if not cfg.get("platform_base"):
        return {"ok": False, "kind": "config", "error": "先填平台服务地址"}
    try:
        return _platform_post("/v1/sms/send", {"phone": phone}, "")
    except LLMError as e:
        return e.as_dict()


def auth_login(phone: str, code: str, device_name: str = "") -> dict:
    """验证码登录：新号自动注册并领赠送额度，老号直接登录。

    令牌只是登录态，余额挂在手机号账号上——卸载重装 / 换手机后
    重新登录即可找回，这是「充值不丢」的关键。
    """
    phone = (phone or "").strip()
    code = (code or "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        return {"ok": False, "kind": "input", "error": "手机号格式不对"}
    if not code:
        return {"ok": False, "kind": "input", "error": "请输入验证码"}
    cfg = load_config()
    base = (cfg.get("platform_base") or "").rstrip("/")
    if not base:
        return {"ok": False, "kind": "config", "error": "先填平台服务地址"}
    try:
        data = _platform_post("/v1/auth", {"phone": phone, "code": code,
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
