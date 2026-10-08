"""The Piterm Bridge server: TCP (frame/text) + HTTP JSON-RPC on Qt's event loop.

Zero threads: both listeners live on QGIS's own event loop (QTcpServer), so no
PyQGIS call ever leaves the GUI thread - the #1 source of plugin crashes in
other implementations. Long operations go through the job manager instead of
blocking the socket loop; the socket stays responsive while they run.

Ports (defaults): TCP 9877 (frames or NDJSON lines), HTTP 9879 (POST /rpc).
"""

from __future__ import annotations

import json
import time

from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtNetwork import QHostAddress, QTcpServer

from . import compat
from .constants import (DEFAULT_FRAME_PORT, DEFAULT_HOST, DEFAULT_HTTP_PORT,
                        HEARTBEAT_INTERVAL_MS, LISTEN_BACKLOG, MAX_CLIENTS,
                        MAX_MESSAGE_SIZE, MAX_OUTBOUND_PENDING, SETTINGS_PREFIX)
from .dispatcher import ConnectionContext, Dispatcher
from .errors import ErrorCode
from .security import QgisSettingsProvider, RateLimiter, SecurityManager
from .wire import MODE_FRAME, MODE_TEXT, MessageDecoder, encode_message


class Connection:  # frame-mode connection wrapper
    def __init__(self, server, socket):
        self.server = server
        self.socket = socket
        self.id = "conn-%d" % server._conn_counter.__next__()
        self.decoder = MessageDecoder()
        self.ctx = ConnectionContext(
            self.id, "frame", remote=_peer(socket),
            write=self.write, close=self.close)
        self.ctx.rate = RateLimiter(server.security.config.rate_per_sec)
        socket.setParent(server)
        socket.readyRead.connect(self.on_ready)
        socket.disconnected.connect(self.close)
        self.pending_payloads = []
        low_delay = compat.resolve(socket, "SocketOption.LowDelayOption",
                                   "LowDelayOption", default=None)
        if low_delay is not None:
            socket.setSocketOption(low_delay, 1)

    def on_ready(self):
        data = bytes(self.socket.readAll())
        if not data:
            return
        self.ctx.bytes_in += len(data)
        self.ctx.last_seen = time.time()
        for kind, payload in self.decoder.feed(data):
            if kind == "error":
                self._send({"id": None, "ok": False, "error": {
                    "code": ErrorCode.PARSE, "message": payload.get("message", "parse error")}})
                continue
            if self.decoder.mode == MODE_TEXT:
                self.ctx.mode = MODE_TEXT
            self.server.process_payload(self, payload)
            if self.ctx.closing:
                self.close()
                return
        if self.decoder.fatal:
            self.server.log("closing %s: %s" % (self.id, self.decoder.fatal), "warning")
            self.close()

    def send_response(self, response):
        self._send(response)
        for event in self.server.dispatcher.drain_pending(self.ctx):
            self._send({"event": event["event"], "data": event["data"],
                        "seq": event["seq"]})

    def _send(self, obj):
        try:
            data = encode_message(obj, self.ctx.mode, self.ctx.gzip_ok)
        except Exception as exc:  # noqa: BLE001 - never lose the socket to bad data
            data = encode_message({"id": None, "ok": False, "error": {
                "code": ErrorCode.PAYLOAD, "message": "response not serializable: %s" % exc}},
                MODE_FRAME, False)
        if self.socket.bytesToWrite() + len(data) > MAX_OUTBOUND_PENDING:
            self.server.log("dropping %s: outbound backlog exceeded" % self.id, "warning")
            self.close()
            return False
        self.ctx.bytes_out += len(data)
        self.socket.write(data)
        return True

    def write(self, data):
        return self._send_raw(data)

    def _send_raw(self, data):
        if self.socket.bytesToWrite() + len(data) > MAX_OUTBOUND_PENDING:
            self.close()
            return False
        self.socket.write(data)
        return True

    def close(self):
        try:
            self.socket.disconnectFromHost()
        except Exception:  # noqa: BLE001
            pass
        self.server.remove_connection(self)


class HttpConnection:
    """Minimal HTTP/1.1 JSON endpoint: POST /rpc + GET /health|/info|/commands|/events."""

    MAX_HEADER = 32 * 1024

    def __init__(self, server, socket):
        self.server = server
        self.socket = socket
        self.id = "http-%d" % server._conn_counter.__next__()
        self.buffer = bytearray()
        self.headers = {}
        self.body = b""
        self.state = "headers"
        self.ctx = ConnectionContext(
            self.id, "http", remote=_peer(socket),
            write=self.write, close=self.close)
        socket.setParent(server)
        socket.readyRead.connect(self.on_ready)
        socket.disconnected.connect(self.close)

    def on_ready(self):
        self.buffer.extend(bytes(self.socket.readAll()))
        self.ctx.bytes_in += len(self.buffer)
        while True:
            if self.state == "headers":
                idx = self.buffer.find(b"\r\n\r\n")
                if idx < 0:
                    if len(self.buffer) > self.MAX_HEADER:
                        self._respond_raw(431, {"error": "headers too large"})
                        self.close()
                    return
                head = bytes(self.buffer[:idx]).decode("latin-1", "replace")
                del self.buffer[: idx + 4]
                lines = head.split("\r\n")
                parts = lines[0].split(" ")
                self.method = parts[0].upper() if parts else "GET"
                self.path = parts[1] if len(parts) > 1 else "/"
                self.headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        self.headers[key.strip().lower()] = value.strip()
                try:
                    self.content_length = int(self.headers.get("content-length", "0"))
                except ValueError:
                    self.content_length = 0
                if self.content_length > MAX_MESSAGE_SIZE:
                    self._respond_raw(413, {"error": "payload too large"})
                    self.close()
                    return
                self.state = "body"
            if self.state == "body":
                if len(self.buffer) < self.content_length:
                    return
                self.body = bytes(self.buffer[: self.content_length])
                del self.buffer[: self.content_length]
                self._handle_request()
                self.state = "headers"
                if self.headers.get("connection", "").lower() == "close":
                    self.close()
                    return
                if not self.buffer:
                    return
            else:
                return

    # -- routing --------------------------------------------------------
    def _handle_request(self):
        path = self.path.split("?", 1)[0]
        if self.method == "OPTIONS":
            return self._respond_json(204, None)
        if self.method == "GET" and path in ("/health", "/"):
            return self._respond_json(200, {"ok": True, "service": "piterm-qgis",
                                            "time": time.time()})
        if self.method == "GET" and path == "/info":
            return self._respond_json(200, self.server.dispatcher.hello_payload(self.ctx))
        if self.method == "GET" and path == "/commands":
            return self._respond_json(200, {"commands": self._command_names()})
        if self.method == "GET" and path == "/events":
            query = _parse_query(self.path)
            payload = self.server.dispatcher.poll_events(
                query.get("since", 0), query.get("limit", 200))
            return self._respond_json(200, payload)
        if self.method == "POST" and path in ("/rpc", "/mcp"):
            return self._handle_rpc()
        return self._respond_json(404, {"error": "not found", "path": path})

    def _command_names(self):
        from .registry import iter_specs
        return [s.name for s in iter_specs()]

    def _handle_rpc(self):
        token = (self.headers.get("x-piterm-token")
                 or _auth_bearer(self.headers.get("authorization"))
                 or _parse_query(self.path).get("token"))
        try:
            message = json.loads(self.body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return self._respond_json(400, {"id": None, "ok": False, "error": {
                "code": ErrorCode.PARSE, "message": "JSON decode failed: %s" % exc}})
        if isinstance(message, dict) and token and "token" not in message:
            message["token"] = token
        response = self.server.dispatcher.handle_message(self.ctx, message)
        return self._respond_json(200, response)

    # -- output ---------------------------------------------------------
    def _respond_json(self, status, payload):
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False, default=str).encode("utf-8")
        head = (
            "HTTP/1.1 %d %s\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Headers: content-type, x-piterm-token, authorization\r\n"
            "Connection: keep-alive\r\n\r\n"
            % (status, _status_text(status), len(body)))
        self._send_raw(head.encode("latin-1") + body)

    def _respond_raw(self, status, payload):
        self._respond_json(status, payload)

    def write(self, data):
        return self._send_raw(data)

    def _send_raw(self, data):
        if self.socket.bytesToWrite() + len(data) > MAX_OUTBOUND_PENDING:
            self.close()
            return False
        self.ctx.bytes_out += len(data)
        self.socket.write(data)
        return True

    def close(self):
        try:
            self.socket.disconnectFromHost()
        except Exception:  # noqa: BLE001
            pass
        self.server.remove_connection(self)


class PitermServer(QObject):
    """Owns the listeners, the dispatcher and the shared managers."""

    def __init__(self, hub, iface=None, settings_provider=None, parent=None,
                 plugin_version="0.0.0"):
        super().__init__(parent)
        self.iface = iface
        self.hub = hub
        provider = settings_provider or QgisSettingsProvider()
        self.security = SecurityManager(provider, project_provider=self._project_dir)
        self.journal = hub.journal
        self.jobs = hub.jobs
        self.cursors = hub.cursors
        self.checkpoints = hub.checkpoints
        self.dispatcher = Dispatcher(hub, self.security, self.journal,
                                     self.jobs, self.cursors, plugin_version)
        self.dispatcher.server = self
        hub.attach_server(self)
        self._frame_server = None
        self._http_server = None
        self._connections = {}
        self._conn_counter = iter(range(1, 10 ** 9))
        self._in_dispatch = False
        # Strong references to live Qt tasks / contexts / feedback objects:
        # QgsProcessingAlgRunnerTask holds raw pointers to them, so letting
        # Python collect them mid-run corrupts the heap (reproduced on 3.44).
        self.qt_tasks = {}
        self.running = False
        self.host = DEFAULT_HOST
        self.frame_port = DEFAULT_FRAME_PORT
        self.http_port = DEFAULT_HTTP_PORT
        self.http_enabled = True
        self.start_errors = {}
        self.message_log = _RingLog(1000)
        self.ui_callback = None
        self._heartbeat = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, host=None, frame_port=None, http_port=None, http_enabled=None):
        self.host = host or DEFAULT_HOST
        self.frame_port = int(frame_port or DEFAULT_FRAME_PORT)
        self.http_port = int(http_port or DEFAULT_HTTP_PORT)
        if http_enabled is not None:
            self.http_enabled = bool(http_enabled)
        self.start_errors = {}
        self.running = True

        # Non-loopback without a token is refused outright: it would expose
        # arbitrary PyQGIS execution to the network.
        from .constants import LOOPBACK_HOSTS
        if str(self.host).lower() not in LOOPBACK_HOSTS and not (
                self.security.config.auth_enabled and self.security.config.token):
            self.running = False
            self.start_errors["all"] = (
                "refusing to bind %s without a token; set one in the dock or "
                "bind 127.0.0.1" % self.host)
            return self.status()

        self._frame_server = self._make_listener(self.host, self.frame_port,
                                                 Connection, "frame")
        if self.http_enabled:
            self._http_server = self._make_listener(self.host, self.http_port,
                                                    HttpConnection, "http")
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(HEARTBEAT_INTERVAL_MS)
        self._heartbeat.timeout.connect(self._on_heartbeat)
        self._heartbeat.start()
        self._notify("server", self.status())
        return self.status()

    def _make_listener(self, host, port, conn_class, kind):
        server = QTcpServer(self)
        server.setMaxPendingConnections(LISTEN_BACKLOG)
        if host in ("localhost",):
            local = compat.resolve(QHostAddress, "SpecialAddress.LocalHost",
                                   "LocalHost", default=None)
            address = QHostAddress(local) if local is not None else QHostAddress("127.0.0.1")
        else:
            address = QHostAddress(host)
        if not server.listen(address, port):
            self.start_errors[kind] = "cannot listen on %s:%s (%s)" % (
                host, port, server.errorString())
            self.running = False
            return None
        server.newConnection.connect(
            lambda cls=conn_class, srv=server: self._on_new_connection(cls, srv))
        return server

    def _on_new_connection(self, conn_class, listener):
        while listener.hasPendingConnections():
            if len(self._connections) >= MAX_CLIENTS:
                socket = listener.nextPendingConnection()
                socket.disconnectFromHost()
                continue
            socket = listener.nextPendingConnection()
            conn = conn_class(self, socket)
            self._connections[conn.id] = conn
            self.log("client connected: %s from %s" % (conn.id, conn.ctx.remote))
            self._notify("clients", self._clients_payload())

    def remove_connection(self, conn):
        if self._connections.pop(conn.id, None) is not None:
            self.log("client disconnected: %s" % conn.id)
            self._notify("clients", self._clients_payload())

    # ------------------------------------------------------------------
    # Dispatch with re-entrancy guard
    # ------------------------------------------------------------------
    def process_payload(self, conn, payload):
        """Run one inbound message; long handlers may pump the event loop,
        so nested readyRead signals queue instead of interleaving commands."""
        if self._in_dispatch:
            conn.pending_payloads.append(payload)
            return
        self._dispatch_now(conn, payload)

    def _dispatch_now(self, conn, payload):
        self._in_dispatch = True
        try:
            response = self.dispatcher.handle_message(conn.ctx, payload)
            conn.send_response(response)
            if conn.ctx.closing:
                conn.close()
        finally:
            self._in_dispatch = False
        self._drain_pending()

    def _drain_pending(self):
        if self._in_dispatch:
            return
        while True:
            candidate = None
            for conn in list(self._connections.values()):
                if getattr(conn, "pending_payloads", None):
                    candidate = (conn, conn.pending_payloads.pop(0))
                    break
            if candidate is None:
                return
            self._dispatch_now(*candidate)

    def stop(self):
        if self._frame_server:
            self._frame_server.close()
            self._frame_server = None
        if self._http_server:
            self._http_server.close()
            self._http_server = None
        for conn in list(self._connections.values()):
            try:
                conn.socket.disconnectFromHost()
            except Exception:  # noqa: BLE001
                pass
        self._connections.clear()
        if self._heartbeat:
            self._heartbeat.stop()
            self._heartbeat = None
        self.running = False
        self._notify("server", self.status())

    def _on_heartbeat(self):
        self.cursors.expire()
        # drop dead sockets
        for conn in list(self._connections.values()):
            try:
                unconnected = compat.resolve(conn.socket, "SocketState.UnconnectedState",
                                             "UnconnectedState", default=None)
                if unconnected is not None and conn.socket.state() == unconnected:
                    self.remove_connection(conn)
            except Exception:  # noqa: BLE001
                self.remove_connection(conn)

    # ------------------------------------------------------------------
    # Introspection / UI plumbing
    # ------------------------------------------------------------------
    def status(self):
        return {
            "running": self.running,
            "name": "Piterm Bridge",
            "host": self.host,
            "frame_port": self.frame_port if self._frame_server else None,
            "http_port": self.http_port if self._http_server else None,
            "http_enabled": self.http_enabled,
            "clients": len(self._connections),
            "uptime_s": round(time.time() - self.dispatcher.started, 1),
            "errors": dict(self.start_errors),
            "stats": self.dispatcher.stats,
        }

    def iter_contexts(self):
        for conn in list(self._connections.values()):
            yield conn.ctx

    def _clients_payload(self):
        return {"clients": [c.ctx.to_dict() for c in self._connections.values()]}

    def clients(self):
        return self._clients_payload()

    def kick(self, conn_id):
        conn = self._connections.get(conn_id)
        if conn is None:
            return {"kicked": False, "id": conn_id}
        conn.close()
        return {"kicked": True, "id": conn_id}

    def note_command(self, name, duration_ms, status):
        self._notify("command", {"name": name, "t_ms": round(duration_ms, 2),
                                 "status": status})

    def log(self, text, level="info"):
        entry = self.message_log.append(text, level)
        try:
            from . import compat
            compat.log(text, level)
        except Exception:  # noqa: BLE001
            pass
        self._notify("log", entry)
        return entry

    def capture_qgis_message(self, message, level="info", tag=""):
        self.message_log.append("[%s] %s" % (tag, message), level)
        self._notify("log", {"text": message, "level": level, "tag": tag,
                             "ts": time.time()})

    def messages(self, limit=200, level=None):
        return self.message_log.tail(limit, level)

    def _notify(self, kind, payload):
        if self.ui_callback:
            try:
                self.ui_callback(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def _project_dir(self):
        try:
            from qgis.core import QgsProject
            path = QgsProject.instance().fileInfo().absolutePath()
            return path or None
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------
class _RingLog:
    def __init__(self, maxlen):
        self.items = []
        self.maxlen = maxlen

    def append(self, text, level="info"):
        entry = {"ts": time.time(), "text": str(text), "level": level}
        self.items.append(entry)
        if len(self.items) > self.maxlen:
            del self.items[: len(self.items) - self.maxlen]
        return entry

    def tail(self, limit=200, level=None):
        items = self.items
        if level:
            items = [i for i in items if i["level"] == level]
        return list(items)[-max(1, min(int(limit), self.maxlen)):]


def _peer(socket):
    try:
        return "%s:%s" % (socket.peerAddress().toString(), socket.peerPort())
    except Exception:  # noqa: BLE001
        return "?"


def _status_text(status):
    return {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found",
            413: "Payload Too Large", 431: "Request Header Fields Too Large"}.get(status, "OK")


def _auth_bearer(value):
    if not value:
        return None
    parts = str(value).split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _parse_query(path):
    out = {}
    if "?" in path:
        for pair in path.split("?", 1)[1].split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                out[key] = value
    return out
