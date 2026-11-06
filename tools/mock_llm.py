# -*- coding: utf-8 -*-
"""本地假模型：不花钱、不联网，用来调试 AI 页面的排版和交互。

为什么需要它
------------
改一版卡片布局就要真调一次 DeepSeek，一天能烧掉几块钱，还慢（每次 3~8 秒）。
这个脚本假装成 OpenAI 兼容服务，按提示词里的关键词返回结构化的假数据，
页面该怎么渲染就怎么渲染。

用法
----
    py -3 tools/mock_llm.py 8799
    # 然后在 App「模型服务」里填：地址 http://127.0.0.1:8799/v1，模型 mock，Key 随便

只在 debug 时用，别指向公网。
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DAILY = {
    "headline": "轻松跑 45 分钟，压住心率",
    "focus": "别被恢复度 63 骗了，睡眠债还在",
    "dos": ["下午 5 点后开跑，心率压在 138 以内",
            "跑完补 30 g 蛋白，别只喝水",
            "11 点前躺下，今晚补足 8 小时"],
    "donts": ["不加间歇，不追配速", "不要空腹跑超过 60 分钟"],
    "watch": "静息心率 53 已高于基线 50，连升三天就减量",
    "why": "恢复度 63 属「一般」，主因是 HRV 61 ms 低于基线 70、静息心率高出 3 bpm；"
           "睡眠 6.2 小时也不够。ACWR 1.02 负荷平稳，所以不必停练，但今天只做有氧，"
           "把强度留给周四的节奏跑。",
}

WEIGHT = {
    "summary": "6 周降 0.9 kg，节奏稳",
    "trend": "down",
    "rate_verdict": "周均 −0.15 kg，低于 −0.5% 的目标速率，但同期跑量涨了 18%，属正常",
    "actions": ["每日蛋白保持 130 g 以上", "长距离前一天碳水加到 300 g",
                "每周固定晨起空腹称重，取七天均值看趋势"],
    "cautions": ["体脂 17.1% 已到备赛合适区间，别再往下压"],
    "nutrition": "训练日碳水 295 g，休息日可降到 240 g，热量别低于 2100 kcal",
}

ACTIVITY = {
    "title": "配速稳，后程略掉",
    "summary": "21.1 km、配速 5'12\"/km，前半 5'05\" 后半 5'19\"，掉速 4.6%，"
               "心率 152/176 属节奏跑区间，符合本次课的目标。",
    "good": ["前半程没有抢速度，5'05\" 稳定输出", "爬升 210 m 下心率没飙到阈值以上"],
    "improve": ["18 km 后掉速 15 秒，补给可能偏晚", "步频 168 偏低，后程靠跨步代偿"],
    "next": "下次同配速跑，10 km 处补一支胶，步频提到 174",
}

CHAT = ("从今天的数据看，恢复度 63、睡眠 6.2 小时，HRV 低于基线，属于「能练但别硬顶」的状态。"
        "今天的处方是 45 分钟轻松跑，心率压在 138 以内——这个强度不会加深疲劳，"
        "又能维持有氧刺激。真正要解决的是睡眠债：连续两晚不足 6.5 小时，"
        "今晚补到 8 小时，明天的恢复度大概率回到 75 以上。")


def answer(prompt: str) -> str:
    """按提示词里要求的字段名判断该返回哪种假数据。

    只看 JSON 字段名，不看中文关键词——训练上下文里本来就会出现「体重」「训练记录」
    这些词，用中文判断会把三种场景串味（早期版本就踩过这个坑）。
    """
    if '"rate_verdict"' in prompt:
        return json.dumps(WEIGHT, ensure_ascii=False)
    if '"headline"' in prompt:
        return json.dumps(DAILY, ensure_ascii=False)
    if '"improve"' in prompt:
        return json.dumps(ACTIVITY, ensure_ascii=False)
    return CHAT


class H(BaseHTTPRequestHandler):
    server_version = "mock-llm/0.1"

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        msgs = body.get("messages") or []
        prompt = "\n".join(str(m.get("content") or "") for m in msgs)
        text = answer(prompt)
        out = {
            "id": "mock-1", "object": "chat.completion", "model": body.get("model") or "mock",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 300, "total_tokens": 1100},
        }
        raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f"mock LLM on http://127.0.0.1:{port}/v1  (Ctrl+C 退出)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
