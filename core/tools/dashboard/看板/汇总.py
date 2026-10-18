"""由索引生成不可变接口快照；金额只在输出边界舍入。"""
import copy
import json
import re
import threading
import time
from collections import defaultdict, OrderedDict

from .索引器 import 字段, 入账, 时刻, 原子写, 安全文字, 分片重建中
from .统计 import 摘要 as 统计摘要, 计算 as 计算统计, 小时摘要, 查价


def 合计桶():
    return dict(agents=0, turns=0, input=0, output=0, cache_read=0, cache_write=0, usd_paid=0.0, usd_ref=0.0)


def 金额相加(left, right):
    return None if left is None or right is None else left + right


def 金额舍入(value):
    return None if value is None else round(value, 2)


def 累加(dest, source):
    for key in ("turns", *字段):
        dest[key] += source[key]
    for key in ("usd_paid", "usd_ref"):
        dest[key] = 金额相加(dest[key], source[key])


def 完成(bucket):
    result = {k: v for k, v in bucket.items() if not k.startswith("_")}
    denom = result["input"] + result["cache_read"]
    result["hit_rate"] = result["cache_read"] / denom if denom else 0
    result["usd_paid"] = 金额舍入(result["usd_paid"])
    result["usd_ref"] = 金额舍入(result["usd_ref"])
    return result


class 看板汇总:
    def __init__(self, index):
        self.index = index
        self.lock = threading.RLock()
        self.notes_lock = threading.Lock()
        self.config = index.base / "配置"
        self.config_stats = {}
        self.prices, self.billing, self.thresholds = {}, {}, {}
        self.notes = {"projects": {}, "sessions": {}}
        self.records = {}
        self.detail_cache = OrderedDict()
        self.detail_lock = threading.Lock()
        self.stats_cache = OrderedDict()
        self.stats_revision = 0
        self.载入配置()
        try:
            with (self.config / "备注.json").open(encoding="utf-8") as source:
                self.notes = json.load(source)
        except FileNotFoundError:
            pass
        self.index.summary = self

    def 载入配置(self):
        for filename, attr in (("价目.json", "prices"), ("计费口径.json", "billing"), ("报警阈值.json", "thresholds")):
            path = self.config / filename
            signature = path.stat().st_mtime_ns
            if self.config_stats.get(filename) != signature:
                with path.open(encoding="utf-8") as source:
                    value = json.load(source)
                setattr(self, attr, value)
                self.config_stats[filename] = signature

    def 计费(self, model):
        for prefix in sorted(self.billing, key=len, reverse=True):
            if not prefix.startswith("_") and model.startswith(prefix):
                return self.billing[prefix]
        return "参考"

    def 模型用量(self, state):
        temp = dict(models=copy.deepcopy(state["models"]), previous=state["previous"], gaps=[])
        if state["pending"]:
            入账(temp, state["pending"])
        for model, bucket in temp["models"].items():
            price = 查价(self.prices, model)
            bucket["usd_ref"] = None if price is None else (
                bucket["input"] * price[0] + bucket["cache_read"] * price[1]
                + bucket["cache_write_5m"] * price[2] + bucket["cache_write_1h"] * price[0] * 2
                + bucket["output"] * price[3]) / 1_000_000
            bucket["billing"] = self.计费(model)
            bucket["usd_paid"] = None if price is None else (bucket["usd_ref"] if bucket["billing"] == "按量美元" else 0.0)
        return temp["models"], state["gaps"] + temp["gaps"]

    def 缺省标题(self, record):
        state = record["state"]
        directory = (state.get("cwd") or record["project"]).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return f"{directory} · {时刻(state.get('start')) or '开始时刻未知'}"

    def 代理(self, record, now):
        state, meta = record["state"], record["meta"]
        aid = record["agent"]
        models, raw_gaps = self.模型用量(state)
        depth = 0 if aid == "main" else int(meta.get("spawnDepth") or 1)
        totals = 合计桶()
        for bucket in models.values():
            累加(totals, bucket)
        start, end = state["start"], state["end"]
        threshold = self.thresholds["主窗失效秒" if aid == "main" else "子代理失效秒"]
        gaps = [dict(at=时刻(at), seconds=round(seconds, 3), cache_write=cw) for at, seconds, cw in raw_gaps if seconds > threshold]
        long_calls = [dict(at=时刻(at), seconds=round(seconds, 3), tool=tool, cmd=cmd) for at, seconds, tool, cmd in state["long_calls"] if seconds >= self.thresholds["长调用秒"]]
        agent = dict(id=aid, parent=None if aid == "main" else (meta.get("parentAgentId") or "main"), depth=depth,
                     type="主窗" if aid == "main" else (meta.get("agentType") or "未知"),
                     title=((state["title"] or state.get("ai_title") or state.get("first_title") or self.缺省标题(record)) if aid == "main" else
                            (meta.get("description") or self.缺省标题(record))),
                     models=sorted(models), start=时刻(start), end=时刻(end), minutes=round(max(0, (end - start) / 60), 2) if start is not None and end is not None else 0,
                     **{k: v for k, v in 完成(totals).items() if k != "agents"},
                     **{k: state[k] for k in ("tool_calls", "builds", "tests", "launches", "kill_by_name", "api_errors")},
                     peak_ctx=max((b["peak_ctx"] for b in models.values()), default=0), running=now - record["mtime"] <= self.thresholds["在跑秒"], gaps=gaps, long_calls=long_calls)
        recent = state["pending"]
        agent["last_ctx"] = sum(recent[k] for k in ("input", "cache_read", "cache_write")) if recent else 0
        agent["ctx_level"] = ("severe" if agent["last_ctx"] > self.thresholds["上下文上限"] else
                              "warn" if agent["last_ctx"] > self.thresholds["上下文警告"] else "ok")
        alerts = []
        def alarm(kind, at, detail):
            alerts.append(dict(kind=kind, agent=aid, at=at, detail=detail))
        for gap in gaps:
            alarm("失效", gap["at"], f"相邻模型回合间隔 {gap['seconds']} 秒，超过 {threshold} 秒")
        for call in long_calls:
            alarm("长调用", call["at"], f"{call['tool']} 前台调用 {call['seconds']} 秒；{call['cmd']}")
        for at, cmd in state["kills"]:
            alarm("按名杀", 时刻(at), cmd)
        title_model = re.search(r"《([^》]+)》", agent["title"])
        if title_model and models:
            label = title_model.group(1).strip().lower()
            expected = self.thresholds["模型别名"].get(label, label if label.startswith(("gpt-", "claude-", "glm-", "deepseek-")) else None)
            if expected and any(not (m == expected or (expected in ("claude-opus", "claude-sonnet", "claude-haiku") and m.startswith(expected + "-"))) for m in models):
                alarm("标题不符", agent["start"], f"标题标注 {label}，实际模型 {'、'.join(models)}")
        if agent["peak_ctx"] > self.thresholds["上下文上限"]:
            alarm("上下文过线", agent["end"], f"峰值上下文 {agent['peak_ctx']}，上限 {self.thresholds['上下文上限']}")
        if agent["type"] == "general-purpose":
            alarm("非专用类型", agent["start"], "代理类型为 general-purpose")
        for at in state["errors"]:
            alarm("接口报错", 时刻(at), "收到接口报错回合，不计 token 与费用")
        gpt_turns = sum(b["turns"] for m, b in models.items() if m.startswith("gpt-"))
        gpt_input = sum(b["input"] for m, b in models.items() if m.startswith("gpt-"))
        gpt_read = sum(b["cache_read"] for m, b in models.items() if m.startswith("gpt-"))
        if gpt_turns >= self.thresholds["低命中最少回合"] and gpt_read / max(1, gpt_input + gpt_read) < self.thresholds["低命中比例"]:
            alarm("低命中", agent["end"], f"GPT 回合 {gpt_turns}，缓存命中率 {gpt_read / max(1, gpt_input + gpt_read):.1%}")
        return agent, models, totals, alerts

    def 构建(self, sid, files, now):
        """仅处理一个会话；调用方发布摘要后释放明细与原始状态。"""
        main_file = next((f for f in files if f["agent"] == "main"), None)
        main_state = main_file["state"] if main_file else {}
        files.sort(key=lambda f: (f["agent"] != "main", f["state"]["start"] or 0, f["agent"]))
        agents, alerts, model_rows, raw_totals = [], [], {}, {}
        by_model = {}
        for record in files:
            agent, models, totals, warnings = self.代理(record, now)
            agents.append(agent)
            alerts.extend(warnings)
            raw_totals[agent["id"]] = totals
            model_rows[agent["id"]] = models
            for model, bucket in models.items():
                target = by_model.setdefault(model, dict(model=model, billing=bucket["billing"], **合计桶(), _hourly={}))
                target["agents"] += 1
                累加(target, bucket)
                for hour, tokens in bucket["hourly"].items():
                    target["_hourly"][hour] = target["_hourly"].get(hour, 0) + tokens
        # 缺少主窗文件时仍保留层零节点，不读取范围之外的旧文件。
        if not main_file:
            empty = dict(id="main", parent=None, depth=0, type="主窗", title=self.缺省标题(files[0]), models=[], start="", end="", minutes=0,
                         **{k: v for k, v in 完成(合计桶()).items() if k != "agents"}, tool_calls=0, builds=0, tests=0, launches=0, kill_by_name=0, api_errors=0, peak_ctx=0, last_ctx=0, ctx_level="ok", running=False, gaps=[], long_calls=[])
            agents.insert(0, empty)
            raw_totals["main"] = 合计桶()
        lookup = {a["id"]: a for a in agents}
        def ticket(agent):
            visited = set()
            while agent and agent["id"] not in visited:
                visited.add(agent["id"])
                match = re.search(r"T-?(\d{4,6})(?!\d)", agent["title"])
                if match:
                    return "T-" + match.group(1).zfill(6)
                agent = lookup.get(agent["parent"])
            return "直派"
        grouped_rows = {}
        for field in ("depth", "type", "ticket"):
            buckets = {}
            for agent in agents:
                key = ticket(agent) if field == "ticket" else agent[field]
                bucket = buckets.setdefault(key, {field: key, **合计桶()})
                bucket["agents"] += 1
                累加(bucket, raw_totals[agent["id"]])
            grouped_rows["by_" + field] = [完成(b) for b in buckets.values()]
        starts = [f["state"]["start"] for f in files if f["state"]["start"] is not None]
        ends = [f["state"]["end"] for f in files if f["state"]["end"] is not None]
        start, end = min(starts, default=None), max(ends, default=None)
        total = 合计桶()
        for bucket in by_model.values():
            累加(total, bucket)
        summary = dict(id=sid, title=agents[0]["title"], alias="", note="", platform=main_state.get("platform", "命令行"), version=main_state.get("version", ""),
                       start=时刻(start), end=时刻(end), minutes=round((end - start) / 60, 2) if start is not None and end is not None else 0,
                       running=any(a["running"] for a in agents), main_model="、".join(agents[0]["models"]), agents=len(agents) - 1,
                       peak_ctx=max(a["peak_ctx"] for a in agents), usd_paid=金额舍入(total["usd_paid"]), usd_ref=金额舍入(total["usd_ref"]),
                       tokens_sub=sum(sum(b[k] for k in 字段) for b in by_model.values() if b["billing"] == "订阅"), alerts=len(alerts))
        # 与参考脚本一致，并发曲线只计子孙代理，不把主窗算作代理。
        changes = defaultdict(int)
        for record in files:
            a, b = record["state"]["start"], record["state"]["end"]
            if record["agent"] != "main" and a is not None and b is not None:
                first, last = int(-(-a // 300)), int(-(-b // 300))
                if last > first:
                    changes[first] += 1
                    changes[last] -= 1
        concurrency = []
        if starts and ends:
            running = 0
            for t in range(int(start // 300), int(end // 300) + 1):
                running += changes.get(t, 0)
                concurrency.append(dict(t=时刻(t * 300), n=running))
        detail = dict(summary=summary, agents=agents, by_model=[完成(b) for b in by_model.values()], **grouped_rows, concurrency=concurrency, alerts=alerts)
        record = dict(project=files[0]["project"], cwd=main_state.get("cwd") or next((f["state"]["cwd"] for f in files if f["state"]["cwd"]), ""),
                            last_active=end, models=by_model, total=total)
        record["stats"] = 统计摘要(files)
        record["stats_hourly"] = 小时摘要(record["stats"])
        record["usd_all"], record["usd_sub"] = 0.0, 0.0
        for row in record["stats"]:
            price = 查价(self.prices, row[1])
            if price:
                amount = (row[5]*price[0] + row[7]*price[1] + row[9]*price[2] + row[10]*price[0]*2 + row[6]*price[3])/1000000
                record["usd_all"] += amount
                if self.计费(row[1]) == "订阅":
                    record["usd_sub"] += amount
        summary["usd_all"] = 金额舍入(record["usd_all"])
        summary["usd_sub"] = 金额舍入(record["usd_sub"])
        record["summary"] = summary
        record["mtime"] = max((f["mtime"] for f in files), default=0)
        return detail, record

    def 发布会话(self, sid, record):
        with self.lock:
            self.records[sid] = record
            self.stats_revision += 1
            self.stats_cache.clear()
        # 旧详情按分片代次失效，不需与后台争用详情构建锁。

    def _summary(self, sid):
        record = self.records[sid]
        result = dict(record["summary"])
        result["running"] = time.time() - record["mtime"] <= self.thresholds["在跑秒"]
        result.update({k: self.notes.get("sessions", {}).get(sid, {}).get(k, "") for k in ("alias", "note")})
        result["title"] = result["alias"] or result["title"]
        return result

    def 会话(self, sid):
        # 每个候选的摘要由同一分片状态构建，不拼接发布线程的另一代摘要。
        with self.detail_lock:
            latest = self.detail_cache.pop(sid, None)
            for _ in range(3):
                with self.lock:
                    record = self.records.get(sid)
                    if record is None:
                        return None
                    generation = record["generation"]
                if latest is None or latest[0] != generation:
                    shard = self.index.读取分片(sid)
                    files = shard["files"]
                    detail, _ = self.构建(sid, list(files.values()), time.time())
                    mtimes = {f["agent"]: f["mtime"] for f in files.values()}
                    candidate = (shard["generation"], detail, mtimes)
                    if latest is None or candidate[0] >= latest[0]:
                        latest = candidate
                    del files, shard
                with self.lock:
                    current = self.records.get(sid)
                    if current and generation == latest[0] == current["generation"]:
                        break
                # 发布竞态最多重取三次；之后返回最新取得的内部一致快照。
            self.detail_cache[sid] = latest
            while len(self.detail_cache) > max(1, int(self.thresholds["详情缓存会话上限"])):
                self.detail_cache.popitem(last=False)
            detail, mtimes = latest[1:]
            now = time.time()
            agents = [dict(a, running=now - mtimes.get(a["id"], 0) <= self.thresholds["在跑秒"]) for a in detail["agents"]]
            summary = dict(detail["summary"], running=any(a["running"] for a in agents))
            with self.lock:
                summary.update({k: self.notes.get("sessions", {}).get(sid, {}).get(k, "") for k in ("alias", "note")})
            summary["title"] = summary["alias"] or summary["title"]
            if summary["alias"]:
                agents = [dict(a, title=summary["alias"]) if a["id"] == "main" else a for a in agents]
            return {**detail, "agents": agents, "summary": summary}

    def 统计(self, hours, now, project=None):
        # 请求时间按秒定界；最多8个范围结果，不持有明细或分片。
        end = int(now)
        with self.lock:
            key = (hours, end, project, self.stats_revision, tuple(self.config_stats.items()))
            if key in self.stats_cache:
                self.stats_cache.move_to_end(key)
                return self.stats_cache[key]
            records, notes = dict(self.records), copy.deepcopy(self.notes)
            prices, billing = self.prices, self.billing
        result = 计算统计(records, prices, billing, notes, hours, end, project)
        with self.lock:
            if key[3] == self.stats_revision:
                self.stats_cache[key] = result
                while len(self.stats_cache) > 8:
                    self.stats_cache.popitem(last=False)
        return result

    def 列表(self, kind, hours, now, project=None):
        with self.lock:
            selected = [sid for sid, r in self.records.items() if (hours == 0 or (r["last_active"] or 0) >= now - hours * 3600) and (project is None or r["project"] == project)]
            selected.sort(key=lambda sid: self.records[sid]["last_active"] or 0, reverse=True)
            if kind == "sessions":
                return [self._summary(sid) for sid in selected]
            if kind == "projects":
                groups = {}
                for sid in selected:
                    record, summary = self.records[sid], self._summary(sid)
                    pid = record["project"]
                    row = groups.setdefault(pid, dict(id=pid, default_name=record["cwd"] or pid, name="", note="", cwd=record["cwd"], sessions=0, running=0, last_active=时刻(record["last_active"]), usd_paid=0.0, usd_ref=0.0, usd_all=0.0, usd_sub=0.0, tokens_sub=0, alerts=0))
                    row["usd_all"] += record["usd_all"]
                    row["usd_sub"] += record.get("usd_sub", 0.0)
                    row["sessions"] += 1
                    row["running"] += summary["running"]
                    for key in ("usd_paid", "usd_ref"):
                        row[key] = 金额相加(row[key], record["total"][key])
                    for key in ("tokens_sub", "alerts"):
                        row[key] += summary[key]
                for pid, row in groups.items():
                    note = self.notes.get("projects", {}).get(pid, {})
                    row["name"], row["note"] = note.get("name") or row["default_name"], note.get("note", "")
                    row["usd_paid"], row["usd_ref"] = 金额舍入(row["usd_paid"]), 金额舍入(row["usd_ref"])
                    row["usd_all"] = 金额舍入(row["usd_all"])
                    row["usd_sub"] = 金额舍入(row["usd_sub"])
                return list(groups.values())
            groups = {}
            for sid in selected:
                for model, source in self.records[sid]["models"].items():
                    target = groups.setdefault(model, dict(model=model, billing=source["billing"], **合计桶(), _hourly={}))
                    累加(target, source)
                    for hour, tokens in source["_hourly"].items():
                        target["_hourly"][hour] = target["_hourly"].get(hour, 0) + tokens
            result = []
            for target in groups.values():
                row = 完成(target)
                row["requests"] = row.pop("turns")
                row.pop("agents")
                row["price_missing"] = 查价(self.prices, target["model"]) is None
                # 小时起点必须在所选范围内；当前未结束小时也提供一个点。
                last_hour = int(now // 3600) * 3600
                first_hour = (min((int(h) for h in target["_hourly"]), default=last_hour) if hours == 0 else
                              int(-(-(now - hours * 3600) // 3600)) * 3600)
                row["hourly"] = [dict(t=时刻(hour), tokens=target["_hourly"].get(str(hour), 0)) for hour in range(first_hour, last_hour + 1, 3600)]
                result.append(row)
            return result

    def 备注(self, kind, ident, value):
        keys = ("name", "note") if kind == "projects" else ("alias", "note")
        if not isinstance(value, dict) or any(k not in keys for k in value) or any(not isinstance(v, str) or len(v) > 4096 for v in value.values()):
            raise ValueError("备注只接受约定的文字字段，每项最多 4096 字")
        with self.notes_lock:
            with self.lock:
                exists = ident in self.records if kind == "sessions" else any(r["project"] == ident for r in self.records.values())
                if not exists:
                    raise KeyError(ident)
                notes = copy.deepcopy(self.notes)
            entry = notes.setdefault(kind, {}).setdefault(ident, {})
            entry.update({k: 安全文字(v, 4096) for k, v in value.items()})
            原子写(self.config / "备注.json", notes)
            with self.lock:
                self.notes = notes
                self.stats_revision += 1
                self.stats_cache.clear()
