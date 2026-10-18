"""从常驻紧凑用量摘要计算历史统计，不读取会话分片。"""
import datetime as dt
import re
from collections import defaultdict

字段 = ("input", "output", "cache_read", "cache_write")
北京时间 = dt.timezone(dt.timedelta(hours=8))


def 查价(prices, model):
    if model in prices and not model.startswith("_"):
        return prices[model]
    normalized = re.sub(r"-\d{8}$", "", model)
    if normalized in prices and not normalized.startswith("_"):
        return prices[normalized]
    for prefix in sorted(prices, key=len, reverse=True):
        if not prefix.startswith("_") and model.startswith(prefix):
            return prices[prefix]
    return None


def 时间(value):
    return dt.datetime.fromtimestamp(value, 北京时间).isoformat(timespec="seconds") if value is not None else ""


def 厂商(model):
    for prefix, vendor in (("claude-", "Claude"), ("gpt-", "GPT"), ("glm-", "GLM"), ("deepseek-", "DeepSeek")):
        if model.startswith(prefix):
            return vendor
    return "其他"


def 摘要(files):
    """行序：时戳、模型、类型、角色、平台、四项token、5m、1h、请求数。"""
    buckets = {}
    for f in files:
        role = "主窗" if f["agent"] == "main" else "子代理"
        kind = "主窗" if role == "主窗" else (f["meta"].get("agentType") or "未知")
        for rec in f["state"]["usage_records"].values():
            key = (rec["at"], rec["model"], kind, role, rec["platform"])
            values = buckets.setdefault(key, [0] * 7)
            for i, k in enumerate((*字段, "cache_write_5m", "cache_write_1h")):
                values[i] += rec[k]
            values[6] += 1
    return [[*key, *value] for key, value in buckets.items()]


def 小时摘要(rows):
    buckets = {}
    for row in rows:
        stamp = row[0]
        key = (int(stamp // 3600) * 3600 if stamp is not None else None, *row[1:5])
        values = buckets.setdefault(key, [0] * 7 + [stamp])
        for i, value in enumerate(row[5:12]):
            values[i] += value
        if stamp is not None:
            values[7] = min(values[7], stamp)
    return [[*key, *value] for key, value in buckets.items()]


def 范围行(record, start, end):
    edges = set()
    for row in record.get("stats_hourly", []):
        stamp = row[0]
        if stamp is None:
            if start is None:
                yield row
        elif stamp <= end and (start is None or stamp + 3600 > start):
            if (start is None or stamp >= start) and stamp + 3600 <= end:
                yield row
            else:
                edges.add(stamp)
    if "stats_hourly" not in record:
        yield from record.get("stats", [])
    elif edges:
        for row in record.get("stats", []):
            if row[0] is not None and int(row[0] // 3600) * 3600 in edges:
                yield row


def 计算(records, prices, billing, notes, hours, end, project=None):
    start = end - hours * 3600 if hours else None
    period = 3600 if 1 <= hours <= 48 else 86400
    totals = dict(requests=0, tokens=0, input=0, output=0, cache_read=0, cache_write=0,
                  usd_all=0.0, usd_paid=0.0, usd_sub=0.0, missing_price_models=[])
    models, projects, types, roles, points = {}, {}, {}, {}, {}
    missing = set()
    earliest = None
    resolved_prices = {}

    def group(table, key, label):
        return table.setdefault(key, {**label, "requests": 0, "tokens": 0, "usd": 0.0})

    def bill(model):
        for prefix in sorted(billing, key=len, reverse=True):
            if not prefix.startswith("_") and model.startswith(prefix):
                return {"按量美元": "按量", "订阅": "订阅"}.get(billing[prefix], "未知")
        return "未知"

    for sid, record in records.items():
        pid = record["project"]
        if project is not None and pid != project:
            continue
        for row in 范围行(record, start, end):
            stamp, model, kind, role, platform, inp, out, cr, cw, cw5, cw1, requests = row[:12]
            precise_stamp = row[12] if len(row) > 12 else stamp
            if stamp is not None and (stamp > end or (start is not None and stamp < start)):
                continue
            if stamp is None and hours:
                continue
            if stamp is not None:
                earliest = precise_stamp if earliest is None else min(earliest, precise_stamp)
            tokens = inp + out + cr + cw
            if model not in models:
                resolved_prices[model] = 查价(prices, model)
                models[model] = dict(model=model, vendor=厂商(model), billing=bill(model), requests=0, tokens=0,
                                     input=0, output=0, cache_read=0, cache_write=0, usd=0.0 if resolved_prices[model] is not None else None)
            m = models[model]
            p = resolved_prices[model]
            usd = (inp*p[0]+cr*p[1]+cw5*p[2]+cw1*p[0]*2+out*p[3])/1000000 if p else None
            if usd is None:
                missing.add(model)
            else:
                totals["usd_all"] += usd
                if m["billing"] == "按量": totals["usd_paid"] += usd
                elif m["billing"] == "订阅": totals["usd_sub"] += usd
                m["usd"] += usd
            for obj in (totals, m):
                obj["requests"] += requests
                obj["tokens"] += tokens
                for k, value in zip(字段, (inp, out, cr, cw)):
                    obj[k] += value
            name = notes.get("projects", {}).get(pid, {}).get("name") or record["cwd"] or pid
            targets = [group(projects, pid, dict(id=pid, name=name)), group(types, kind, dict(type=kind)), group(roles, role, dict(role=role))]
            if stamp is not None:
                bucket = int((stamp + 28800) // period) * period - 28800
                point = points.setdefault(bucket, dict(t=时间(bucket), requests=0, tokens=0, usd=0.0, by_model={}))
                point["by_model"][model] = point["by_model"].get(model, 0) + tokens
                targets.append(point)
            for target in targets:
                target["requests"] += requests
                target["tokens"] += tokens
                target["usd"] += usd or 0.0
    totals["missing_price_models"] = sorted(missing)
    for k in ("usd_all", "usd_paid", "usd_sub"):
        totals[k] = round(totals[k], 2)
    for table in (models, projects, types, roles, points):
        for row in table.values():
            if row["usd"] is not None:
                row["usd"] = round(row["usd"], 2)
    return {"range": {"hours": hours, "from": 时间(start if start is not None else earliest), "to": 时间(end)},
            "totals": totals, "models": sorted(models.values(), key=lambda m: (-m["tokens"], m["model"])),
            "groups": {"by_project": list(projects.values()), "by_agent_type": list(types.values()), "by_role": list(roles.values())},
            "trend": {"bucket": "hour" if period == 3600 else "day", "points": [points[t] for t in sorted(points)]}}
