"""把看板挂到本机工单台，不另起监听端口。"""
import hashlib
import ipaddress
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .看板.接口 import 请求处理
from .看板.汇总 import 看板汇总
from .看板.索引器 import 索引器


class 看板挂载:
    def __init__(self, server):
        base = Path(__file__).resolve().parent
        identity = hashlib.sha256(str(server.service.store.root.resolve()).encode("utf-8")).hexdigest()[:16]
        cache = base / "数据缓存" / ("工单台_" + identity)
        self.index = 索引器(Path.home() / ".claude/projects", base, cache_dir=cache)
        self.summary = 看板汇总(self.index)
        self.static = (base / "static").resolve()
        self.server_port = server.server_port
        self.index.开始()

    def 关闭(self):
        self.index.关闭()


class 挂载请求:
    _reply = 请求处理._reply
    _json = 请求处理._json
    _local = 请求处理._local

    def __init__(self, handler, dashboard, path):
        self.handler = handler
        self.server = dashboard
        self.path = path
        self.began = time.perf_counter()
        self._request_body_read = False

    def __getattr__(self, name):
        return getattr(self.handler, name)


def 处理挂载(handler):
    server = handler.server
    try:
        local = (ipaddress.ip_address(server.server_address[0]).is_loopback
                 and ipaddress.ip_address(handler.client_address[0]).is_loopback)
    except ValueError:
        local = False
    if not local or server.auth is not None or server.ssl_context is not None:
        handler._error(403, "运行看板仅向本机明文监听开放，远程监听、账号认证或 TLS 模式不可访问。")
        return
    request = 挂载请求(handler, server, handler.path)
    if not request._local():
        return
    parsed = urlsplit(handler.path)
    if parsed.path == "/dashboard":
        handler.send_response(302)
        handler.send_header("Location", urlunsplit(("", "", "/dashboard/", parsed.query, "")))
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    with server.dashboard_lock:
        if server.dashboard_closed:
            handler._error(503, "运行看板正在关闭，请稍后重试。")
            return
        if server.dashboard is None:
            server.dashboard = 看板挂载(server)
        dashboard = server.dashboard
    request.server = dashboard
    request.path = urlunsplit(("", "", parsed.path.removeprefix("/dashboard"), parsed.query, ""))
    try:
        if handler.command == "POST":
            请求处理.do_POST(request)
        else:
            请求处理.do_GET(request)
    finally:
        handler._request_body_read = request._request_body_read
