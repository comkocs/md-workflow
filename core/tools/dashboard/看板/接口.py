"""仅本机 HTTP 接口；静态根目录隔离，备注采用同源校验。"""
import json
import mimetypes
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from .索引器 import 分片重建中


class 本机服务(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, index, summary):
        super().__init__(address, 请求处理)
        self.index, self.summary = index, summary
        self.static = (index.base / "static").resolve()


class 请求处理(BaseHTTPRequestHandler):
    server_version = "LocalDashboard/1"
    sys_version = ""

    def log_message(self, format, *args):
        # 不记录路径、客户端地址、对话内容或请求正文。
        pass

    def _reply(self, code, body, content_type="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Server-Timing", f"app;dur={(time.perf_counter() - self.began) * 1000:.3f}")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _json(self, value, code=200):
        self._reply(code, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def _local(self):
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "") not in hosts:
            self._json({"error": "只接受本机访问"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            self._json({"error": "不接受跨站请求"}, 403)
            return False
        return True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.began = time.perf_counter()
        if not self._local():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/status":
                with self.server.index.lock:
                    self._json(self.server.index.status)
                return
            if path.startswith("/api/session/"):
                result = self.server.summary.会话(path[len("/api/session/"):])
                self._json(result if result is not None else {"error": "所选范围未索引此会话"}, 200 if result is not None else 404)
                return
            if path == "/api/stats":
                query = parse_qs(parsed.query)
                try:
                    hours = int(query.get("hours", ["24"])[0])
                    # 可选结束时戳仅用于历史对账；不传仍是当前时刻。
                    end = float(query["to"][0]) if "to" in query else time.time()
                except (TypeError, ValueError):
                    raise ValueError("统计范围必须是整数小时，结束点须为秒时戳")
                if hours < 0 or not 0 <= end <= time.time():
                    raise ValueError("统计范围须非负，结束点不能晚于当前时刻")
                self._json(self.server.summary.统计(hours, end, query.get("project", [None])[0]))
                return
            if path in ("/api/projects", "/api/sessions", "/api/models"):
                query = parse_qs(parsed.query)
                try:
                    hours = int(query.get("hours", ["24"])[0])
                except (TypeError, ValueError):
                    raise ValueError("时间范围必须是整数小时")
                if not 0 <= hours <= 168:
                    raise ValueError("时间范围须为 0（全部）或 1 至 168 小时")
                self.server.index.确保范围(hours)
                project = query.get("project", [None])[0]
                self._json(self.server.summary.列表(path.rsplit("/", 1)[1], hours, time.time(), project))
                return
            if path.startswith("/api/"):
                self._json({"error": "接口不存在"}, 404)
                return
            relative = path.lstrip("/") or "index.html"
            target = (self.server.static / relative).resolve()
            if not target.is_relative_to(self.server.static) or not target.is_file():
                if path == "/" and not (self.server.static / "index.html").is_file():
                    self._reply(200, "运行看板后端已启动，前端页面尚未放入静态目录。".encode("utf-8"), "text/plain; charset=utf-8")
                else:
                    self._json({"error": "文件不存在"}, 404)
                return
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
                content_type += "; charset=utf-8"
            self._reply(200, target.read_bytes(), content_type)
        except 分片重建中:
            self._json({"error": "会话分片重建中，请稍后重试"}, 503)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except OSError:
            self._json({"error": "暂时无法读取本机数据，请稍后重试"}, 503)

    def do_POST(self):
        self.began = time.perf_counter()
        if not self._local():
            return
        parts = unquote(urlsplit(self.path).path).strip("/").split("/")
        if len(parts) != 4 or parts[0] != "api" or parts[1] not in ("projects", "sessions") or parts[3] != "note":
            self._json({"error": "接口不存在"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise ValueError("请求正文长度不合规")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请求正文必须使用 JSON")
            self.connection.settimeout(10)
            raw = self.rfile.read(length)
            self._request_body_read = len(raw) == length
            value = json.loads(raw)
            self.server.summary.备注(parts[1], parts[2], value)
            self._json({"ok": True})
        except (ValueError, UnicodeError):
            self._json({"error": "备注格式不合规：只接受约定文字字段，每项最多 4096 字"}, 400)
        except KeyError:
            self._json({"error": "项目或窗口不存在"}, 404)
        except OSError:
            self._json({"error": "备注未保存，请稍后重试"}, 503)
