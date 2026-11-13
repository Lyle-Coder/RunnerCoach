"""COROS structured workouts, using the live MCP contract (2026-11-13).

Reference: https://github.com/coroslab/COROS-MCP/blob/main/docs/en/tools.md
Nested course/section fields were checked against authenticated tools/list.
Conversion and preview are offline. Only submit() writes to COROS.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .coros_mcp import McpError
from .sports import number

_PREVIEWS = {}
_LOCK = threading.Lock()
TTL = 1800
SECTION = {"warmup": 1, "interval": 2, "other": 2, "recovery": 3, "rest": 3, "cooldown": 4}
INTENSITY = {"heart.rate.zone": 1, "pace.zone": 2, "power.zone": 4}


def _integer(value, low, high, label):
    v = number(value)
    if v is None or not low <= v <= high or int(v) != v:
        raise ValueError(f"{label}必须为 {low}–{high} 范围内的整数")
    return int(v)


def _section(step: dict, no_intensity=False, member=False) -> dict:
    if step.get("type") == "RepeatGroupDTO":
        if member:
            raise ValueError("高驰不支持嵌套重复组，不能无损导入此课程")
        return {"intervalGroup": True,
                "repeats": _integer(step.get("numberOfIterations"), 1, 20, "重复次数"),
                "sets": [_section(s, no_intensity, True) for s in step.get("workoutSteps") or []]}
    kind = (step.get("stepType") or {}).get("stepTypeKey")
    if kind not in SECTION or (member and SECTION[kind] not in (2, 3)):
        raise ValueError("此训练包含高驰 MCP 无法表达的步骤")
    end = (step.get("endCondition") or {}).get("conditionTypeKey")
    if end not in ("distance", "time", "lap"):
        raise ValueError("此训练包含不支持的结束条件，不能自动改成时间目标")
    value = number(step.get("endConditionValue"))
    if value is None or value <= 0:
        raise ValueError("训练步骤缺少有效的时间或距离")
    section = {"sectionType": SECTION[kind], "targetType": {"distance": 1, "time": 2, "lap": 4}[end]}
    if end != "lap":
        section["targetValue"] = max(1, round(value))
    if no_intensity:
        return section
    target = (step.get("targetType") or {}).get("workoutTargetTypeKey")
    if target not in INTENSITY:
        raise ValueError("部分步骤没有可转换的强度目标；可明确选择「不设置强度提醒」后重新预览")
    a, b = number(step.get("targetValueOne")), number(step.get("targetValueTwo"))
    if a is None or b is None:
        raise ValueError("强度目标不完整")
    if target == "pace.zone":
        if min(a, b) <= 0:
            raise ValueError("配速目标无效")
        a, b = 1000 / a, 1000 / b  # Garmin m/s -> COROS integer sec/km.
    section.update(intensityType=INTENSITY[target],
                   intensityValueStart=round(min(a, b)), intensityValueEnd=round(max(a, b)))
    return section


def from_dto(payload: dict, *, no_intensity=False, trail=False) -> dict:
    """Reuse the generated training structure, preserving repeats and physical units."""
    sport = (payload.get("sportType") or {}).get("sportTypeKey")
    if sport not in ("running", "cycling"):
        raise ValueError("高驰 MCP 目前只支持创建跑步、越野跑和骑行训练")
    sections, instructions = [], []
    for segment in payload.get("workoutSegments") or []:
        if (segment.get("sportType") or {}).get("sportTypeKey") != sport:
            raise ValueError("不能将多运动混合课程合并成单项训练")
        for step in segment.get("workoutSteps") or []:
            sections.append(_section(step, no_intensity))
            for part in step.get("workoutSteps") or [step]:
                if part.get("description"):
                    instructions.append(str(part["description"]))
    desc = str(payload.get("description") or "按课程步骤完成训练。")
    if instructions:
        desc += "\n" + "；".join(dict.fromkeys(instructions))
    metrics = {p.get("intensityType") for s in sections for p in (s.get("sets") or [s])}
    units = {1: "心率按 bpm 控制", 2: "配速按实际每公里用时控制", 4: "功率按瓦数控制"}
    desc += "\n" + ("不设置强度提醒，按文字说明和体感执行。" if no_intensity else
                     "；".join(units[k] for k in sorted(metrics - {None})) + "，具体目标见各步骤。")
    course = {"sportType": 5 if trail else 2 if sport == "cycling" else 1,
              "courseName": str(payload.get("workoutName") or "训练")[:100],
              "courseDescription": desc, "sections": sections}
    validate(course)
    return course


def build(info: dict, *, no_intensity=False) -> tuple[list, list]:
    """Convert one locally generated session. Unsupported companion strength stays explicit."""
    from . import workout
    from .sports import item_family
    if not info or info.get("is_rest") or info.get("rest"):
        raise ValueError("休息日没有可导入的训练")
    warnings = []
    if info.get("kind"):
        payloads = workout.build_all(info)
        strength = [p for p in payloads if (p.get("sportType") or {}).get("sportTypeKey") == "strength_training"]
        if strength:
            warnings.append("高驰 MCP 不支持创建力量课程；本次只导入同日跑步，力量部分需在高驰 App 单独安排。")
        payloads = [p for p in payloads if (p.get("sportType") or {}).get("sportTypeKey") in ("running", "cycling")]
        if not payloads:
            raise ValueError("这一天只有力量或休息内容，高驰 MCP 无可导入的跑步/骑行课程")
    elif item_family(info) == "bike" and info.get("steps"):
        payloads = [workout.build_cycling(info)]
    else:
        kind = item_family(info)
        if kind not in ("run", "bike"):
            raise ValueError("只支持导入跑步或骑行训练")
        title = str(info.get("name") or info.get("type") or "训练")
        if any(word in title for word in ("休息", "已完成", "已训练", "数据不足", "力量")):
            raise ValueError("这条建议没有可执行的跑步/骑行课程")
        km, minutes = number(info.get("km"), 0), number(info.get("minutes"), 0)
        if km <= 0 and minutes <= 0:
            raise ValueError("训练没有确定的距离或时长，请先完善训练目标")
        if not no_intensity:
            raise ValueError("此建议尚无逐步强度目标；请明确选择「不设置强度提醒」，按原文字说明执行")
        step = workout._step(1, "other", "distance" if km > 0 else "time",
                             km * 1000 if km > 0 else minutes * 60)
        desc = "；".join(str(info.get(k) or "") for k in ("detail", "effect", "note"))
        payloads = [workout._workout(title, desc, workout.SPORT_BIKE if kind == "bike" else workout.SPORT_RUN,
                                   [workout._seg(workout.SPORT_BIKE if kind == "bike" else workout.SPORT_RUN, [step])])]
        warnings.append("原建议只有总时长/距离和文字说明，本次不会新增间歇组或强度提醒。")
    courses = [from_dto(p, no_intensity=no_intensity, trail=info.get("sport_key") == "trail_running") for p in payloads]
    if no_intensity:
        warnings.append("已按你的选择移除强度提醒，保留时间/距离目标及训练说明。")
    return courses, warnings


def validate(course: dict) -> None:
    sport = course.get("sportType")
    if sport not in (1, 2, 5) or any(k in course for k in ("dayNo", "intensityType", "strengthBodypart")):
        raise ValueError("训练运动类型或课程字段无效")
    if not str(course.get("courseName") or "").strip() or len(course["courseName"]) > 100 or not str(course.get("courseDescription") or "").strip():
        raise ValueError("训练名称和说明不能为空，名称最多 100 字")
    def plain(s, member=False):
        if s.get("sectionType") not in ((2, 3) if member else (1, 2, 3, 4)):
            raise ValueError("步骤类型不受支持")
        target = s.get("targetType")
        if target not in ((1, 3, 4) if sport == 5 else (1, 2, 4)):
            raise ValueError("高驰越野跑不支持按时间结束步骤；请使用距离/爬升目标或选择跑步模式")
        if target == 4:
            if "targetValue" in s:
                raise ValueError("手动结束步骤不应设置目标数值")
        else:
            _integer(s.get("targetValue"), 1, 2147483647, "步骤目标")
        metric = s.get("intensityType")
        if metric is None:
            if any(k.startswith("intensity") or k == "sectionIntensity" for k in s):
                raise ValueError("缺少强度指标")
            return
        if metric not in ((1, 4) if sport == 2 else (1, 2, 4)):
            raise ValueError("强度指标不受支持")
        bounds = {1: (30, 240), 2: (120, 1499), 4: (10, 2000)}[metric]
        for k in ("intensityValueStart", "intensityValueEnd"):
            _integer(s.get(k), *bounds, "强度目标")
        if any(k in s for k in ("sectionIntensity", "intensityPercentStart", "intensityPercentEnd")):
            raise ValueError("不能混合绝对值、分区和百分比目标")
    sections = course.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("课程没有训练步骤")
    for s in sections:
        if s.get("intervalGroup"):
            if set(s) != {"intervalGroup", "repeats", "sets"} or not s["sets"]:
                raise ValueError("重复组结构无效")
            _integer(s["repeats"], 1, 20, "重复次数")
            for member in s["sets"]:
                if member.get("intervalGroup"):
                    raise ValueError("高驰不支持嵌套重复组")
                plain(member, True)
        else:
            plain(s)


def validate_schema(value, schema, root=None):
    """Check supported JSON-Schema constraints before writing; server still validates semantics."""
    root = root or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValueError("高驰工具结构已变化，请更新 App")
        target = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return validate_schema(value, target, root)
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    kind = schema.get("type")
    if kind in kinds and (not isinstance(value, kinds[kind]) or kind in ("integer", "number") and isinstance(value, bool)):
        raise ValueError("高驰工具参数类型已变化，请更新 App")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("高驰工具参数取值已变化，请更新 App")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        if any(k not in value for k in schema.get("required") or []):
            raise ValueError("高驰接口新增了必填参数，请更新 App")
        for k, v in value.items():
            if k not in props:
                raise ValueError("高驰接口已不支持此训练字段，请更新 App")
            validate_schema(v, props[k], root)
    elif isinstance(value, list):
        for v in value:
            validate_schema(v, schema.get("items") or {}, root)
    for key, failed in (("minimum", lambda n: value < n), ("maximum", lambda n: value > n),
                        ("minLength", lambda n: len(value) < n), ("maxLength", lambda n: len(value) > n),
                        ("minItems", lambda n: len(value) < n), ("maxItems", lambda n: len(value) > n)):
        if key in schema and failed(schema[key]):
            raise ValueError("训练超出高驰接口允许的范围")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _db(home):
    path = Path(home) / "data" / "coros_workouts.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS imports (key TEXT PRIMARY KEY, state TEXT NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL, result TEXT NOT NULL, updated REAL NOT NULL)")
    conn.commit()
    return conn


def prepare(home, courses, cloud, *, date="", warnings=None, today=None):
    """Freeze exactly what the athlete previews. No COROS requests and no remote writes."""
    date = str(date or "")
    if date:
        d, now = dt.date.fromisoformat(date), today or dt.date.today()
        if not now <= d <= now + dt.timedelta(days=90):
            raise ValueError("单次训练只能安排到今天至未来 90 天；更远的训练可先存入训练库")
    binding = cloud.binding() if cloud.status().get("authorized") else ""
    jobs = []
    for course in courses:
        validate(course)
        tool = "createScheduledWorkout" if date else "createSingleWorkout"
        args = {"course": copy.deepcopy(course)}
        if date:
            args["date"] = date.replace("-", "")
        key = hashlib.sha256(_json([binding, tool, args]).encode()).hexdigest()
        jobs.append({"key": key, "tool": tool, "arguments": args})
    if not jobs:
        raise ValueError("没有可导入的课程")
    token = secrets.token_urlsafe(24)
    preview = {"home": str(Path(home).resolve()), "binding": binding, "jobs": jobs,
               "expires": time.time() + TTL}
    with _LOCK:
        for old in [k for k, p in _PREVIEWS.items() if p["expires"] <= time.time()]:
            _PREVIEWS.pop(old, None)
        _PREVIEWS[token] = preview
    return {"ok": True, "preview_id": token, "date": date, "destination": "calendar" if date else "library",
            "courses": copy.deepcopy(courses), "warnings": list(warnings or []), "authorized": bool(binding)}


def local_session(home, db, date="", sport="auto") -> dict:
    """Use the same local planner as the plan page; never infer steps from a remote title."""
    from . import raceplan
    from .metrics import derive
    from .plan import from_coach
    from .store import Store
    from .sports import preferred
    day = dt.date.fromisoformat(date) if date else dt.date.today()
    if sport not in ("auto", "run", "bike"):
        raise ValueError("请选择跑步或骑行")
    store = Store(db)
    try:
        res = derive(store, day.isoformat())
    finally:
        store.conn.close()
    chosen = preferred(res) if sport == "auto" else sport
    res["preferred_sport"] = chosen
    if chosen == "run" and raceplan.available():
        info = raceplan.day_info(day)
        if info and not info.get("is_rest"):
            return info
    # Readiness-based recommendations only exist for today, not as forecasts.
    if day != dt.date.today():
        raise ValueError("该日期没有本地备赛课；恢复度建议只能从今日生成，再选择导入日期")
    data = from_coach(res, day.isoformat())
    items = (data.get("today") or {}).get("items") or []
    if not items:
        raise ValueError((data.get("today") or {}).get("note") or "今天是休息日，没有可导入的训练")
    return items[0]


def _snapshot(home, token, cloud):
    with _LOCK:
        p = copy.deepcopy(_PREVIEWS.get(str(token)))
    if not p or p["expires"] <= time.time() or p["home"] != str(Path(home).resolve()):
        raise ValueError("预览已过期，请重新预览后导入")
    if not p["binding"] or cloud.binding() != p["binding"]:
        raise ValueError("高驰授权已变化，请授权后重新预览")
    return p


def _record(conn, key, state, result):
    conn.execute("UPDATE imports SET state=?, result=?, updated=? WHERE key=?", (state, _json(result), time.time(), key))
    conn.commit()


def recent_imports(home, cloud) -> list:
    """Local journal for the currently authorized account, including interrupted writes."""
    if not cloud.status().get("authorized") or not (Path(home) / "data" / "coros_workouts.sqlite3").is_file():
        return []
    binding = cloud.binding()
    conn = _db(home)
    rows = []
    try:
        for row in conn.execute("SELECT * FROM imports ORDER BY updated DESC LIMIT 200"):
            args = json.loads(row["arguments"])
            key = hashlib.sha256(_json([binding, row["tool"], args]).encode()).hexdigest()
            if key != row["key"]:
                continue
            rows.append({"import_key": key, "name": args["course"]["courseName"],
                         "date": args.get("date") or "", "state": row["state"]})
            if len(rows) == 20:
                break
    finally:
        conn.close()
    return rows


def check_import(home, key, cloud):
    """Recover a prior operation from the journal after a process restart. Read only remotely."""
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("导入记录无效")
    conn = _db(home)
    try:
        row = conn.execute("SELECT * FROM imports WHERE key=?", (key,)).fetchone()
        if not row:
            raise ValueError("未找到该导入记录")
        args = json.loads(row["arguments"])
        expected = hashlib.sha256(_json([cloud.binding(), row["tool"], args]).encode()).hexdigest()
        if key != expected:
            raise ValueError("这条导入记录不属于当前高驰授权")
        token = secrets.token_urlsafe(24)
        with _LOCK:
            _PREVIEWS[token] = {"home": str(Path(home).resolve()), "binding": cloud.binding(),
                                "expires": time.time() + TTL,
                                "jobs": [{"key": key, "tool": row["tool"], "arguments": args}]}
    finally:
        conn.close()
    try:
        return submit(home, token, cloud, check_only=True)
    finally:
        with _LOCK:
            _PREVIEWS.pop(token, None)


def _objects(result):
    """Structured MCP data or inline JSON inside text; do not coerce 64-bit IDs to floats."""
    if isinstance(result, dict):
        yield result
        for v in result.values():
            if isinstance(v, (dict, list, str)):
                yield from _objects(v)
    elif isinstance(result, list):
        for v in result:
            yield from _objects(v)
    elif isinstance(result, str):
        # The live COROS server wraps tool text in a JSON string, including
        # escaped inline course JSON. Decode the wrapper before locating it.
        text = result
        for _ in range(3):
            try:
                decoded = json.loads(text)
            except ValueError:
                break
            if isinstance(decoded, (dict, list)):
                yield from _objects(decoded)
                return
            if not isinstance(decoded, str) or decoded == text:
                break
            text = decoded
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            try:
                obj, _ = decoder.raw_decode(text[m.start():])
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield from _objects(obj)


def _ids(result, field):
    found = []
    for obj in _objects(result):
        value = obj.get(field)
        if isinstance(value, (int, str)) and re.fullmatch(r"\d+", str(value)):
            found.append(str(value))
    # COROS currently returns human-readable text for creation results.
    labels = r"(?:Workout\s*ID|workoutId)" if field == "workoutId" else r"(?:idInPlan)"
    for part in result.get("content") or []:
        found.extend(re.findall(labels + r"[\s*`]*[:：=][\s*`]*(\d+)", str(part.get("text") or ""), flags=re.I))
    return list(dict.fromkeys(found))


def _verify(cloud, job, result):
    args = job["arguments"]
    scheduled = job["tool"] == "createScheduledWorkout"
    field = "idInPlan" if scheduled else "workoutId"
    for handle in _ids(result, field):
        query = {field: handle}
        if scheduled:
            query["date"] = args["date"]
        details = cloud.call("queryScheduledWorkoutDetails" if scheduled else "queryWorkoutDetails", query)
        if any(all(obj.get(k) == v for k, v in args["course"].items()) for obj in _objects(details)):
            return {field: handle}
    return None


def submit(home, token, cloud, *, check_only=False):
    """Durably reserve before write. Crashes/timeouts remain blocked until reconciliation."""
    preview = _snapshot(home, token, cloud)
    catalog = {t["name"]: t for t in cloud.tools()}
    for job in preview["jobs"]:
        read_tool = "queryScheduledWorkoutDetails" if job["tool"] == "createScheduledWorkout" else "queryWorkoutDetails"
        if job["tool"] not in catalog or read_tool not in catalog:
            raise ValueError("当前高驰连接未开放所需训练写入接口，请更新授权或稍后再试")
        validate_schema(job["arguments"], catalog[job["tool"]]["inputSchema"])
    results = []
    conn = _db(home)
    try:
        for job in preview["jobs"]:
            key = job["key"]
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM imports WHERE key=?", (key,)).fetchone()
            fresh = row is None and not check_only
            if fresh:
                conn.execute("INSERT INTO imports VALUES (?,?,?,?,?,?)", (key, "pending", job["tool"], _json(job["arguments"]), "{}", time.time()))
            conn.commit()
            if row and row["state"] == "verified":
                results.append({"ok": True, "duplicate": True, "name": job["arguments"]["course"]["courseName"]})
                continue
            saved = json.loads(row["result"]) if row else {}
            if fresh:
                try:
                    saved = cloud.call(job["tool"], job["arguments"])
                    _record(conn, key, "unverified", saved)
                except Exception:
                    # Never retry an ambiguous write, even on the next app start.
                    _record(conn, key, "uncertain", {})
            if not fresh and row is None:
                results.append({"ok": False, "error": "这条课程还没有提交"})
                continue
            verified = None
            try:
                verified = _verify(cloud, job, saved)
                if not verified and check_only:
                    a = job["arguments"]
                    lookup = cloud.call("queryTrainingSchedule", {"startDate": a["date"], "endDate": a["date"]}) if "date" in a else cloud.call("queryWorkoutLibrary", {"sportType": a["course"]["sportType"], "courseType": "mcp"})
                    verified = _verify(cloud, job, lookup)
            except Exception:
                pass  # Saved may be real even when its read-back is temporarily unavailable.
            if verified:
                _record(conn, key, "verified", verified)
                results.append({"ok": True, "name": job["arguments"]["course"]["courseName"]})
            else:
                results.append({"ok": False, "uncertain": True, "name": job["arguments"]["course"]["courseName"],
                                "error": "导入结果尚未确认，已阻止重复提交。请点「检查导入结果」，或到高驰 App 核对。"})
    finally:
        conn.close()
    return {"ok": all(r["ok"] for r in results), "items": results,
            "uncertain": any(r.get("uncertain") for r in results),
            "message": "高驰账户中的课程已核验。请打开高驰 App 同步到设备。" if all(r["ok"] for r in results) else "部分课程尚未确认，请核对后再操作。"}


def push_cli(args, db, home) -> int:
    """CLI route; dry runs are completely offline, bulk requests validate before writing."""
    from . import raceplan
    from .coros_mcp import CorosMcp
    try:
        if args.replace:
            raise ValueError("高驰不支持通过 MCP 删除课程，不能使用 --replace")
        bulk = args.all or args.week is not None or args.start or args.end
        if args.sport == "bike" and bulk:
            raise ValueError("骑行恢复度处方只支持单日，使用 --date 选择今日")
        if bulk:
            if args.all or args.week is not None:
                if not raceplan.available():
                    raise ValueError("未配置本地备赛课表")
                if args.all:
                    start, end = raceplan.span()
                else:
                    start, end = raceplan._mp().date_of(args.week, 0), raceplan._mp().date_of(args.week, 6)
            else:
                start = dt.date.fromisoformat(args.start or args.end)
                end = dt.date.fromisoformat(args.end or args.start)
        else:
            start = end = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
        if start > end or (end - start).days > 366:
            raise ValueError("日期区间无效或超过一年")
        # Keep old issuer when authorized; otherwise gateway discovery is used on login.
        cloud = CorosMcp(home, "auto")
        prepared = []
        day = start
        while day <= end:
            if bulk:
                info = raceplan.day_info(day) if raceplan.available() else None
                if not info or info.get("is_rest"):
                    day += dt.timedelta(days=1)
                    continue
            else:
                info = local_session(home, db, day.isoformat(), args.sport)
            courses, warnings = build(info, no_intensity=args.no_intensity)
            target_date = "" if args.library else day.isoformat()
            preview = prepare(home, courses, cloud, date=target_date, warnings=warnings)
            prepared.append((day, preview))
            day += dt.timedelta(days=1)
        if not prepared:
            raise ValueError("区间内没有可导入的本地训练")
        if not args.dry_run and not cloud.status()["authorized"]:
            raise ValueError("请先运行 run.bat coros mcp-login 授权高驰")
        failed = False
        for day, p in prepared:
            label = "、".join(c["courseName"] for c in p["courses"])
            print(f"{day} {label} → " + ("高驰训练库" if args.library else "高驰训练日历"))
            for warning in p["warnings"]:
                print("  提示：" + warning)
            if args.dry_run:
                print("  演练，未联网提交")
                if args.verbose:
                    print(json.dumps(p["courses"], ensure_ascii=False, indent=2))
            else:
                result = submit(home, p["preview_id"], cloud)
                print("  " + result["message"])
                if not result["ok"]:
                    failed = True
                    for item in result["items"]:
                        if item.get("error"):
                            print("  " + item["error"])
                    break  # Stop a batch at the first ambiguous result.
        return 1 if failed else 0
    except (ValueError, McpError) as e:
        print("× " + str(e))
        return 2
