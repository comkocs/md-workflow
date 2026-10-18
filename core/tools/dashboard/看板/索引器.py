"""按完整行增量读取；缓存不保存对话正文或工具返回正文。"""
import datetime as dt
import json
import os
import re
import shlex
import threading
import time
from pathlib import Path

字段 = ("input", "output", "cache_read", "cache_write")
用量键 = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def 时戳(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=dt.timezone.utc).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def 时刻(value):
    return (dt.datetime.fromtimestamp(value, dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%m-%d %H:%M") if value is not None else ""


def 安全文字(value, limit=200):
    text = str(value or "")[:limit]
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[地址已隐去]", text)
    text = re.sub(r"(?i)(password|passwd|token|secret|api[_-]?key|口令|密码)(\s*[:=]\s*|\s+)([^\s,;]+)", r"\1=[已隐去]", text)
    text = re.sub(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+", "[已隐去]", text)
    return text


def 原子写(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def 新汇总():
    return dict(start=None, end=None, cwd="", title="", ai_title="", first_title="", platform="命令行", version="",
                models={}, pending=None, usage_records={}, previous=None, gaps=[], calls={}, long_calls=[], kills=[],
                errors=[], tool_calls=0, builds=0, tests=0, launches=0, kill_by_name=0, api_errors=0)


def 模型桶():
    return dict(turns=0, input=0, output=0, cache_read=0, cache_write=0,
                cache_write_5m=0, cache_write_1h=0, peak_ctx=0, hourly={})


def 入账(state, rec):
    bucket = state["models"].setdefault(rec["model"], 模型桶())
    bucket["turns"] += 1
    for key in (*字段, "cache_write_5m", "cache_write_1h"):
        bucket[key] += rec[key]
    bucket["peak_ctx"] = max(bucket["peak_ctx"], rec["input"] + rec["cache_read"] + rec["cache_write"])
    if rec["at"] is not None:
        hour = str(int(rec["at"] // 3600 * 3600))
        bucket["hourly"][hour] = bucket["hourly"].get(hour, 0) + sum(rec[k] for k in 字段)
        if state["previous"] is not None and rec["at"] > state["previous"]:
            state["gaps"].append([state["previous"], rec["at"] - state["previous"], rec["cache_write"]])
        state["previous"] = rec["at"]


def _命令段(command):
    """按 shell 分隔符切段；引号内的 && || ; | 与换行不当分隔。"""
    segments, current = [], []
    quote = None
    escaped = False
    pending_break = False
    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char in "&|;":
            if pending_break and char in "&|" and not current:
                continue
            pending_break = True
            segments.append("".join(current))
            current = []
        elif char in "\n\r":
            pending_break = False
            segments.append("".join(current))
            current = []
        else:
            pending_break = False
            current.append(char)
    segments.append("".join(current))
    return segments


def _段首命令(segment):
    """去掉前置环境变量赋值与 cd；返回段首命令词（路径末名、去 .exe）及后续词。"""
    try:
        words = shlex.split(segment, comments=True, posix=True)
    except ValueError:
        return None
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words.pop(0)
    while words and words[0].lower() in ("cd", "pushd", "popd"):
        if words[0].lower() == "popd":
            words = words[1:]
        elif len(words) >= 2:
            words = words[2:]
        else:
            return None
    if not words:
        return None
    name = words[0].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.lower(), words[1:]


def 命令计数(command):
    """按段首命令词计 build / test / 起客户端 / 按名杀；引号里的参数文本不算命令。"""
    builds = tests = launches = kills = 0
    for segment in _命令段(command):
        head = _段首命令(segment)
        if not head:
            continue
        name, args = head
        if name == "dotnet" and args:
            sub = args[0].lower()
            builds += sub == "build"
            tests += sub == "test"
        elif name in ("python", "python3", "py") and any(
                re.search(r"(?:shot-run|launch)\S*\.py$", word, re.I) for word in args if not word.startswith("-")):
            launches += 1
        elif name in ("pkill", "killall"):
            kills += 1
        elif name == "taskkill" and any(word.lower() == "/im" for word in args):
            kills += 1
    return builds, tests, launches, kills


def 按名杀命令(command):
    """只识别引号外分段的段首命令，不把查询参数或正文当作命令。"""
    return 命令计数(command)[3] > 0


# 仅对允许字段反序列化；消息 content 的字节区间只跳过，不解码、不创建正文对象。
_JSON_TOKEN = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"|[{}\[\]]')
_JSON_SPACE = b" \t\r\n"


def _值末尾(raw, pos):
    if raw[pos] == 34:
        match = _JSON_TOKEN.match(raw, pos)
        if match is None:
            raise ValueError("无效字符串")
        return match.end()
    if raw[pos] in (123, 91):
        depth = 0
        for token in _JSON_TOKEN.finditer(raw, pos):
            kind = raw[token.start()]
            if kind in (123, 91):
                depth += 1
            elif kind in (125, 93):
                depth -= 1
                if depth == 0:
                    return token.end()
        raise ValueError("未闭合对象")
    end = pos
    while end < len(raw) and raw[end] not in b",}\r\n\t ":
        end += 1
    return end


def _投影(raw, allowed, message=False, raw_fields=()):
    pos = 0
    while pos < len(raw) and raw[pos] in _JSON_SPACE:
        pos += 1
    if pos >= len(raw) or raw[pos] != 123:
        raise ValueError("非对象")
    pos += 1
    result = {}
    while pos < len(raw):
        while pos < len(raw) and raw[pos] in _JSON_SPACE:
            pos += 1
        if pos < len(raw) and raw[pos] == 125:
            return result
        end = _值末尾(raw, pos)
        key = json.loads(raw[pos:end])
        pos = end
        while pos < len(raw) and raw[pos] in _JSON_SPACE:
            pos += 1
        if pos >= len(raw) or raw[pos] != 58:
            raise ValueError("缺少冒号")
        pos += 1
        while pos < len(raw) and raw[pos] in _JSON_SPACE:
            pos += 1
        end = _值末尾(raw, pos)
        if key in allowed:
            if key in raw_fields:
                result[key] = raw[pos:end]
            elif message and key == "message":
                result[key] = _投影(raw[pos:end], {"id", "model", "usage", "requestId"}) if raw[pos] == 123 else {}
            else:
                result[key] = json.loads(raw[pos:end])
        pos = end
        while pos < len(raw) and raw[pos] in _JSON_SPACE:
            pos += 1
        if pos < len(raw) and raw[pos] == 125:
            return result
        if pos >= len(raw) or raw[pos] != 44:
            raise ValueError("缺少逗号")
        pos += 1
    raise ValueError("未闭合对象")


def _文字前缀(raw, limit):
    """只解码允许的前缀；其余字符串不反序列化。"""
    if not raw or raw[0] != 34:
        return ""
    pos, count = 1, 0
    while pos < len(raw) and raw[pos] != 34 and count < limit:
        char = raw[pos]
        if char == 92:
            if raw[pos+1] == 117:
                code = int(raw[pos+2:pos+6], 16)
                pos += 6
                if 0xD800 <= code <= 0xDBFF and raw[pos:pos+2] == b"\\u" and 0xDC00 <= int(raw[pos+2:pos+6], 16) <= 0xDFFF:
                    pos += 6
            else:
                pos += 2
        else:
            pos += 1 if char < 128 else (2 if char < 224 else (3 if char < 240 else 4))
        count += 1
    return json.loads(raw[:pos] + b'"')


def _内容项(raw):
    if not raw:
        return
    if raw[0] == 34:
        yield {"type": "text", "text": raw}
    elif raw[0] == 91:
        pos = 1
        while pos < len(raw):
            while pos < len(raw) and raw[pos] in _JSON_SPACE + b",":
                pos += 1
            if pos >= len(raw) or raw[pos] == 93:
                break
            end = _值末尾(raw, pos)
            if raw[pos] == 123:
                yield _投影(raw[pos:end], {"type", "id", "name", "input", "text", "tool_use_id"}, raw_fields={"input", "text"})
            pos = end


def 元数据行(raw, need_title=True):
    row = _投影(raw, {"type", "timestamp", "cwd", "entrypoint", "version", "customTitle", "aiTitle",
                       "message", "requestId", "uuid", "isMeta"}, raw_fields={"message"})
    message = row.pop("message", b"{}")
    if not message or message[0] != 123:
        return row
    msg = _投影(message, {"id", "model", "usage", "requestId", "content"}, raw_fields={"content"})
    content = msg.pop("content", b"[]")
    permitted = []
    for item in _内容项(content):
        kind = item.get("type")
        if row.get("type") == "assistant" and kind == "tool_use":
            tool = {k: item.get(k) for k in ("type", "id", "name")}
            inp = item.get("input", b"{}")
            values = _投影(inp, {"command", "run_in_background", "background", "is_background"}, raw_fields={"command"}) if inp[:1] == b"{" else {}
            background = any(values.get(k) for k in ("run_in_background", "background", "is_background"))
            if tool["name"] == "Bash":
                command = values.get("command", b"")
                tool["input"] = {"command": _文字前缀(command, 200) if command[:1] == b'"' else "",
                                 "run_in_background": bool(background)}
            elif background:
                tool["input"] = {"run_in_background": True}
            permitted.append(tool)
        elif row.get("type") == "user" and kind == "tool_result":
            permitted.append({"type": kind, "tool_use_id": item.get("tool_use_id")})
        elif row.get("type") == "user" and need_title and not row.get("isMeta") and kind == "text":
            text = _文字前缀(item.get("text", b'""'), 80).strip()
            if text and not text.startswith("<"):
                row["opening_title"] = text
                need_title = False
    msg["content"] = permitted
    row["message"] = msg
    return row


def 处理行(state, row):
    if not isinstance(row, dict):
        return
    stamp = 时戳(row.get("timestamp"))
    if stamp is not None:
        state["start"] = stamp if state["start"] is None else min(state["start"], stamp)
        state["end"] = stamp if state["end"] is None else max(state["end"], stamp)
    if not state["cwd"] and row.get("cwd"):
        state["cwd"] = 安全文字(row["cwd"], 1024)
    if row.get("entrypoint"):
        state["platform"] = {"claude-desktop": "桌面版", "claude-vscode": "VS Code"}.get(row["entrypoint"], "命令行")
    if row.get("version"):
        state["version"] = str(row["version"])
    if row.get("type") == "custom-title":
        state["title"] = 安全文字(row.get("customTitle"), 512)
    if row.get("type") == "ai-title":
        state["ai_title"] = 安全文字(row.get("aiTitle"), 512)
    msg = row.get("message") or {}
    if not isinstance(msg, dict):
        return
    if not state["first_title"] and row.get("opening_title"):
        state["first_title"] = 安全文字(row["opening_title"], 80)
    content = msg.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "tool_use" and item.get("id") not in state["calls"]:
                ident = item.get("id")
                if not ident:
                    continue
                inp = item.get("input") or {}
                if not isinstance(inp, dict):
                    inp = {}
                name = item.get("name", "")
                cmd = str(inp.get("command") or "")[:200] if name == "Bash" else ""
                state["calls"][ident] = [stamp, name, 安全文字(cmd), bool(inp.get("run_in_background"))]
                state["tool_calls"] += 1
                if name == "Bash":
                    builds, tests, launches, kills = 命令计数(cmd)
                    state["builds"] += builds
                    state["tests"] += tests
                    state["launches"] += launches
                    if kills:
                        state["kill_by_name"] += kills
                        state["kills"].append([stamp, 安全文字(cmd)])
            elif kind == "tool_result":
                ident = item.get("tool_use_id")
                call = state["calls"].get(ident)
                if call and stamp is not None and call[0] is not None:
                    seconds = stamp - call[0]
                    if not call[3]:
                        state["long_calls"].append([call[0], max(0, seconds), call[1], call[2]])
                    state["calls"][ident] = None
    if row.get("type") != "assistant":
        return
    model = msg.get("model")
    if model == "<synthetic>":
        state["api_errors"] += 1
        state["errors"].append(stamp)
        return
    usage = msg.get("usage")
    if not model or not isinstance(usage, dict) or not usage:
        return
    ident = msg.get("id") or row.get("requestId") or msg.get("requestId") or row.get("uuid")
    rec = dict(id=ident, model=model, at=stamp)
    rec.update({k: int(usage.get(v, 0) or 0) for k, v in zip(字段, 用量键)})
    creation = usage.get("cache_creation")
    if isinstance(creation, dict) and any(k in creation for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")):
        rec["cache_write_5m"] = int(creation.get("ephemeral_5m_input_tokens", 0) or 0)
        rec["cache_write_1h"] = int(creation.get("ephemeral_1h_input_tokens", 0) or 0)
    else:
        rec["cache_write_5m"], rec["cache_write_1h"] = rec["cache_write"], 0
    # 统计使用文件内全局回合去重，覆盖同ID非连续内容行；仅保存元数据与用量。
    rec["platform"] = state["platform"]
    state["usage_records"][str(ident)] = rec
    pending = state["pending"]
    if pending and pending["id"] != ident:
        入账(state, pending)
    state["pending"] = rec


class 分片重建中(OSError):
    """分片不可用，后台已排队重建。"""


class 索引器:
    def __init__(self, root, base, hours=24, cache_dir=None):
        self.root, self.base = Path(root), Path(base)
        self.cache = (Path(cache_dir) if cache_dir else self.base / "数据缓存") / "索引_v7"
        self.hours = hours
        # 常驻只保留文件指纹、偏移和会话轻摘要，绝不保留全历史解析状态。
        self.files = {}
        self.bytes_total = 0
        self.indexing = dict(active=True, done_files=0, total_files=0, bytes_done=0, bytes_total=0)
        self.status = dict(version="1.1", scanned_at="", scan_ms=0, bytes_read_last=0,
                           bytes_read_total=0, files_tracked=0, range_hours=hours, indexing=dict(self.indexing))
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.cache_dirty = False
        self.last_cache_write = 0.0
        self.last_full_discovery = None
        self.scanned_projects = set()
        self.scanned_branches = set()
        self.summary = None
        self.thread = None
        self.config_signature = {}
        self.invalid_sessions = set()
        self.summary_encoded = {}
        self.phase_seconds = dict(发现=0.0, 读取解析=0.0, 汇总=0.0, 分片写入=0.0, 摘要写入=0.0)

    def 分片路径(self, sid):
        # 会话标识只作为哈希输入，不允许接口参数参与路径拼接。
        import hashlib
        return self.cache / (hashlib.sha256(sid.encode("utf-8")).hexdigest() + ".json")

    def 标记失效(self, sid):
        with self.lock:
            self.invalid_sessions.add(sid)
            self.last_full_discovery = None
        self.wake.set()

    def 读取分片(self, sid):
        import hashlib
        try:
            envelope = json.loads(self.分片路径(sid).read_bytes())
            payload = envelope["payload"].encode("utf-8")
            if (envelope["version"] != 7 or len(payload) != envelope["length"] or
                    hashlib.sha256(payload).hexdigest() != envelope["sha256"]):
                raise ValueError("分片校验不符")
            value = json.loads(payload)
            if value["session"] != sid or not isinstance(value["generation"], int) or not isinstance(value["files"], dict) or not value["files"]:
                raise ValueError("分片结构不符")
            return value
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.标记失效(sid)
            raise 分片重建中("会话分片重建中，请稍后重试") from exc

    def 读取会话(self, sid):
        return self.读取分片(sid)["files"]

    def 写入分片(self, sid, generation, states):
        import hashlib
        payload = json.dumps(dict(session=sid, generation=generation, files=states), ensure_ascii=False, separators=(",", ":"))
        raw = payload.encode("utf-8")
        原子写(self.分片路径(sid), dict(version=7, length=len(raw), sha256=hashlib.sha256(raw).hexdigest(), payload=payload))
        stat = self.分片路径(sid).stat()
        return [stat.st_size, stat.st_mtime_ns]

    def 迁移旧缓存(self):
        """逐会话回读白名单字段；旧目录只读，恢复首句与工具计数。"""
        import hashlib
        began = time.perf_counter()
        candidates = [self.cache.parent / "索引_v6"]
        if self.cache.parent.name != "数据缓存":
            candidates.append(self.cache.parent.parent / "索引_v6")
        old_cache = next((path for path in candidates if (path / "摘要.json").exists()), candidates[-1])
        try:
            saved = json.loads((old_cache / "摘要.json").read_bytes())
            if saved.get("version") != 6 or saved.get("root") != str(self.root) or not self.summary:
                return
        except (OSError, ValueError, TypeError):
            return
        self.bytes_total = saved.get("bytes_read_total", 0)
        self.config_signature = dict(self.summary.config_stats)
        sessions = list(saved.get("records", {}))
        del saved
        migrated = 0
        for sid in sessions:
            if self.stop.is_set():
                return
            try:
                envelope = json.loads((old_cache / self.分片路径(sid).name).read_bytes())
                payload = envelope["payload"].encode("utf-8")
                if (envelope["version"] != 6 or len(payload) != envelope["length"] or
                        hashlib.sha256(payload).hexdigest() != envelope["sha256"]):
                    continue
                shard = json.loads(payload)
                if shard["session"] != sid:
                    continue
                states = shard["files"]
                del envelope, payload, shard
                for path, file in states.items():
                    file["state"] = 新汇总()
                    file["parser_version"] = 8
                    # 回读到已记偏移，只投影白名单；用量仍按原去重规则重建。
                    with open(path, "rb") as source:
                        while source.tell() < file["offset"]:
                            line = source.readline()
                            if not line or not line.endswith(b"\n"):
                                break
                            try:
                                处理行(file["state"], 元数据行(line, not file["state"]["first_title"]))
                            except (ValueError, TypeError, IndexError, AttributeError, OverflowError):
                                continue
                detail, record = self.summary.构建(sid, list(states.values()), time.time())
                del detail
                generation = time.time_ns()
                record["generation"] = generation
                record["shard_stat"] = self.写入分片(sid, generation, states)
                self.summary.发布会话(sid, record)
                for path, file in states.items():
                    self.files[path] = {k: file[k] for k in ("project", "session", "agent", "mtime", "fingerprint", "meta_stat", "offset")}
                migrated += 1
                del states
            except (OSError, ValueError, KeyError, TypeError):
                # 不迁移不可校验会话；正常扫描只重建缺失会话。
                continue
        self.保存缓存()
        原子写(self.cache / "迁移计量.json", dict(来源版本=6,目标版本=7,会话数=migrated,秒=time.perf_counter()-began))
        return True

    def 载入缓存(self):
        if not (self.cache / "摘要.json").exists() and self.迁移旧缓存():
            return
        try:
            with (self.cache / "摘要.json").open(encoding="utf-8") as source:
                saved = json.load(source)
            if saved.get("version") == 7 and saved.get("root") == str(self.root):
                self.files = saved["files"]
                self.bytes_total = saved.get("bytes_read_total", 0)
                self.config_signature = saved.get("config_signature", {})
                if self.summary:
                    with self.summary.lock:
                        self.summary.records = saved["records"]
                        from .统计 import 小时摘要
                        for record in self.summary.records.values():
                            if "stats_hourly" not in record:
                                record["stats_hourly"] = 小时摘要(record.get("stats", []))
                                self.cache_dirty = True
                        self.summary_encoded = {sid: (rec["generation"], json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) for sid, rec in self.summary.records.items()}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def 发现(self):
        discovery_started = time.monotonic()
        self.full_discovery = self.last_full_discovery is None or discovery_started - self.last_full_discovery >= 60
        cutoff = time.time() - 120
        active_branches = {(r["project"], r["session"]) for r in self.files.values() if r["mtime"] >= cutoff}
        self.scanned_projects = set()
        self.scanned_branches = set()
        try:
            projects = list(os.scandir(self.root))
        except OSError:
            return
        for project in projects:
            if not project.is_dir(follow_symlinks=False):
                continue
            try:
                entries = list(os.scandir(project.path))
            except OSError:
                continue
            self.scanned_projects.add(project.name)
            for item in entries:
                try:
                    if item.is_file(follow_symlinks=False) and item.name.endswith(".jsonl"):
                        stat = item.stat(follow_symlinks=False)
                        if self.full_discovery or stat.st_mtime >= cutoff:
                            yield item, project.name, item.name[:-6], "main", stat
                    elif item.is_dir(follow_symlinks=False):
                        branch = (project.name, item.name)
                        if not self.full_discovery and branch not in active_branches and item.stat(follow_symlinks=False).st_mtime < cutoff:
                            continue
                        try:
                            with os.scandir(Path(item.path) / "subagents") as source:
                                agents = [a for a in source if a.is_file(follow_symlinks=False) and a.name.startswith("agent-") and a.name.endswith(".jsonl")]
                            self.scanned_branches.add(branch)
                            for agent in agents:
                                yield agent, project.name, item.name, agent.name[6:-6], agent.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            self.scanned_branches.add(branch)
                except OSError:
                    continue
        if self.full_discovery:
            self.last_full_discovery = discovery_started

    def 发布(self, began, now, read_bytes):
        with self.lock:
            self.status = dict(version="1.1", scanned_at=时刻(now),
                               scan_ms=round((time.perf_counter() - began) * 1000, 2),
                               bytes_read_last=read_bytes, bytes_read_total=self.bytes_total + read_bytes,
                               files_tracked=len(self.files), range_hours=self.hours, indexing=dict(self.indexing))

    def 文件签名(self, item):
        entry, project, sid, aid, stat = item
        meta_stat = None
        if aid != "main":
            try:
                ms = os.stat(Path(entry.path).with_suffix(".meta.json"), follow_symlinks=False)
                meta_stat = [ms.st_size, ms.st_mtime_ns, ms.st_ino]
            except OSError:
                pass
        return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns], meta_stat

    def 读文件(self, item, old, signature, began, now, read_bytes):
        entry, project, sid, aid, discovered_stat = item
        stat = os.stat(entry.path, follow_symlinks=False)
        identity = [stat.st_dev, stat.st_ino, stat.st_ctime_ns]
        if old is None or old.get("parser_version") != 8 or old["identity"] != identity or stat.st_size < old["offset"] or stat.st_size < old["size"]:
            old = dict(project=project, session=sid, agent=aid, identity=identity, parser_version=8,
                       offset=0, size=0, mtime=0, meta={}, meta_stat=None, state=新汇总())
        if stat.st_size != old["size"]:
            with open(entry.path, "rb") as source:
                source.seek(old["offset"])
                progress_at = source.tell()
                while not self.stop.is_set():
                    line = source.readline()
                    if not line:
                        break
                    read_bytes += len(line)
                    if not line.endswith(b"\n"):
                        break
                    old["offset"] += len(line)
                    try:
                        处理行(old["state"], 元数据行(line, not old["state"]["first_title"]))
                    except (ValueError, TypeError, AttributeError, OverflowError, IndexError):
                        continue
                    if self.indexing["active"] and source.tell() - progress_at >= 4 * 1024 * 1024:
                        progress_at = source.tell()
                        completed = self.indexing["bytes_done"]
                        self.indexing["bytes_done"] = completed + min(old["offset"], discovered_stat.st_size)
                        self.发布(began, now, read_bytes)
                        self.indexing["bytes_done"] = completed
        old["size"], old["mtime"] = stat.st_size, stat.st_mtime
        old["fingerprint"] = signature[0]
        if aid != "main" and signature[1] != old["meta_stat"]:
            try:
                with Path(entry.path).with_suffix(".meta.json").open("rb") as source:
                    raw = source.read()
                read_bytes += len(raw)
                meta = json.loads(raw)
                old["meta"] = {k: meta.get(k) for k in ("agentType", "description", "toolUseId", "parentAgentId", "spawnDepth")}
                old["meta"]["description"] = 安全文字(meta.get("description"), 512)
                old["meta_stat"] = signature[1]
            except (OSError, ValueError, TypeError):
                pass
        return old, read_bytes

    def 扫描(self):
        from collections import defaultdict
        with self.scan_lock:
            began, now = time.perf_counter(), time.time()
            initial = self.indexing["active"]
            if self.summary:
                self.summary.载入配置()
                config_changed = self.config_signature != self.summary.config_stats
            else:
                config_changed = False
            with self.lock:
                invalid = set(self.invalid_sessions)
            if config_changed or invalid:
                self.last_full_discovery = None
            phase = time.perf_counter()
            entries = list(self.发现())
            self.phase_seconds["发现"] += time.perf_counter() - phase
            shard_names = {p.name for p in self.cache.glob("*.json")} if self.full_discovery else None
            grouped = defaultdict(list)
            for item in entries:
                grouped[item[2]].append(item)
            # 同一会话的所有历史一次处理完并释放；会话按最近修改优先。
            groups = sorted(grouped.items(), key=lambda pair: max(x[4].st_mtime for x in pair[1]), reverse=True)
            if initial:
                self.indexing.update(done_files=0, total_files=len(entries), bytes_done=0,
                                     bytes_total=sum(x[4].st_size for x in entries))
                self.发布(began, now, 0)
            read_bytes = 0
            known_sessions = defaultdict(set)
            for path, record in self.files.items():
                known_sessions[record["session"]].add(path)
            for sid, items in groups:
                if self.stop.is_set():
                    break
                signatures = {x[0].path: self.文件签名(x) for x in items}
                changed = config_changed or any(x[0].path not in self.files or
                    signatures[x[0].path] != (self.files[x[0].path]["fingerprint"], self.files[x[0].path]["meta_stat"]) for x in items)
                known = known_sessions.get(sid, set())
                seen = set(signatures)
                stale = {p for p in known - seen if
                         (self.full_discovery and self.files[p]["project"] in self.scanned_projects if self.files[p]["agent"] == "main" else
                          (self.files[p]["project"], sid) in self.scanned_branches)}
                changed = changed or bool(stale) or sid in invalid or (shard_names is not None and self.分片路径(sid).name not in shard_names)
                record = self.summary.records.get(sid) if self.summary else None
                if self.summary and (record is None or "usd_sub" not in record):
                    # 兼容复用旧v4摘要，补齐分账并重新应用统一价目匹配。
                    changed = True
                # 启动和全枚举检查分片属性；外部截断/损坏不依赖源日志变化来触发恢复。
                if self.full_discovery and record:
                    try:
                        stat = self.分片路径(sid).stat()
                        if record.get("shard_stat") != [stat.st_size, stat.st_mtime_ns]:
                            changed = True
                    except OSError:
                        changed = True
                states = None
                if changed:
                    try:
                        states = {} if sid in invalid or not known else self.读取会话(sid)
                    except 分片重建中:
                        states = {}
                    # 缓存损坏时补枚举该会话所有源文件，不能只拿轻扫入选部分重建。
                    if not states and not initial:
                        source_project = Path(items[0][0].path).parent if items[0][3] == "main" else Path(items[0][0].path).parents[2]
                        recovered = []
                        for source_dir, is_main in ((source_project, True), (source_project / sid / "subagents", False)):
                            try:
                                for entry in os.scandir(source_dir):
                                    if entry.is_file(follow_symlinks=False) and (entry.name == sid + ".jsonl" if is_main else entry.name.startswith("agent-") and entry.name.endswith(".jsonl")):
                                        recovered.append((entry, items[0][1], sid, "main" if is_main else entry.name[6:-6], entry.stat(follow_symlinks=False)))
                            except OSError:
                                pass
                        if not initial:
                            items = recovered
                            signatures = {x[0].path: self.文件签名(x) for x in items}
                    for p in stale:
                        states.pop(p, None)
                success = True
                for item in items:
                    path = item[0].path
                    if states is not None:
                        try:
                            phase = time.perf_counter()
                            states[path], read_bytes = self.读文件(item, states.get(path), signatures[path], began, now, read_bytes)
                            self.phase_seconds["读取解析"] += time.perf_counter() - phase
                        except OSError:
                            success = False
                            break
                    if initial:
                        self.indexing["done_files"] += 1
                        self.indexing["bytes_done"] += item[4].st_size
                    self.发布(began, now, read_bytes)
                if states is not None and success and not self.stop.is_set():
                    record = None
                    if self.summary:
                        phase = time.perf_counter()
                        detail, record = self.summary.构建(sid, list(states.values()), now)
                        self.phase_seconds["汇总"] += time.perf_counter() - phase
                        # 摘要保留金额、模型小时桶和列表字段；代理树、工具细节等不常驻。
                        del detail
                        record["generation"] = time.time_ns()
                    generation = record["generation"] if record else time.time_ns()
                    phase = time.perf_counter()
                    shard_stat = self.写入分片(sid, generation, states)
                    self.phase_seconds["分片写入"] += time.perf_counter() - phase
                    if record:
                        record["shard_stat"] = shard_stat
                    for p in known:
                        self.files.pop(p, None)
                    for p, f in states.items():
                        self.files[p] = {k: f[k] for k in ("project", "session", "agent", "mtime", "fingerprint", "meta_stat", "offset")}
                    if self.summary:
                        self.summary.发布会话(sid, record)
                    with self.lock:
                        self.invalid_sessions.discard(sid)
                    self.cache_dirty = True
                del states
                if not success:
                    self.last_full_discovery = None
                self.发布(began, now, read_bytes)
                if self.cache_dirty and time.monotonic() - self.last_cache_write >= 60:
                    self.保存缓存()
            if self.full_discovery and not self.stop.is_set():
                # 完整枚举确认已消失的会话只撤销内存/摘要引用；旧分片原地保留，不删除文件。
                for sid in known_sessions.keys() - grouped.keys():
                    paths = known_sessions[sid]
                    if all(self.files[p]["project"] in self.scanned_projects for p in paths):
                        for p in paths:
                            self.files.pop(p, None)
                        if self.summary:
                            with self.summary.lock:
                                self.summary.records.pop(sid, None)
                                self.summary.stats_revision += 1
                                self.summary.stats_cache.clear()
                        self.cache_dirty = True
            self.bytes_total += read_bytes
            if self.summary:
                self.config_signature = dict(self.summary.config_stats)
            complete = initial and not self.stop.is_set() and self.indexing["done_files"] == self.indexing["total_files"]
            if self.cache_dirty and (initial or time.monotonic() - self.last_cache_write >= 60):
                self.保存缓存()
            if complete:
                self.indexing["active"] = False
                原子写(self.cache / "阶段计量.json", self.phase_seconds)
            elif initial:
                self.last_full_discovery = None
            self.发布(began, now, 0)
            with self.lock:
                self.status["bytes_read_last"] = read_bytes

    def 保存缓存(self):
        phase = time.perf_counter()
        records = dict(self.summary.records) if self.summary else {}
        # 只序列化变代会话；旧实现每分钟重编码约33MB摘要，空闲CPU被整包写盘占用。
        for sid in self.summary_encoded.keys() - records.keys():
            self.summary_encoded.pop(sid)
        for sid, record in records.items():
            cached = self.summary_encoded.get(sid)
            if cached is None or cached[0] != record["generation"]:
                raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.summary_encoded[sid] = (record["generation"], raw)
        header = json.dumps(dict(version=7, root=str(self.root), bytes_read_total=self.bytes_total,
                                 files=self.files, config_signature=self.config_signature),
                            ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path = self.cache / "摘要.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with temp.open("wb") as out:
            out.write(header[:-1] + b',"records":{')
            for n, (sid, (_, raw)) in enumerate(self.summary_encoded.items()):
                if n:
                    out.write(b",")
                out.write(json.dumps(sid).encode("utf-8") + b":" + raw)
            out.write(b"}}")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        self.cache_dirty = False
        self.last_cache_write = time.monotonic()
        self.phase_seconds["摘要写入"] += time.perf_counter() - phase

    def 开始(self):
        self.thread = threading.Thread(target=self._循环, name="会话索引", daemon=True)
        self.thread.start()

    def _循环(self):
        self.载入缓存()
        while not self.stop.is_set():
            try:
                self.扫描()
            except (OSError, ValueError):
                pass
            self.wake.wait(10)
            self.wake.clear()

    def 确保范围(self, hours):
        with self.lock:
            self.hours = hours
            self.status = {**self.status, "range_hours": hours}

    def 关闭(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join()
        with self.scan_lock:
            if self.cache_dirty:
                self.保存缓存()
