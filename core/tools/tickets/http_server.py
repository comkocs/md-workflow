"""工单台本地 HTTP 服务：静态页面和与 CLI 共用规则的 JSON API。"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import gzip
import json
import mimetypes
import socket
import ssl
import sys
import threading
import time
import webbrowser
from collections import defaultdict, deque
from datetime import timezone
from functools import partial
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import extension_loader
from .channel import PROTOCOL_VERSION
from .config import client_view
from .model import CONDUCTOR_SLOT, TicketError, now_text
from .service import TicketService
from .auth import AccountManager, SESSION_SECONDS


MAX_REQUEST_BYTES = 16 * 1024 * 1024
# 网页在 tickets.js 之前同步载入的位表配置(由服务端按配置文件现生成,不是静态文件)。
DESK_CONFIG_PATH = "/desk-config.js"
# 压缩阈值与级别。1 KB 以下不值得压;级别 6 是 gzip 的默认折中——
# 实测 7.48 MB 中文 JSON 压到 2.50 MB 只花 0.28 秒,再往上调收益很小、CPU 明显变贵。
GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 6
# 静态件里值得压的后缀。线上实测过一个 3.7 MB 的静态 js,
# 客户端明明发了 Accept-Encoding: gzip 也照样回全量——因为 gzip 当初只加在 _json 那条路上,
# SimpleHTTPRequestHandler 这条路一个字节都没压过。
# ★图片/字体/压缩包不进这张表:它们本身已经是压过的,再压一遍只费 CPU 不省字节。
GZIP_STATIC_SUFFIXES = frozenset({
    ".js", ".mjs", ".css", ".html", ".htm", ".json", ".svg", ".txt", ".md", ".csv", ".map", ".xml",
})
# 图片的缓存口径。★绝不能是 immutable / 长 max-age:图片允许同名覆盖,
# 缓存住就等于设计者屏上永远是旧图。no-cache 不是禁缓存——它是「存下来,但每次用之前问一句」,
# 没变就换回一个 304 空响应,53 KB 的正文省掉、新图也是下一次请求就拿到。
IMAGE_CACHE_CONTROL = "no-cache, must-revalidate"


class FailureLimiter:
    def __init__(self) -> None:
        self.failures: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.blocked_until: dict[tuple[str, str], float] = {}
        self.lock = threading.Lock()

    def blocked(self, category: str, address: str) -> bool:
        with self.lock:
            return self.blocked_until.get((category, address), 0) > time.time()

    def fail(self, category: str, address: str, limit: int) -> bool:
        now = time.time()
        key = (category, address)
        with self.lock:
            rows = self.failures[key]
            while rows and rows[0] < now - 600:
                rows.popleft()
            rows.append(now)
            if len(rows) >= limit:
                self.blocked_until[key] = now + 600
                rows.clear()
                return True
        return False

    def clear(self, category: str, address: str) -> None:
        with self.lock:
            self.failures.pop((category, address), None)
            self.blocked_until.pop((category, address), None)


class TicketHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    # socketserver 默认 backlog 只有 5。11 个总监窗加网页轮询,一阵子就排满;
    # 排满之后新连接的 SYN 直接被内核丢掉,客户端看到的是「超时」而不是「拒绝连接」,
    # 极难和「服务器宕机」区分开(2026-09-05 设计者报的就是这个现象)。
    request_queue_size = 128

    # 单条连接**每一次**套接字读写的上限(不是整个请求的上限)。
    # 卡住的客户端只拖死自己那条工作线程,拖不动别人。
    #
    # ★从 30 下调到 10,理由是 keep-alive 开了之后这个数多了一层含义:
    #   一条服完请求、正等下一个请求的**空闲**连接,会一直占着自己那条工作线程直到超时。
    #   浏览器对同一个源常开 6 条连接,13 个窗就是几十条;30 秒意味着这些线程要空占半分钟。
    #   Apache 在同样的「一连接一线程」模型下把 KeepAliveTimeout 默认设成 5 秒,正是这个原因。
    #   10 秒取了个折中:比 Apache 宽一倍,比原来省两倍。
    # ★下调**不会**伤到大上传:这个数管的是「单次 recv/send 之间最多能空多久」,
    #   不是整个请求的总时长。16 MB 的图片上传只要数据在持续流,每次 recv 都在毫秒级返回。
    # ★对 2026-09-05 那个慢握手打死 accept 循环的病,10 秒比 30 秒**更严**,不是更松。
    connection_timeout = 10

    def __init__(
        self, address: tuple[str, int], handler: type[SimpleHTTPRequestHandler], service: TicketService,
        token: str = "", auth: AccountManager | None = None,
    ) -> None:
        super().__init__(address, handler)
        self.service = service
        self.token = token
        self.auth = auth
        self.limiter = FailureLimiter()
        self.server_log = service.store.root / "server.log"
        # TLS 上下文挂在这里,由 serve() 填;绝不要去包监听套接字,原因见 get_request。
        self.ssl_context: ssl.SSLContext | None = None

    def get_request(self) -> tuple[Any, Any]:
        """每条连接单独包 TLS,并且把握手推迟到工作线程里做。

        ★2026-09-05 线上卡死的真因就在这里。原先是在 serve() 里写
            server.socket = context.wrap_socket(server.socket, server_side=True)
        把「监听套接字」整个包成了 SSLSocket。这样 accept() 返回的就已经是 SSLSocket,
        **TLS 握手是在 accept 那一步、也就是 serve_forever 的主循环线程里做的**,而且没有超时。
        于是任何一个连上来却不完成握手的客户端(端口扫描器、断掉的浏览器标签、半开连接)
        都能把整个 accept 循环永久钉死;ThreadingHTTPServer 的多线程要等 accept 返回之后
        才轮得到,永远轮不到。现场证据:ss 显示 Recv-Q 6 / Send-Q 5(accept 队列满),
        systemd 说服务 active,进程 Tasks 只剩 1(一个工作线程都没起来),
        外面看到的是连接超时,restart 之后立刻恢复。

        改法:accept 先拿到裸 socket(不阻塞),设好超时,再逐连接包 TLS,并且
        do_handshake_on_connect=False —— 握手推迟到工作线程第一次 recv 时才做。
        这样握手慢或者根本不握手的客户端,只会拖死自己那条线程,accept 循环照转。
        """
        sock, address = super().get_request()
        sock.settimeout(self.connection_timeout)
        if self.ssl_context is not None:
            sock = self.ssl_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        """握手失败/超时/对端掐断不值得刷一整页栈。

        HTTPS 端口上被人用明文 HTTP 探一下就打一屏 traceback,真出事时反而看不见。
        """
        error = sys.exc_info()[1]
        if isinstance(error, (ssl.SSLError, TimeoutError, ConnectionError, socket.timeout)):
            return
        super().handle_error(request, client_address)

class TicketRequestHandler(SimpleHTTPRequestHandler):
    server: TicketHTTPServer
    current_user = ""

    # ★开 keep-alive。改之前全程 HTTP/1.0 ⇒ 每个请求一次全新 TCP+TLS 握手。
    #   线上访问日志实测:最近 3000 条请求里 1672 条是 /api/image/,去重后只有 300 张,
    #   也就是说光图片这一项就白握了一千多次手,远程线路每次三个来回。
    # ★★开了复用之后,**任何一条响应少了或写错 Content-Length,下一个请求就会串包或吊死**。
    #   所以出响应的路径逐条核过一遍,结论记在这里,改这个文件的人请一起维护:
    #     _json / _html / _send_image        —— 自己发 Content-Length            ✓
    #     静态件(_static_gzip 与 SimpleHTTPRequestHandler.send_head) —— 发 Content-Length ✓
    #     send_error(:/setup 404、/auth/setup 404、静态件 404) —— 标准库自带
    #                                            Content-Length **且自带 Connection: close** ✓
    #     _authorized 的 302 跳转                —— 原来**没有** Content-Length,已补 0(见那一处)
    #     304                                    —— 按 RFC 7230 定义就没有正文,不需要长度 ✓
    #   还有一类不在响应上而在**请求**上:POST 的正文没被读走就回了响应,剩下的字节
    #   会被当成下一个请求的起始行——见 _guard_request_body。
    protocol_version = "HTTP/1.1"
    # 响应是先攒齐 headers 再一次 write 正文,Nagle 只会平白多等一个 RTT。
    disable_nagle_algorithm = True

    # 访问日志写失败的累计次数。★不许静默吞错:吞了要留计数 + 降级写 stderr。
    log_failures = 0
    log_failure_lock = threading.Lock()

    def end_headers(self) -> None:
        # 静态页与脚本(tickets.js/index.html)原来不带任何缓存指令,
        # 浏览器按启发式缓存会拿**旧 JS 配今天的数据**一起跑(2026-09-09 设计者
        # 屏上待复检红条两个数都是 undefined,据此误判复检卡了二十多单)。
        # no-cache 不是禁缓存:每次拿 Last-Modified 再验证,没变就 304,不加流量;
        # 已带指令的响应(API 的 no-store 等)不覆盖。
        buffer = getattr(self, "_headers_buffer", None)
        lines = [line.lower() for line in buffer] if buffer else []
        if not any(line.startswith(b"cache-control:") for line in lines):
            self.send_header("Cache-Control", "no-cache")
        # ★请求正文还没读走的那一次,必须在**头发出去之前**补一句 Connection: close。
        #   光把 close_connection 置 True 是服务端单方面关:客户端不知情,照样会把
        #   下一个请求写进这条连接,于是要么读到半截、要么撞上 ECONNRESET。
        #   原因与判据见 _request_body_is_pending。
        if self._request_body_is_pending() and not any(line.startswith(b"connection:") for line in lines):
            self.send_header("Connection", "close")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """记一行访问日志。★这里出任何事都不许影响响应本身。

        线上 journal 里的真异常:
            AttributeError: 'TicketRequestHandler' object has no attribute 'path'
        病因不是「偶发」,是 BaseHTTPRequestHandler.parse_request() 里
        `self.command, self.path = command, path` **是最后才赋值的**,而
        「Bad request version」「Bad request syntax」「414 URI 太长」这三条出错路径
        在赋值**之前**就调 send_error → log_error → 走到这里。于是 self.path 还不存在。
        后果不是少记一行日志,是那一次的 400/505 **整个发不出去,客户端收到空响应**。
        ⇒ 用 getattr 取,请求行都没解析出来的那一次就记空串。

        ★再往外还有一层:记账失败(磁盘满、日志文件被占、store 抛错)同样会把一个
          本来能发出去的响应变成空响应。所以整段兜住。
        ★但兜住 ≠ 静默吞:吞掉的错要留计数并降级写 stderr,否则日志链断了没人知道。
        """
        try:
            safe_path = urlparse(getattr(self, "path", "") or "").path
            self.server.service.store.append_jsonl(
                self.server.server_log,
                {
                    "client": self.client_address[0],
                    "method": getattr(self, "command", "") or "",
                    "path": safe_path,
                    "message": format % args,
                },
            )
        except Exception as exc:  # 记账失败不许吃掉响应
            with TicketRequestHandler.log_failure_lock:
                TicketRequestHandler.log_failures += 1
                total = TicketRequestHandler.log_failures
            try:
                print(
                    f"[工单台] 访问日志写入失败(累计 {total} 次),响应照发:{exc!r}",
                    file=sys.stderr, flush=True,
                )
            except Exception:
                pass  # stderr 都写不出去就真的无处可说了;绝不因此拖累响应

    def _request_body_is_pending(self) -> bool:
        """这一次请求的正文有没有被读干净。没读干净 ⇒ 这条连接不能再用。

        ★这是开 keep-alive 之后才有的坑,而且是**静默**的:
          POST 带着正文进来,服务端在读正文之前就回了 404/401(路径不认、没登录、
          Content-Length 格式不对、超过 16 MB),响应本身完全正确、长度也对,
          但正文那几百字节还躺在套接字里。HTTP/1.0 时代连接随即关闭,这些字节跟着没了;
          HTTP/1.1 下连接要复用,下一轮 handle_one_request 会把那堆 JSON 的第一行
          当成**请求起始行**去解析 —— 客户端明明发的是 GET,收回来的却是 400,
          或者更坏:两个响应的字节对错位(串包)。
        ⇒ 凡是没把正文读干净的那一次,就在那一条上关连接。不猜、不尝试跳过。
        """
        if getattr(self, "_request_body_read", False):
            return False
        headers = getattr(self, "headers", None)
        if headers is None:  # 请求行都没解析出来(send_error 从 parse_request 里打回来)
            return False
        return bool(headers.get("Content-Length") or headers.get("Transfer-Encoding"))

    def _guard_request_body(self) -> None:
        """兜底:响应已经发完(或压根没发出去)时再关一次连接。

        正常情况下 end_headers 已经把 Connection: close 写进头里、也顺手置了
        close_connection;这里只管**一个响应都没发出去**的那种极端路径。
        """
        if self._request_body_is_pending():
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._request_body_read = False
        try:
            self._get()
        finally:
            self._guard_request_body()

    def _get(self) -> None:
        parsed = urlparse(self.path)
        if self.server.auth and parsed.path == "/setup":
            if not self.server.auth.setup_available():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._auth_page("setup")
            return
        if self.server.auth and parsed.path == "/login":
            self._auth_page("login")
            return
        if self.server.auth and not self._authorized(parsed.path.startswith("/api/") or parsed.path.startswith("/img/")):
            return
        if self.server.auth and parsed.path == "/account":
            self._account_page()
            return
        if parsed.path.startswith("/img/"):
            # ★这一条原来在 try 之外:文件名不安全时 _send_image 抛 TicketError,
            #   一路冒到 handle_error,**一个字节的响应都没发**。/api/image/ 那条同样的
            #   调用在 try 里、回的是 400,两条路对同一个错给两种结果。这里补齐。
            try:
                self._send_image(unquote(parsed.path.removeprefix("/img/")))
            except (TicketError, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == DESK_CONFIG_PATH:
            self._send_desk_config()
            return
        if not parsed.path.startswith("/api/"):
            if parsed.path == "/":
                self.path = "/index.html"
            if not self._static_gzip():
                super().do_GET()
            return
        if not self.server.auth and not self._authorized(True):
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/slots":
                self._ok({
                    "slots": self.server.service.store.read_json(self.server.service.store.slots_path),
                    "staff": self.server.service.store.load_staff(),
                    # 章程路径随名册一起下发,卡片上要看得见——
                    # 那是新人开窗前唯一该读的东西,不该让人自己去猜目录。
                    "charters": self.server.service.slot_charters(),
                })
            elif parsed.path == "/api/tickets":
                slot, state = self._one(query, "slot"), self._one(query, "state")
                kind = self._one(query, "type")
                pending = self._one(query, "shot-pending").lower() in {"1", "true", "yes"}
                needle = self._one(query, "q").strip().lower()
                if needle:
                    # ★搜索必须在**全文**上匹配,并且把命中的那几张**整份**回过去。
                    #   列表那一趟为了体积摘掉了正文/答复/接线证据/备注(card_view),
                    #   若搜索也在精简行上匹配,搜「正文里的词」就再也搜不到——
                    #   而且页面不会报错,人只会以为「没有这张单」。那是最坏的静默失败。
                    #   命中集通常只有几张,回全文不值几个字节。
                    rows = [
                        row for row in self.server.service.list_tickets(slot, state, kind, pending)
                        if needle in json.dumps(row, ensure_ascii=False).lower()
                    ]
                else:
                    rows = self.server.service.list_cards(slot, state, kind, pending)
                self._ok(rows)
            elif parsed.path.startswith("/api/ticket/"):
                ticket = self.server.service.store.load_ticket(unquote(parsed.path.removeprefix("/api/ticket/")))
                self._ok(self.server.service.ticket_view(ticket))
            elif parsed.path == "/api/thread-summary":
                # 13 条线的未读摘要,一次几 KB。全文只有当前在看的那一位才拉。
                self._ok(self.server.service.thread_summaries())
            elif parsed.path == "/api/changes":
                # 网页增量刷新。「第 N 行流水之后有什么动静」——
                # 稳态下这一趟只回几十字节,而整份是 2.45 MB(压后)。
                self._ok(self.server.service.changes_since(int(self._one(query, "since") or 0)))
            elif parsed.path == "/api/inbox":
                slot = self._one(query, "slot")
                actor = self._one(query, "for")
                if self._one(query, "all") == "1":
                    rows = self.server.service.store.read_jsonl(self.server.service.store.thread_path(slot))
                    # 对话线本来就只追加,所以「第 N 行之后」= 直接切片,不会漏也不会重。
                    # 网页拿它做增量:12 条不看的线每次只回一个空数组。
                    since = self._one(query, "since")
                    if since:
                        try:
                            start = max(0, min(int(since), len(rows)))
                        except ValueError:
                            start = 0
                        self._ok({"起点": start, "总行数": len(rows), "新增": rows[start:]})
                        return
                    self._ok(rows)
                else:
                    mark_read = self._one(query, "mark") == "1"
                    if mark_read:
                        with self.server.service.store.locked():
                            self._ok(self.server.service.inbox(slot, actor, True))
                    else:
                        self._ok(self.server.service.inbox(slot, actor, False))
            elif parsed.path == "/api/digest":
                raw = self._one(query, "hours") or "24"
                self._ok(self.server.service.digest(int(raw)))
            elif parsed.path == "/api/state":
                # 顶栏那四项的唯一来源；写口子只在 CLI 的 state set 上，网页只读。
                self._ok(self.server.service.state_board())
            elif parsed.path == "/api/staff":
                # 默认只回在岗，?all=1 才要全量——与命令行 staff list --all 同一把尺子。
                self._ok(self.server.service.list_staff(
                    self._one(query, "slot") or None, self._one(query, "all") == "1",
                ))
            elif parsed.path == "/api/running-windows":
                # 在跑窗口列表:与命令行 running 同一个来源(service.running_windows),不另算第二遍。
                self._ok(self.server.service.running_windows(self._one(query, "slot")))
            elif parsed.path == "/api/me" and self.server.auth:
                self._ok(self.server.auth.account(self.current_user))
            elif parsed.path.startswith("/api/image/"):
                self._send_image(unquote(parsed.path.removeprefix("/api/image/")))
            else:
                self._error(HTTPStatus.NOT_FOUND, "找不到这个工单接口。")
        except (TicketError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover - safety net is exercised by integration use
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务处理失败：{exc}")

    def do_POST(self) -> None:  # noqa: N802
        self._request_body_read = False
        try:
            self._post()
        finally:
            self._guard_request_body()

    def _post(self) -> None:
        parsed = urlparse(self.path)
        if self.server.auth and parsed.path in {"/auth/login", "/auth/setup", "/auth/token-login"}:
            self._login_or_setup(parsed.path)
            return
        if self.server.auth and not self._authorized(True):
            return
        if self.server.auth and parsed.path == "/auth/logout":
            self._logout()
            return
        if self.server.auth and parsed.path == "/api/token":
            self._token_action()
            return
        if not parsed.path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "找不到这个工单接口。")
            return
        if not self.server.auth and not self._authorized(True):
            return
        try:
            payload = self._read_json()
            with self.server.service.store.locked():
                if parsed.path == "/api/action":
                    result = self._action(payload)
                elif parsed.path == "/api/say":
                    result = self.server.service.say_uploaded(
                        str(payload.get("slot", "")),
                        str(payload.get("by", "")),
                        str(payload.get("text", "")),
                        list(payload.get("images") or []),
                        str(payload.get("ref", "")),
                    )
                elif parsed.path == "/api/upload":
                    result = self._upload(payload)
                elif parsed.path == "/api/cli":
                    result = self._cli(payload)
                else:
                    self._error(HTTPStatus.NOT_FOUND, "找不到这个工单接口。")
                    return
                if self.server.auth:
                    self._audit_write(parsed.path, str(payload.get("op", "")))
            self._ok(result)
        except (TicketError, ValueError, TypeError, KeyError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover - safety net is exercised by integration use
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务处理失败：{exc}")

    def _action(self, data: dict[str, Any]) -> Any:
        service = self.server.service
        op = str(data.get("op", ""))
        ticket_id = str(data.get("ticket", ""))
        actor = str(data.get("by", ""))
        if op == "new":
            return service.create_dispatch(
                str(data.get("slot", "")), str(data.get("title", "")), data.get("source") or [],
                str(data.get("consumer", "")), str(data.get("assign", "")), actor or CONDUCTOR_SLOT,
                str(data.get("notes", "")), data.get("tier"), data.get("context_lines"), data.get("deliverables") or [],
                # internal 原样透传,缺了就让服务端拒——服务端是唯一真闸,前端勾选只是方便。
                data.get("internal"), str(data.get("body", "")), str(data.get("taskbook", "")),
                window=str(data.get("window", "")),
            )
        if op == "ask":
            return service.create_question(
                str(data.get("type", "需求")), str(data.get("slot", "")), str(data.get("title", "")),
                str(data.get("body", "")), actor or "设计者", data.get("source") or [],
                str(data.get("consumer", "")), str(data.get("tier", "")), data.get("context_lines"),
                str(data.get("taskbook", "")),
            )
        if op == "set":
            ticket, changes = service.edit(
                ticket_id, actor,
                data.get("taskbook"), data.get("assign"), data.get("source"),
                data.get("body"), data.get("consumer"),
                internal=data.get("internal"), window=data.get("window"),
            )
            return {"工单": ticket, "改动": changes}
        if op == "claim":
            return service.claim(ticket_id, actor)
        if op == "open-window":
            ticket, warning = service.open_window(
                ticket_id, actor, str(data.get("actual_model", "")), str(data.get("actual_platform", "")),
            )
            return {"工单": ticket, "提示": warning}
        if op == "submit":
            return service.submit(
                ticket_id, str(data.get("evidence", "")),
                str(data.get("verify_command", "")), str(data.get("raw_output", "")),
                handoff=str(data.get("handoff", "")),
                gate_report=str(data.get("gate_report", "")),
            )
        if op == "verify":
            # 判卷与复验并行。这里只记结论,不改状态——
            # 退回照旧走 rework/退回单,那两条路才带责任归属与返工次数。
            ticket, hint = service.verify(
                ticket_id, actor, str(data.get("result", "")),
                str(data.get("gates", "")), str(data.get("evidence", "")),
            )
            return {"工单": ticket, "提示": hint}
        if op == "judge":
            ticket, warning = service.judge(
                ticket_id, bool(data.get("passed")), actor,
                str(data.get("reason", "")), str(data.get("verdict", "")), str(data.get("blame", "")),
                str(data.get("strike_handoff", "")),
            )
            return {"工单": ticket, "提示": warning}
        if op == "merge":
            return service.merge(ticket_id, actor)
        if op == "live":
            return service.live_uploaded(
                ticket_id, str(data.get("filename", "")), actor, str(data.get("shot", ""))
            )
        if op == "live-batch":
            ticket_ids = data.get("tickets")
            if not isinstance(ticket_ids, list) or not all(isinstance(value, str) for value in ticket_ids):
                raise TicketError("批量 live 的 tickets 必须是工单号列表。")
            return service.live_batch_uploaded(
                ticket_ids, str(data.get("filename", "")), actor, str(data.get("shot", ""))
            )
        if op == "rework-from-review":
            # 待复检退回重做。blame 默认「出题」,与 CLI 同一个默认值——
            # 两处默认不一样的话,同一个动作从网页点和从命令行跑会记出两本不同的账。
            return service.rework_from_review(
                ticket_id, str(data.get("reason", "")), actor, str(data.get("blame", "") or "出题"),
            )
        if op == "close":
            return service.close(ticket_id, actor)
        if op == "void":
            return service.void(ticket_id, str(data.get("reason", "")), actor)
        if op == "block":
            return service.block(ticket_id, str(data.get("reason", "")), actor or CONDUCTOR_SLOT)
        if op == "unblock":
            return service.unblock(ticket_id, actor or CONDUCTOR_SLOT)
        if op == "transfer":
            return service.transfer(ticket_id, str(data.get("to", "")), str(data.get("reason", "")), actor)
        if op == "answer":
            return service.answer(ticket_id, str(data.get("answer", "")), actor)
        # 配置里启用的扩展可以挂自己的 op(例如 deploy_record 的 "deploy-record")。
        handler = extension_loader.http_op(op)
        if handler is not None:
            return handler(service, data)
        raise TicketError(f"不认识的动作：{op}")

    def _upload(self, data: dict[str, Any]) -> Any:
        encoded = str(data.get("base64", ""))
        if "," in encoded and encoded.lstrip().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise TicketError("上传内容不是有效的 base64 图片。") from exc
        filename = Path(str(data.get("filename", "浏览器上传"))).name
        actor = str(data.get("by", ""))
        if data.get("live") is True:
            return {"图片": self.server.service.upload_live_bytes(raw, filename, actor)}
        if data.get("ticket"):
            ticket, image = self.server.service.attach_bytes(
                str(data["ticket"]), raw, filename, str(data.get("origin", "other")), actor
            )
            return {"工单": ticket, "图片": image}
        if data.get("slot"):
            return {"图片": self.server.service.upload_thread_bytes(str(data["slot"]), raw, filename, actor)}
        raise TicketError("上传图片必须写 ticket 或 slot。")

    def _cli(self, data: dict[str, Any]) -> dict[str, Any]:
        from .ticket import execute, parser

        argv = data.get("argv")
        if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
            raise TicketError("远程命令参数必须是字符串列表。")
        try:
            args = parser().parse_args(argv)
        except TicketError as exc:
            try:
                client_protocol = int(data.get("client_protocol", 0))
            except (TypeError, ValueError):
                client_protocol = 0
            if client_protocol > PROTOCOL_VERSION:
                raise TicketError(
                    "你的客户端比服务器新,服务器还没上这一版:"
                    f"客户端 {client_protocol} / 服务端 {PROTOCOL_VERSION};"
                    "请等平台位上服,或改用并线前的客户端。"
                ) from exc
            raise
        if args.command in {"serve", "migrate", "dump", "account"}:
            raise TicketError("这个管理命令只能在服务端本机执行。")
        payload, text = execute(args, self.server.service)
        return {"payload": payload, "text": text}

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise TicketError("请求长度格式不对。") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise TicketError("请求为空或超过 16MB。")
        raw = self.rfile.read(length)
        # ★正文是否真读干净,决定这条连接还能不能复用(见 _guard_request_body)。
        #   客户端少发字节时 read 会短读,那条连接已经废了,照样要关。
        self._request_body_read = len(raw) == length
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TicketError("请求不是有效的 UTF-8 JSON。") from exc
        if not isinstance(value, dict):
            raise TicketError("请求正文必须是 JSON 对象。")
        return value

    def _login_or_setup(self, path: str) -> None:
        address = self.client_address[0]
        if self.server.limiter.blocked("login", address):
            self._error(HTTPStatus.TOO_MANY_REQUESTS, "登录失败过多，请 10 分钟后再试。")
            return
        try:
            payload = self._read_json()
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            if path == "/auth/token-login":
                username = self.server.auth.token_user(str(payload.get("token", ""))) if self.server.auth else ""
                if not username:
                    raise TicketError("个人令牌无效。")
                session = self.server.auth.create_session(username)
            elif path == "/auth/setup":
                if not self.server.auth or not self.server.auth.setup_available(username):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                session = self.server.auth.set_initial_password(username, password)
            else:
                session = self.server.auth.login(username, password) if self.server.auth else ""
            self.server.limiter.clear("login", address)
            self.server.limiter.clear("401", address)
            self.current_user = username
            self._json(
                HTTPStatus.OK, {"ok": True, "result": {"用户名": username}},
                cookie=f"ticket_session={session}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}",
            )
        except (TicketError, ValueError, TypeError):
            self.server.limiter.fail("login", address, 10)
            self._error(HTTPStatus.UNAUTHORIZED, "用户名或密码不对，或首次设密窗口已关闭。")

    def _logout(self) -> None:
        session = self._cookie("ticket_session")
        if self.server.auth:
            self.server.auth.end_session(session)
        self._json(
            HTTPStatus.OK, {"ok": True, "result": "已退出"},
            cookie="ticket_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0",
        )

    def _token_action(self) -> None:
        try:
            payload = self._read_json()
            action = str(payload.get("op", "generate"))
            if action == "revoke":
                self.server.auth.revoke_api_token(self.current_user)
                result: Any = {"已吊销": True}
            elif action == "generate":
                result = {"token": self.server.auth.issue_api_token(self.current_user), "提示": "只显示这一次，请立即保存。"}
            else:
                raise TicketError("令牌动作只能是 generate 或 revoke。")
            self._audit_write("/api/token", action)
            self._ok(result)
        except (TicketError, ValueError, TypeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def _audit_write(self, path: str, operation: str) -> None:
        self.server.service.store.append_jsonl(
            self.server.service.store.log_path,
            {
                "时间": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "工单号": "",
                "事件": "http-write",
                "发言人": self.current_user,
                "状态": "",
                "说明": f"{path} {operation}".strip(),
                "事件序号": 0,
                "来源IP": self.client_address[0],
                "账号": self.current_user,
            },
        )

    def _cookie(self, name: str) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(name)
        return morsel.value if morsel else ""

    def _auth_page(self, mode: str) -> None:
        setup = mode == "setup"
        username = self.server.auth.pending_username() if setup and self.server.auth else ""
        title = "首次设置管理员密码" if setup else "登录工单台"
        endpoint = "/auth/setup" if setup else "/auth/login"
        readonly = "readonly" if setup else ""
        token_form = "" if setup else """<hr><h2>使用个人 API token</h2><form id=tokenForm><label>个人令牌</label><input name=token type=password autocomplete=off required><button>用令牌登录</button></form>"""
        html = f"""<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>{title}</title><style>body{{font-family:system-ui;background:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh}}main{{background:white;padding:32px;border-radius:16px;box-shadow:0 10px 35px #0002;width:min(360px,85vw)}}label,input,button{{display:block;width:100%;box-sizing:border-box}}label{{margin:16px 0 6px}}input,button{{padding:12px;border-radius:8px;border:1px solid #b8c2ce}}button{{margin-top:20px;background:#153b66;color:white}}#message{{color:#a11}}</style>
<main><h1>{title}</h1><p>{'此页面只开放 30 分钟，设完永久关闭。' if setup else '请输入你的工单台账号。'}</p><form id=mainForm><label>用户名</label><input name=username autocomplete=username value={json.dumps(username)} {readonly}><label>密码</label><input name=password type=password autocomplete={'new-password' if setup else 'current-password'} required minlength=12><button>继续</button></form>{token_form}<p id=message></p></main>
<script>async function send(e,url){{e.preventDefault();let f=new FormData(e.target),r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(Object.fromEntries(f))}}),j=await r.json().catch(()=>({{reason:'请求失败'}}));if(r.ok)location='/';else document.querySelector('#message').textContent=j.reason||'未能登录';}}document.querySelector('#mainForm').onsubmit=e=>send(e,'{endpoint}');let tf=document.querySelector('#tokenForm');if(tf)tf.onsubmit=e=>send(e,'/auth/token-login');</script></html>"""
        self._html(html)

    def _account_page(self) -> None:
        html = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>我的令牌</title><style>body{font-family:system-ui;max-width:760px;margin:50px auto;padding:0 20px}button{padding:10px 16px;margin-right:10px}pre{white-space:pre-wrap;word-break:break-all;background:#f3f5f7;padding:16px}</style><h1>我的令牌</h1><p>新令牌只显示一次；生成新令牌会立即让旧令牌失效。</p><button id=g>生成新令牌</button><button id=r>吊销令牌</button><a href='/'>返回工单台</a><pre id=o>令牌不会自动显示。</pre><script>async function go(op){let r=await fetch('/api/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op})}),j=await r.json();document.querySelector('#o').textContent=r.ok?(j.result.token||'已吊销'):j.reason}g.onclick=()=>go('generate');r.onclick=()=>go('revoke');</script></html>"""
        self._html(html)

    def _send_desk_config(self) -> None:
        """网页的位表配置(位名、特殊位、任务档):与服务端同一份配置文件,不在网页里另抄。"""
        payload = (
            "window.TICKET_DESK_CONFIG = "
            + json.dumps(client_view(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + ";\n"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self, api_request: bool = True) -> bool:
        if not self.server.auth:
            token = self.server.token
            if token and self.headers.get("X-Ticket-Token", "") != token:
                self._error(HTTPStatus.UNAUTHORIZED, "未获授权：请提供正确的 X-Ticket-Token。")
                return False
            return True
        address = self.client_address[0]
        if self.server.limiter.blocked("401", address):
            self._error(HTTPStatus.TOO_MANY_REQUESTS, "该地址认证失败过多，请 10 分钟后再试。")
            return False
        session = self._cookie("ticket_session")
        user = self.server.auth.session_user(session)
        supplied = self.headers.get("X-Ticket-Token", "")
        if not supplied:
            supplied = self._one(parse_qs(urlparse(self.path).query), "t")
        if not user and supplied:
            user = self.server.auth.token_user(supplied)
            if not user and self.server.token and hmac.compare_digest(supplied, self.server.token):
                user = "服务令牌"
        if user:
            self.current_user = user
            return True
        self.server.limiter.fail("401", address, 20)
        if api_request:
            self._error(HTTPStatus.UNAUTHORIZED, "未登录或令牌无效。")
        else:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/login")
            # ★HTTP/1.1 下没有 Content-Length 又不是 chunked 的响应,客户端会一直读到
            #   连接关闭为止 —— 这条跳转原来正好是这样,开了复用之后会把浏览器吊死在这里。
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        return False

    @staticmethod
    def _etag(payload: bytes, suffix: str = "") -> str:
        """按**内容**算 ETag,不按 mtime+size 算。

        图片允许同名覆盖(store.save_image 是 os.replace)。mtime+size 这条路上
        「同尺寸、同一秒内覆盖」会算出一模一样的 ETag,客户端就永远拿不到新图——
        而且这种失败是静默的,没有任何报错。内容 hash 没有这个洞。
        代价只是多读一次文件:53 KB 从页缓存读出来是微秒级,换掉的是远程线路上 53 KB 的正文。
        ★带 suffix 是因为 gzip 过的是**另一份表示**,RFC 7232 要求两者 ETag 不同,
          否则不带 Accept-Encoding 的客户端会拿着压缩版的 ETag 换到一个 304。
        """
        digest = hashlib.blake2b(payload, digest_size=16).hexdigest()
        return f'"{digest}{suffix}"'

    def _conditional_hit(self, etag: str, mtime: float) -> bool:
        """客户端手上那份还新不新。按 RFC 7232 §3.3:有 If-None-Match 就只看它。"""
        supplied = self.headers.get("If-None-Match", "")
        if supplied:
            candidates = {value.strip().removeprefix("W/").strip() for value in supplied.split(",")}
            return "*" in candidates or etag in candidates
        since = self.headers.get("If-Modified-Since", "")
        if not since:
            return False
        try:
            stamp = email.utils.parsedate_to_datetime(since)
        except (TypeError, ValueError, IndexError):
            return False
        if stamp is None:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        # HTTP 日期只精确到秒,文件 mtime 带小数。不向下取整的话,同一秒内写的文件
        # 会被判成「比客户端那份新」,于是永远 200、永远 304 不了。
        return int(mtime) <= int(stamp.timestamp())

    def _send_not_modified(self, etag: str) -> None:
        # 304 按 RFC 7230 §3.3.1 定义就没有正文,所以不发 Content-Length 也不会让
        # keep-alive 串包:客户端看见 304 就知道正文长度是 0。
        self.send_response(HTTPStatus.NOT_MODIFIED)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", IMAGE_CACHE_CONTROL)
        self.end_headers()

    def _send_image(self, name: str) -> None:
        safe_name = Path(name).name
        if safe_name != name or not safe_name:
            raise TicketError("图片文件名不安全。")
        path = self.server.service.store.images_dir / safe_name
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "找不到这张图片。")
            return
        payload = path.read_bytes()
        etag = self._etag(payload)
        mtime = path.stat().st_mtime
        # ★图片原来发 Cache-Control: no-store ⇒ 浏览器**绝不缓存**。
        #   线上访问日志实测:最近 3000 条请求里 1672 条是 /api/image/,去重后只有 300 张,
        #   同一张图被重拉 12 次,每次都是完整正文。
        # ★为什么不用 immutable / 长 max-age:图片允许同名覆盖(store.save_image)。
        #   缓存住就意味着覆盖之后设计者屏上还是旧图,而且他无从察觉——
        # 那次「旧 JS 配今天的数据」就是同一种病,他据此误判了二十多单。
        #   ⇒ 走 ETag + no-cache/must-revalidate:每次都问一句,但没变就只回一个 304 空响应,
        #     53 KB 的正文省掉了,新图也是下一次请求就拿到,零陈旧窗口。
        #     这和 end_headers 里静态页用 no-cache 的口径是同一把尺子。
        if self._conditional_hit(etag, mtime):
            self._send_not_modified(etag)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", self.date_time_string(mtime))
        self.send_header("Cache-Control", IMAGE_CACHE_CONTROL)
        self.end_headers()
        self.wfile.write(payload)

    def _static_gzip(self) -> bool:
        """静态件走 gzip + 条件请求。处理掉了回 True,没处理回 False 交还给标准库。

        ★gzip 当初只加在 _json 那条路上,静态件这条从来没压过。
          线上实测一个 3.7 MB 的静态 js 带着 Accept-Encoding: gzip
          请求也照回全量 —— 在约 480 KB/s 的线路上,光这一件就是七八秒。
        ★口径与 _json 对齐:只对**主动声明能收 gzip** 的客户端压。命令行那条路
          (remote.py 用 http.client)默认不发 Accept-Encoding,一个字节都不受影响。
        ★没进 GZIP_STATIC_SUFFIXES 的(图片、字体)与小于 GZIP_MIN_BYTES 的直接交还标准库,
          由它按原样发 —— 行为与改动前完全一样。
        """
        if "gzip" not in self.headers.get("Accept-Encoding", "").lower():
            return False
        # translate_path 带了标准库那套目录穿越防护,不要自己拼路径。
        path = Path(self.translate_path(self.path))
        if path.suffix.lower() not in GZIP_STATIC_SUFFIXES or not path.is_file():
            return False
        try:
            raw = path.read_bytes()
            mtime = path.stat().st_mtime
        except OSError:
            return False  # 读不动就交还标准库,由它去报 404/403
        if len(raw) < GZIP_MIN_BYTES:
            return False  # 几百字节压完可能更大,还白费一次 CPU
        etag = self._etag(raw, "-gzip")
        if self._conditional_hit(etag, mtime):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()  # Cache-Control 由 end_headers 补 no-cache
            return True
        payload = gzip.compress(raw, GZIP_LEVEL)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", self.date_time_string(mtime))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        return True

    def _ok(self, result: Any) -> None:
        self._json(HTTPStatus.OK, {"ok": True, "result": result})

    def _error(self, status: HTTPStatus, reason: str) -> None:
        self._json(status, {"ok": False, "reason": reason})

    def _json(self, status: HTTPStatus, value: dict[str, Any], cookie: str = "") -> None:
        # server_time:服务器本地的真时刻随每个信封下发。
        # 客户端机器的时区五花八门,拿本机 `date` 的墙钟对表必错——
        # 真正可比的是这格与客户端自己算出的绝对时刻。
        value = dict(value, server_protocol=PROTOCOL_VERSION, server_time=now_text())
        payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        # ★响应压缩。设计者 2026-09-08 报「刷新工单页要多等十几秒」,量下来
        # 病根不在服务端算得慢——取数+序列化只花 1.1 秒——而在**线路**:
        # 一次刷新下行 11.17 MB(工单 7.48 MB + 13 条对话线 3.59 MB),
        # 线路约 480 KB/s 时,光传就要二十多秒。
        # 而这些全是中文 JSON,gzip 压到 32%(7.48 MB → 2.50 MB),压一次只花 0.28 秒。
        # ★只对**主动声明能收 gzip** 的客户端压:命令行那条路(remote.py)用 http.client,
        #   默认不发 Accept-Encoding,所以一个字节都不受影响;浏览器一律会发,并自动解压。
        # ★小响应不压:几百字节的回执压完可能更大,还白费一次 CPU。
        encoding = ""
        if len(payload) >= GZIP_MIN_BYTES and "gzip" in self.headers.get("Accept-Encoding", "").lower():
            payload = gzip.compress(payload, GZIP_LEVEL)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _one(query: dict[str, list[str]], key: str) -> str:
        return query.get(key, [""])[0]


def serve(
    service: TicketService, host: str, port: int, token: str = "", open_browser: bool = False,
    tls_cert: str = "", tls_key: str = "", auth: AccountManager | None = None,
) -> None:
    extension_loader.load_configured()
    browser_root = Path(__file__).resolve().parents[1] / "browser"
    handler = partial(TicketRequestHandler, directory=str(browser_root))
    server = TicketHTTPServer((host, port), handler, service, token, auth)
    scheme = "https" if tls_cert and tls_key else "http"
    if bool(tls_cert) != bool(tls_key):
        server.server_close()
        raise TicketError("TLS 证书与私钥必须同时提供。")
    if tls_cert and tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        # ★逐连接包,不包监听套接字;为什么见 TicketHTTPServer.get_request。
        server.ssl_context = context
    url = f"{scheme}://127.0.0.1:{server.server_address[1]}/"
    print(f"工单台服务已启动：{url}")
    if token:
        print("API 已启用 X-Ticket-Token 校验。")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
