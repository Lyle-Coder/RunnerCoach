"""国区 /gc-api/ 数据端点封装。所有方法都容错：取不到就返回 None，不影响整体同步。"""
from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlencode

from .net import GATE_HEADERS, NAV_HEADERS, HttpError, Net, extract_csrf

BASE = "https://connect.garmin.cn/gc-api"
APP = "https://connect.garmin.cn/app/"


class SessionExpired(RuntimeError):
    pass


class GarminCNClient:
    def __init__(self, net: Net, display_name: str | None = None):
        self.net = net
        self._display_name = display_name
        self.last_errors: list[str] = []
        self.last_status: int | None = None

    # ---------- 底层 ----------
    def csrf(self) -> str | None:
        return self.net.get_cookie("connect-csrf-token")

    def refresh_csrf(self) -> str | None:
        """从 /app/ 页的 <meta name="csrf-token"> 里重新取一次令牌。"""
        status, html = self.net.request("GET", APP, headers=NAV_HEADERS)
        if status != 200:
            return None
        token = extract_csrf(html)
        if token:
            self.net.set_cookie("connect-csrf-token", token)
            self.net.save_session()
        return token

    def _headers(self) -> dict:
        # GATE_HEADERS 是必须的：缺 Sec-Fetch-Site 时网关直接回空 403
        h = dict(GATE_HEADERS)
        h["Accept"] = "application/json, text/plain, */*"
        h["X-Requested-With"] = "XMLHttpRequest"
        h["Referer"] = "https://connect.garmin.cn/app/"
        csrf = self.csrf()
        if csrf:
            h["Connect-Csrf-Token"] = csrf
        return h

    def get(self, path: str, params: dict | None = None, *, tries: int | None = None,
            timeout: int = 30, retries: int = 3) -> Any:
        """`timeout` / `retries` 只给「出图前顺手刷一下」这类场景用：
        那里的请求失败无所谓，但不能让页面等 30 秒。"""
        url = BASE + path
        if params:
            url += "?" + urlencode(params)
        try:
            status, text = self.net.request("GET", url, headers=self._headers(),
                                            timeout=timeout, retries=retries)
        except Exception as e:  # 网络异常
            self.last_errors.append(f"{path}: {type(e).__name__} {e}")
            return None

        # 403 且 body 为空 = 网关没拿到 CSRF 令牌（令牌会随会话轮换），续一次再试
        if status == 403 and not text.strip() and self.refresh_csrf():
            try:
                status, text = self.net.request("GET", url, headers=self._headers(),
                                                timeout=timeout, retries=retries)
            except Exception as e:
                self.last_errors.append(f"{path}: {type(e).__name__} {e}")
                return None

        self.last_status = status
        if status in (401, 403):
            raise SessionExpired(f"{status} {path}")
        if status == 200 and text.strip():
            import json

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        if status != 404:
            self.last_errors.append(f"{path}: HTTP {status}")
        return None

    def post(self, path: str, body: dict | None = None, *, tries: int = 2) -> Any:
        """POST 一个 JSON 到 /gc-api/（创建训练课程 / 排程用）。"""
        import json

        url = BASE + path
        for attempt in range(tries):
            status, text = self.net.request("POST", url, json_body=body,
                                            headers=self._headers())
            # 403 空 body = CSRF 轮换，续一次再试
            if status == 403 and not (text or "").strip() and self.refresh_csrf():
                continue
            self.last_status = status
            if status in (401, 403):
                raise SessionExpired(f"{status} {path}")
            if status in (200, 201) and (text or "").strip():
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
            if attempt == tries - 1:
                self.last_errors.append(f"POST {path}: HTTP {status} {text[:200]}")
        return None

    def _first(self, candidates: list[tuple[str, dict | None]],
               *, timeout: int = 30, retries: int = 3) -> Any:
        for path, params in candidates:
            data = self.get(path, params, timeout=timeout, retries=retries)
            if data:
                return data
        return None

    def delete(self, path: str, *, tries: int = 2) -> bool:
        """DELETE 一个 /gc-api/ 资源（删除训练课程用）。"""
        url = BASE + path
        for attempt in range(tries):
            status, text = self.net.request("DELETE", url, headers=self._headers())
            if status == 403 and not (text or "").strip() and self.refresh_csrf():
                continue
            self.last_status = status
            if status in (401, 403):
                raise SessionExpired(f"{status} {path}")
            if status in (200, 201, 204):
                return True
            if attempt == tries - 1:
                self.last_errors.append(f"DELETE {path}: HTTP {status} {(text or '')[:200]}")
        return False

    # ---------- 账号 ----------
    def devices(self) -> list[dict]:
        """已绑定设备列表。接口不保证字段名，所以只做宽松提取。"""
        for path in ("/device-service/deviceregistration/devices",
                     "/device-service/deviceservice/device-info/devices"):
            try:
                data = self.get(path)
            except SessionExpired:
                raise
            except Exception:
                continue
            rows: list = []
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                for k in ("deviceInfos", "devices", "deviceList"):
                    v = data.get(k)
                    if isinstance(v, list):
                        rows = v
                        break
            if rows:
                return [r for r in rows if isinstance(r, dict)]
        return []

    def primary_device(self) -> str:
        """当前主设备名，例如「Forerunner 570」。取不到返回空串。"""
        rows = self.devices()
        if not rows:
            return ""

        def name_of(d: dict) -> str:
            for k in ("productDisplayName", "displayName", "deviceName",
                      "productName", "unitName"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        def ts_of(d: dict) -> str:
            for k in ("lastSyncTime", "lastUsedTime", "updateTime"):
                v = d.get(k)
                if isinstance(v, str):
                    return v
            return ""

        named = [d for d in rows if name_of(d)]
        if not named:
            return ""
        # 多设备时取最近同步的那台：用户戴哪块表跑，那块就是「当前设备」
        named.sort(key=ts_of, reverse=True)
        return name_of(named[0])

    def profile(self) -> dict | None:
        data = self.get("/userprofile-service/socialProfile")
        if isinstance(data, dict) and data.get("displayName"):
            self._display_name = data["displayName"]
        return data if isinstance(data, dict) else None

    @property
    def display_name(self) -> str:
        if not self._display_name:
            self.profile()
        if not self._display_name:
            raise SessionExpired("无法获取 displayName，会话可能已失效")
        return self._display_name

    def user_settings(self) -> dict | None:
        data = self.get("/userprofile-service/userprofile/settings")
        return data if isinstance(data, dict) else None

    # ---------- 日汇总 ----------
    def daily_summary(self, date: str) -> dict | None:
        dn = self.display_name
        return self._first(
            [
                (f"/usersummary-service/usersummary/daily/{dn}", {"calendarDate": date}),
                ("/usersummary-service/usersummary/daily", {"calendarDate": date}),
            ]
        )

    # ---------- 睡眠 ----------
    def sleep(self, date: str) -> dict | None:
        return self._first(
            [
                ("/wellness-service/wellness/dailySleepData", {"date": date, "nonSleepBufferMinutes": 60}),
                ("/wellness-service/wellness/dailySleepData", {"date": date}),
            ]
        )

    # ---------- HRV ----------
    def hrv(self, date: str) -> dict | None:
        return self._first([(f"/hrv-service/hrv/{date}", None)])

    def hrv_range(self, start: str, end: str) -> Any:
        return self._first([(f"/hrv-service/hrv/daily/{start}/{end}", None)])

    # ---------- 压力 / 身体电量 / 血氧 / 呼吸 ----------
    def stress(self, date: str) -> dict | None:
        return self._first([(f"/wellness-service/wellness/dailyStress/{date}", None)])

    def body_battery(self, start: str, end: str) -> Any:
        return self._first(
            [
                (
                    "/wellness-service/wellness/bodyBattery/reports/daily",
                    {"startDate": start, "endDate": end},
                )
            ]
        )

    def body_battery_day(self, date: str, *, timeout: int = 30,
                         retries: int = 3) -> Any:
        return self._first(
            [
                (f"/wellness-service/wellness/daily/bodyBattery/{date}", None),
                ("/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": date, "endDate": date}),
            ],
            timeout=timeout, retries=retries,
        )

    def spo2(self, date: str) -> Any:
        return self._first([(f"/wellness-service/wellness/daily/spo2/{date}", None)])

    def respiration(self, date: str) -> Any:
        return self._first([(f"/wellness-service/wellness/daily/respiration/{date}", None)])

    def heart_rate(self, date: str) -> Any:
        dn = self.display_name
        return self._first([(f"/wellness-service/wellness/dailyHeartRate/{dn}", {"date": date})])

    def steps_range(self, start: str, end: str) -> Any:
        return self._first([(f"/usersummary-service/stats/steps/daily/{start}/{end}", None)])

    # ---------- 活动 ----------
    def activities(self, limit: int = 30, start: int = 0) -> list:
        data = self._first(
            [
                (
                    "/activitylist-service/activities/search/activities",
                    {"limit": limit, "start": start},
                )
            ]
        )
        return data if isinstance(data, list) else []

    def activities_earliest(self, max_pages: int = 60, page_size: int = 60) -> dict:
        """翻到活动列表末尾，返回账号里**最早一条活动**的日期。

        活动列表按时间倒序，「本页不满一页」就等于「到底了」——这是数据源
        自己给出的「从哪天开始有记录」，比按年数硬猜准得多。补历史时用它
        当停止线，就不会为了一段根本没数据的年份白拉 365 天的日汇总。

        返回 `{date, exhausted, pages}`：
        - `exhausted=True` 才说明 date 是真正的第一条；
        - 页面翻到上限还没到底时 `exhausted=False`，调用方只能当参考。
        """
        oldest = None
        pages = 0
        exhausted = False
        cap = max(1, int(max_pages))
        for page in range(cap):
            items = self.activities(limit=page_size, start=page * page_size)
            pages += 1
            if not items:
                exhausted = True
                break
            for a in items:
                if not isinstance(a, dict):
                    continue
                d = (a.get("startTimeLocal") or "")[:10]
                if d and (oldest is None or d < oldest):
                    oldest = d
            if len(items) < page_size:
                exhausted = True
                break
        return {"date": oldest, "exhausted": exhausted, "pages": pages}

    def activity(self, activity_id: int | str) -> dict | None:
        data = self.get(f"/activity-service/activity/{activity_id}")
        return data if isinstance(data, dict) else None

    # ---------- 训练计划 / 佳明教练 ----------
    def active_plans(self, start_date: str | None = None) -> list:
        """当前激活的训练计划（含 Garmin Coach 自适应计划）。"""
        date = start_date or dt.date.today().isoformat()
        data = self.get("/trainingplan-service/trainingplan/active-plans", {"startDate": date})
        if isinstance(data, dict):
            return data.get("trainingPlanList") or []
        return []

    def adaptive_plan(self, plan_id: int | str) -> dict | None:
        """计划详情，taskList 里是逐日课表（含时长、目标配速、训练效果）。"""
        data = self.get(f"/trainingplan-service/trainingplan/fbt-adaptive/{plan_id}")
        return data if isinstance(data, dict) else None

    def calendar_month(self, year: int, month: int) -> dict | None:
        data = self.get(f"/calendar-service/year/{year}/month/{month}")
        return data if isinstance(data, dict) else None

    # ---------- 工具 ----------
    def ping(self) -> bool:
        try:
            return bool(self.profile())
        except SessionExpired:
            return False
