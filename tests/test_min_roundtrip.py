"""Round-trip tests: fake bridge server <-> BridgeClient <-> Minimal MCP server."""

from __future__ import annotations

import json
import socket
import struct
import threading

from piterm_qgis_mcp.minimal import (BridgeClient, BridgeError,
                                     MinimalMCPServer, encode_frame)

HEADER = struct.Struct(">I")


class FakeBridge:
    """Threaded TCP server speaking the frame protocol."""

    def __init__(self, token=""):
        self.token = token
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.commands = [
            {"name": "system.ping", "summary": "ping", "risk": "read",
             "tags": ["system"], "params": []},
            {"name": "layers.remove", "summary": "remove", "risk": "destructive",
             "tags": ["layers"],
             "params": [{"name": "layer", "required": True, "type": "str"}]},
            {"name": "canvas.screenshot", "summary": "shot", "risk": "read",
             "tags": ["canvas"], "params": []},
        ]

    def close(self):
        try:
            self.server.close()
        except OSError:
            pass

    def _serve(self):
        while True:
            try:
                conn, _addr = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buffer = bytearray()
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer.extend(chunk)
                if len(buffer) < 4:
                    continue
                length = HEADER.unpack_from(buffer, 0)[0]
                if len(buffer) < 4 + length:
                    continue
                body = bytes(buffer[4:4 + length])
                del buffer[:4 + length]
                message = json.loads(body.decode("utf-8"))
                response = self._answer(message)
                conn.sendall(encode_frame(response))

    def _answer(self, message):
        request_id = message.get("id")
        if self.token and message.get("token") != self.token:
            return {"id": request_id, "ok": False,
                    "error": {"code": "E_AUTH", "message": "bad token"}}
        ctype = message.get("type")
        if ctype == "system.ping":
            return {"id": request_id, "ok": True, "result": {"pong": True}}
        if ctype == "system.capabilities":
            return {"id": request_id, "ok": True,
                    "result": {"count": len(self.commands),
                               "commands": self.commands}}
        if ctype == "canvas.screenshot":
            return {"id": request_id, "ok": True,
                    "result": {"format": "png", "base64": "aGVsbG8=",
                               "size": [10, 10]}}
        if ctype == "layers.remove":
            return {"id": request_id, "ok": True, "result": {"removed": "x"}}
        return {"id": request_id, "ok": False,
                "error": {"code": "E_UNKNOWN_COMMAND",
                          "message": "unknown %s" % ctype}}


def test_client_roundtrip_and_error():
    fake = FakeBridge()
    try:
        client = BridgeClient("127.0.0.1", fake.port, client_name="test")
        assert client.call("system.ping")["pong"] is True
        try:
            client.call("nope.nothing")
            raise AssertionError("expected BridgeError")
        except BridgeError as exc:
            assert exc.code == "E_UNKNOWN_COMMAND"
        client.close()
    finally:
        fake.close()


def test_client_token_enforced():
    fake = FakeBridge(token="sesame")
    try:
        bad = BridgeClient("127.0.0.1", fake.port, token="wrong")
        try:
            bad.call("system.ping")
            raise AssertionError("expected BridgeError")
        except BridgeError as exc:
            assert exc.code == "E_AUTH"
        bad.close()
        good = BridgeClient("127.0.0.1", fake.port, token="sesame")
        assert good.call("system.ping")["pong"] is True
        good.close()
    finally:
        fake.close()


def test_minimal_mcp_handshake_and_tools():
    fake = FakeBridge()
    try:
        client = BridgeClient("127.0.0.1", fake.port)
        server = MinimalMCPServer(client)
        init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2024-11-05"}})
        assert init["result"]["protocolVersion"] == "2024-11-05"
        listing = server.handle({"jsonrpc": "2.0", "id": 2,
                                 "method": "tools/list", "params": {}})
        names = [t["name"] for t in listing["result"]["tools"]]
        assert "system_ping" in names
        assert "layers_remove" in names
        assert "canvas_screenshot" in names
        call = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "system_ping",
                                         "arguments": {}}})
        assert call["result"]["isError"] is False
        assert "pong" in call["result"]["content"][0]["text"]
        call_error = server.handle({"jsonrpc": "2.0", "id": 4,
                                    "method": "tools/call",
                                    "params": {"name": "does_not_exist",
                                               "arguments": {}}})
        assert call_error["result"]["isError"] is True
        image = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "canvas_screenshot",
                                          "arguments": {}}})
        blocks = image["result"]["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["mimeType"] == "image/png"
        client.close()
    finally:
        fake.close()


def test_client_reports_bridge_down():
    client = BridgeClient("127.0.0.1", 1, timeout=1)
    try:
        client.call("system.ping")
        raise AssertionError("expected BridgeError")
    except BridgeError as exc:
        assert exc.code in ("E_BRIDGE_DOWN", "E_IO")
