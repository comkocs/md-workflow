"""本机工单台看板挂载的页面、接口与远程隔离验收。"""

from functools import partial
import http.client
import json
from pathlib import Path
import ssl
import threading

import pytest

from tools.dashboard import 挂载
from tools.tickets.http_server import TicketHTTPServer, TicketRequestHandler
from tools.tickets.service import TicketService
from tools.tickets.store import TicketStore


@pytest.fixture
def dashboard_server(tmp_path, monkeypatch):
    original = 挂载.索引器

    def isolated_index(root, base, cache_dir=None):
        return original(tmp_path / "会话", base, cache_dir=tmp_path / "缓存")

    monkeypatch.setattr(挂载, "索引器", isolated_index)
    store = TicketStore(tmp_path / "工单")
    store.ensure()
    handler = partial(TicketRequestHandler, directory=str(Path(__file__).resolve().parents[2] / "browser"))
    server = TicketHTTPServer(("127.0.0.1", 0), handler, TicketService(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert server.dashboard is None
        assert not thread.is_alive()


def request(server, path, method="GET", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_dashboard_mount_local_page_returns_200(dashboard_server):
    status, body = request(dashboard_server, "/dashboard/")
    assert status == 200
    assert "运行看板" in body.decode("utf-8")
    assert request(dashboard_server, "/dashboard")[0] == 302
    for path in ("style.css", "app.js", "vendor/vue/vue.global.prod.js", "mock/projects.json"):
        assert request(dashboard_server, "/dashboard/" + path)[0] == 200
    status, body = request(dashboard_server, "/dashboard/app.js")
    assert b"new URL(path.slice(1), new URL('.', location.href))" in body
    assert request(dashboard_server, "/dashboard/../server.py")[0] == 404
    assert request(dashboard_server, "/dashboard/%2e%2e/server.py")[0] == 404


def test_dashboard_mount_api_returns_200_without_ticket_collision(dashboard_server):
    status, body = request(dashboard_server, "/dashboard/api/status")
    assert status == 200
    assert "range_hours" in json.loads(body)
    status, body = request(dashboard_server, "/dashboard/api/projects")
    assert status == 200
    assert isinstance(json.loads(body), list)
    status, body = request(dashboard_server, "/api/tickets")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_dashboard_mount_nonlocal_listener_is_denied(dashboard_server):
    original = dashboard_server.server_address
    try:
        dashboard_server.server_address = ("", original[1])
        for path in ("/dashboard/", "/dashboard/api/status"):
            status, body = request(dashboard_server, path)
            assert status == 403
            assert "本机" in json.loads(body)["reason"]
        assert dashboard_server.dashboard is None
    finally:
        dashboard_server.server_address = original
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    connection = http.client.HTTPConnection("127.0.0.1", dashboard_server.server_port, timeout=10)
    try:
        connection.request("GET", "/api/tickets")
        assert connection.getresponse().read()
        dashboard_server.ssl_context = context
        connection.request("GET", "/dashboard/")
        response = connection.getresponse()
        assert response.status == 403
        assert "TLS" in json.loads(response.read())["reason"]
        assert dashboard_server.dashboard is None
    finally:
        dashboard_server.ssl_context = None
        connection.close()
