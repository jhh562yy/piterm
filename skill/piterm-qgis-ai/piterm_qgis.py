#!/usr/bin/env python3
"""Piterm QGIS AI - single-file, stdlib-only bridge client + MCP stdio server.

This file has three jobs and no third-party dependencies:

1. **Client**: talk to the QGIS plugin's bridge (TCP frames or NDJSON lines,
   optional token, optional gzip) and return structured results.
2. **MCP stdio server**: expose every bridge command as an MCP tool to any
   MCP-capable client (``python min.py --mcp-stdio``). Tool schemas come from
   the live bridge (``system.capabilities``) or from a bundled ``manifest.json``
   when QGIS is not running, so the tool list never drifts from the plugin.
3. **CLI**: ``ping`` / ``call`` / ``tools`` / ``screenshot`` for shells, agents
   and skills that do not speak MCP at all.

Environment variables:
    PITERM_QGIS_HOST   default 127.0.0.1
    PITERM_QGIS_PORT   default 9877
    PITERM_QGIS_TOKEN  optional shared secret
    PITERM_QGIS_CLIENT client name reported on the wire
"""

from __future__ import annotations

import argparse
import io
import json
import gzip
import os
import re
import socket
import struct
import sys
import time

DEFAULT_HOST = os.environ.get("PITERM_QGIS_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PITERM_QGIS_PORT", "9877") or 9877)
DEFAULT_TIMEOUT = 60.0
CONNECT_TIMEOUT = 4.0
MAX_MESSAGE = 64 * 1024 * 1024
HEADER = struct.Struct(">I")
CLIENT_VERSION = "1.0.0"
PROTOCOL_FALLBACK = "2024-11-05"
GZIP_MAGIC = b"\x1f\x8b"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class BridgeError(Exception):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_text(self):
        extra = ("\n" + json.dumps(self.detail, ensure_ascii=False, indent=2)
                 if self.detail else "")
        return "%s: %s%s" % (self.code, self.message, extra)


# ---------------------------------------------------------------------------
# codec
# ---------------------------------------------------------------------------
def _json_dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=_json_default)


def _json_default(obj):
    if isinstance(obj, (bytes, bytearray, memoryview)):
        import base64
        return {"__b64__": base64.b64encode(bytes(obj)).decode("ascii")}
    return str(obj)


def encode_frame(obj, compress=False, threshold=256 * 1024):
    body = _json_dump(obj).encode("utf-8")
    if compress and len(body) >= threshold:
        body = gzip.compress(body, 6)
    return HEADER.pack(len(body)) + body


def encode_line(obj):
    return _json_dump(obj).encode("utf-8") + b"\n"


class ResponseDecoder:
    """Reads either a framed response or a single NDJSON line."""

    def __init__(self, mode="frame"):
        self.mode = mode
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)

    def try_take(self):
        if not self.buffer:
            return None
        if self.buffer[0] == 0x7B or self.mode == "text":
            idx = self.buffer.find(b"\n")
            if idx < 0:
                # possible frame that happens to start with '{' only if mode text
                if self.buffer[0] == 0x7B:
                    return None
                return None
            line = bytes(self.buffer[:idx])
            del self.buffer[:idx + 1]
            return self._decode(line)
        if len(self.buffer) < 4:
            return None
        length = HEADER.unpack_from(self.buffer, 0)[0]
        if length <= 0 or length > MAX_MESSAGE:
            raise BridgeError("E_PARSE", "invalid frame length %d" % length)
        if len(self.buffer) < 4 + length:
            return None
        body = bytes(self.buffer[4:4 + length])
        del self.buffer[:4 + length]
        return self._decode(body)

    def _decode(self, body):
        if body[:2] == GZIP_MAGIC:
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------
class BridgeClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, token=None,
                 timeout=DEFAULT_TIMEOUT, client_name="piterm-min",
                 compress=False):
        self.host = host
        self.port = int(port)
        self.token = token if token is not None else os.environ.get(
            "PITERM_QGIS_TOKEN", "")
        self.timeout = float(timeout)
        self.client_name = client_name
        self.compress = bool(compress)
        self.sock = None
        self.decoder = ResponseDecoder()
        self._counter = 0

    # -- connection -------------------------------------------------------
    def connect(self):
        if self.sock is not None:
            return self
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=CONNECT_TIMEOUT)
            self.sock.settimeout(self.timeout)
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        except OSError as exc:
            self.sock = None
            raise BridgeError(
                "E_BRIDGE_DOWN",
                "cannot reach the Piterm bridge at %s:%s (%s). Is QGIS open "
                "with the Piterm QGIS AI plugin's server started?"
                % (self.host, self.port, exc))
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- rpc --------------------------------------------------------------
    def call(self, command, params=None, timeout=None, meta=None):
        self.connect()
        self._counter += 1
        message = {
            "id": self._counter,
            "type": command,
            "params": params or {},
            "meta": {
                "client": self.client_name,
                "version": CLIENT_VERSION,
                "gzip": self.compress,
                **(meta or {}),
            },
        }
        if self.token:
            message["token"] = self.token
        payload = (encode_frame(message, compress=self.compress)
                   if not self.compress or True else encode_line(message))
        old_timeout = self.sock.gettimeout()
        if timeout:
            self.sock.settimeout(float(timeout))
        try:
            self.sock.sendall(payload)
            response = self._read_response()
        except socket.timeout:
            self.close()
            raise BridgeError("E_TIMEOUT", "bridge did not answer within %ss"
                              % (timeout or self.timeout))
        except OSError as exc:
            self.close()
            raise BridgeError("E_IO", "socket error: %s" % exc)
        finally:
            if self.sock is not None and old_timeout is not None:
                try:
                    self.sock.settimeout(old_timeout)
                except OSError:
                    pass
        if not isinstance(response, dict):
            raise BridgeError("E_PARSE", "unexpected response shape: %r"
                              % (response,))
        if not response.get("ok", False):
            error = response.get("error") or {}
            raise BridgeError(error.get("code", "E_UNKNOWN"),
                              error.get("message", "unknown bridge error"),
                              error.get("detail"))
        return response.get("result")

    def _read_response(self):
        deadline = time.monotonic() + (self.sock.gettimeout() or self.timeout)
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BridgeError("E_IO", "connection closed by the bridge")
            self.decoder.feed(chunk)
            response = self.decoder.try_take()
            if response is not None:
                return response
            if time.monotonic() > deadline:
                raise socket.timeout()

    # -- convenience ------------------------------------------------------
    def ping(self):
        return self.call("system.ping")

    def hello(self):
        return self.call("system.hello")

    def tools(self, live=True):
        if live:
            try:
                payload = self.call("system.capabilities")
                commands = payload.get("commands", [])
                if commands:
                    return commands
            except BridgeError:
                pass
        return load_manifest()

    def screenshot(self, path=None, **params):
        result = self.call("canvas.screenshot", params)
        if path and result.get("base64"):
            import base64
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(result["base64"]))
            result.pop("base64", None)
            result["path"] = os.path.abspath(path)
        return result


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_MANIFEST_PATHS = [
    os.path.join(_PACKAGE_DIR, "manifest.json"),
    os.path.join(os.path.dirname(_PACKAGE_DIR), "manifest.json"),
]

_BUILTIN_TOOLS = [
    {"name": "system.ping", "summary": "Connectivity check", "risk": "read",
     "params": [], "tags": ["system"]},
    {"name": "system.help", "summary": "Search commands", "risk": "read",
     "params": [{"name": "query", "required": False, "type": "str"}],
     "tags": ["system"]},
    {"name": "system.capabilities", "summary": "Full command catalog",
     "risk": "read", "params": [], "tags": ["system"]},
    {"name": "layers.list", "summary": "List layers", "risk": "read",
     "params": [{"name": "kind", "required": False, "type": "str"}],
     "tags": ["layers"]},
    {"name": "features.query", "summary": "Query features", "risk": "read",
     "params": [{"name": "layer", "required": True, "type": "str"},
                {"name": "limit", "required": False, "type": "int"}],
     "tags": ["features"]},
    {"name": "canvas.screenshot", "summary": "Canvas PNG", "risk": "read",
     "params": [{"name": "path", "required": False, "type": "str"}],
     "tags": ["canvas"]},
]


def load_manifest():
    for path in _MANIFEST_PATHS:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "commands" in data:
                return data["commands"]
            if isinstance(data, list):
                return data
        except OSError:
            continue
        except ValueError:
            continue
    return list(_BUILTIN_TOOLS)


# ---------------------------------------------------------------------------
# MCP plumbing
# ---------------------------------------------------------------------------
_JSON_TYPES = {
    "int": "integer", "float": "number", "bool": "boolean", "str": "string",
    "string": "string", "dict": "object", "list": "array", "tuple": "array",
    "list[str]": "array", "list[int]": "array",
}


def tool_name_for(command):
    return str(command).replace(".", "_")


def schema_for(command):
    properties = {}
    required = []
    for param in command.get("params") or []:
        name = param.get("name")
        if not name:
            continue
        ptype = _JSON_TYPES.get(str(param.get("type") or "str"), "string")
        entry = {"type": ptype}
        if param.get("doc"):
            entry["description"] = str(param["doc"])
        if "default" in param and param["default"] is not None:
            entry["default"] = param["default"]
        properties[name] = entry
        if param.get("required", True):
            required.append(name)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def mcp_tool_descriptor(command):
    name = tool_name_for(command.get("name", ""))
    description = command.get("summary") or command.get("description") or name
    risk = command.get("risk")
    if risk and risk != "read":
        description = "[%s] %s" % (risk, description)
    return {
        "name": name,
        "description": description,
        "inputSchema": schema_for(command),
    }


def content_for(result):
    """MCP content blocks for a bridge result (image-aware)."""
    if isinstance(result, dict) and result.get("base64") and result.get("format"):
        mime = "image/png" if str(result["format"]).lower() == "png" \
            else "image/%s" % result["format"]
        blocks = [{"type": "image", "data": result["base64"], "mimeType": mime}]
        summary = {k: v for k, v in result.items() if k != "base64"}
        blocks.append({"type": "text",
                       "text": _json_dump(summary)})
        return blocks
    return [{"type": "text", "text": _json_dump(result)}]


class MinimalMCPServer:
    """JSON-RPC 2.0 over stdio implementing the MCP subset agents use."""

    def __init__(self, client, tool_source="live"):
        self.client = client
        self.tool_source = tool_source
        self.tools = None
        self.by_tool_name = {}
        self.protocol_version = PROTOCOL_FALLBACK

    def _ensure_tools(self):
        if self.tools is not None:
            return
        commands = self.client.tools(live=(self.tool_source == "live"))
        self.tools = []
        for command in commands:
            descriptor = mcp_tool_descriptor(command)
            self.by_tool_name[descriptor["name"]] = command.get("name")
            self.tools.append(descriptor)

    # -- request handling -------------------------------------------------
    def handle(self, message):
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                self.protocol_version = params.get("protocolVersion") \
                    or PROTOCOL_FALLBACK
                return _ok(request_id, {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "piterm-qgis-min",
                                   "version": CLIENT_VERSION},
                })
            if method in ("notifications/initialized", "initialized"):
                return None
            if method == "ping":
                return _ok(request_id, {})
            if method == "tools/list":
                self._ensure_tools()
                return _ok(request_id, {"tools": self.tools})
            if method == "tools/call":
                return self._call_tool(request_id, params)
            if method in ("resources/list",):
                return _ok(request_id, {"resources": []})
            if method in ("prompts/list",):
                return _ok(request_id, {"prompts": []})
            if method == "logging/setLevel":
                return _ok(request_id, {})
            return _err(request_id, -32601, "method not found: %s" % method)
        except BridgeError as exc:
            return _ok(request_id, {
                "content": [{"type": "text", "text": exc.to_text()}],
                "isError": True,
            })
        except Exception as exc:  # noqa: BLE001
            return _err(request_id, -32603, "internal error: %s" % exc)

    def _call_tool(self, request_id, params):
        tool = params.get("name")
        arguments = params.get("arguments") or {}
        self._ensure_tools()
        if tool not in self.by_tool_name:
            if tool in self.by_tool_name.values():
                wire = tool
            else:
                return _ok(request_id, {
                    "content": [{"type": "text",
                                 "text": "unknown tool: %s" % tool}],
                    "isError": True,
                })
        else:
            wire = self.by_tool_name[tool]
        try:
            result = self.client.call(wire, arguments)
        except BridgeError as exc:
            return _ok(request_id, {
                "content": [{"type": "text", "text": exc.to_text()}],
                "isError": True,
            })
        return _ok(request_id, {"content": content_for(result),
                                "isError": False})

    # -- loop -------------------------------------------------------------
    def serve_stdio(self, stdin=None, stdout=None):
        stdin = stdin or io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
        stdout = stdout or io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            response = self.handle(message)
            if response is None:
                continue
            stdout.write(_json_dump(response) + "\n")
            stdout.flush()


def _ok(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _client_from_args(args):
    return BridgeClient(
        host=args.host, port=args.port, token=args.token,
        timeout=args.timeout, client_name=args.client_name,
        compress=args.compress,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="piterm-qgis",
        description="Piterm QGIS AI bridge client / MCP stdio server "
                    "(stdlib only)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--client-name", default="piterm-min")
    parser.add_argument("--compress", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="compact JSON output")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("ping", help="connectivity check")
    sub.add_parser("hello", help="handshake/capabilities")

    call_parser = sub.add_parser("call", help="call one bridge command")
    call_parser.add_argument("cmd", help="command name, e.g. layers.list")
    call_parser.add_argument("params", nargs="?", default="{}",
                             help="params as JSON (default {})")
    call_parser.add_argument("--file", dest="params_file",
                             help="read params JSON from a file")

    tools_parser = sub.add_parser("tools", help="list available tools")
    tools_parser.add_argument("--offline", action="store_true",
                              help="use the bundled manifest instead of the "
                                   "live bridge")

    shot_parser = sub.add_parser("screenshot", help="save a canvas PNG")
    shot_parser.add_argument("path", help="output file")
    shot_parser.add_argument("--max-size", type=int, default=2000)

    mcp_parser = sub.add_parser("mcp-stdio",
                                help="run as an MCP stdio server")
    mcp_parser.add_argument("--offline-tools", action="store_true",
                            help="never contact QGIS while listing tools")

    serve_parser = sub.add_parser("batch", help="run a JSON array of commands")
    serve_parser.add_argument("commands_file",
                              help="file containing [{type, params}, ...]")
    return parser


def cli(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp-stdio":
        client = _client_from_args(args)
        source = "manifest" if args.offline_tools else "live"
        server = MinimalMCPServer(client, tool_source=source)
        server.serve_stdio()
        return 0
    client = _client_from_args(args)
    try:
        if args.command in (None, "ping"):
            result = client.ping()
        elif args.command == "hello":
            result = client.hello()
        elif args.command == "tools":
            commands = client.tools(live=not args.offline)
            result = {"count": len(commands),
                      "tools": [tool_name_for(c.get("name"))
                                for c in commands]}
        elif args.command == "call":
            params = {}
            if args.params_file:
                with open(args.params_file, "r", encoding="utf-8") as fh:
                    params = json.load(fh)
            elif args.params:
                params = json.loads(args.params)
            result = client.call(args.cmd, params,
                                 timeout=max(args.timeout, 60.0))
        elif args.command == "screenshot":
            result = client.screenshot(args.path, max_size=args.max_size)
        elif args.command == "batch":
            with open(args.commands_file, "r", encoding="utf-8") as fh:
                commands = json.load(fh)
            result = client.call("system.batch", {"commands": commands,
                                                  "stop_on_error": False})
        else:
            parser.error("unknown command %r" % args.command)
            return 2
    except BridgeError as exc:
        sys.stderr.write(exc.to_text() + "\n")
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    finally:
        client.close()
    if args.json:
        sys.stdout.write(_json_dump(result) + "\n")
    else:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
