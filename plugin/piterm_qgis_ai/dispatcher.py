"""Message dispatcher: auth -> policy -> validation -> handler -> response.

One place where every inbound message is decoded, authorized, timed, recorded
(journal + audit + metrics) and answered. The Qt server hands raw messages in
and gets response dicts out; handlers never see sockets.
"""

from __future__ import annotations

import collections
import difflib
import inspect
import itertools
import time
import traceback

from .constants import (MAX_MESSAGE_SIZE, PROTOCOL_NAME, PROTOCOL_VERSION,
                        RISK_READ)
from .errors import CommandError, ErrorCode

_RESERVED_PARAM_PREFIX = "_"


class ConnectionContext:
    """Per-connection state (one QGIS may serve many clients)."""

    def __init__(self, conn_id, kind="frame", remote="", write=None, close=None):
        self.id = conn_id
        self.kind = kind                    # "frame" | "http"
        self.remote = remote
        self.write = write                  # callable(bytes) -> bool
        self.close = close                  # callable()
        self.mode = "frame"                 # reply encoding: frame | text
        self.gzip_ok = False
        self.subscriptions = set()
        self.pending_events = collections.deque(maxlen=1000)
        self.client = ""
        self.client_version = ""
        self.auth_failures = 0
        self.connected = time.time()
        self.last_seen = self.connected
        self.commands = 0
        self.errors = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.rate = None                    # set by dispatcher (RateLimiter)
        self.closing = False

    def to_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "remote": self.remote,
            "client": self.client,
            "client_version": self.client_version,
            "connected_s": round(time.time() - self.connected, 1),
            "commands": self.commands,
            "errors": self.errors,
            "subscriptions": sorted(self.subscriptions),
        }


class Dispatcher:
    def __init__(self, hub, security, journal, jobs, cursors, plugin_version="0.0.0"):
        self.hub = hub
        self.security = security
        self.journal = journal
        self.jobs = jobs
        self.cursors = cursors
        self.plugin_version = plugin_version
        self.server = None                   # set by server after construction
        self.events = collections.deque(maxlen=2000)
        self.event_seq = itertools.count(1)
        self.started = time.time()
        self.stats = {
            "commands": 0,
            "errors": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "per_command": {},               # name -> {"n","ms","errors"}
        }
        self._signatures = {}
        self._members = None
        self.current_ctx = None              # connection currently being served

    # ------------------------------------------------------------------
    # Envelope handling
    # ------------------------------------------------------------------
    def handle_message(self, ctx, message):
        """Return a single response dict (never raises)."""
        t0 = time.perf_counter()
        self.current_ctx = ctx
        if not isinstance(message, dict):
            return self._error(None, ErrorCode.BAD_REQUEST,
                               "message must be a JSON object")

        self._absorb_meta(ctx, message.get("meta"))
        cmd = message.get("type") or message.get("command")
        msg_id = message.get("id")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(cmd, str) or not cmd:
            return self._error(msg_id, ErrorCode.BAD_REQUEST,
                               "missing 'type' (command name)")
        if not isinstance(params, dict):
            return self._error(msg_id, ErrorCode.BAD_REQUEST,
                               "'params' must be an object", cmd=cmd)

        # --- authentication -------------------------------------------
        if not self.security.token_ok(message.get("token")):
            ctx.auth_failures += 1
            from .constants import MAX_AUTH_FAILURES
            if ctx.auth_failures >= MAX_AUTH_FAILURES:
                ctx.closing = True
                return self._error(msg_id, ErrorCode.AUTH_LOCKED,
                                   "too many authentication failures; "
                                   "connection will be closed", cmd=cmd)
            return self._error(msg_id, ErrorCode.AUTH,
                               "authentication failed: missing or invalid token",
                               cmd=cmd)
        if ctx.auth_failures:
            ctx.auth_failures = 0

        # --- rate limit -------------------------------------------------
        if ctx.rate is not None and not ctx.rate.allow():
            return self._error(msg_id, ErrorCode.BUSY,
                               "rate limit exceeded (%.0f commands/second)"
                               % self.security.config.rate_per_sec, cmd=cmd)

        # --- dispatch ----------------------------------------------------
        response = self.execute_single(ctx, cmd, params, msg_id)
        response.setdefault("meta", {})["t_ms"] = round(
            (time.perf_counter() - t0) * 1000.0, 3)
        ctx.commands += 1
        if not response.get("ok"):
            ctx.errors += 1
        self.stats["commands"] += 1
        if not response.get("ok"):
            self.stats["errors"] += 1
        return response

    def execute_single(self, ctx, cmd, params, msg_id=None, batch_depth=0):
        """Authorize, validate, execute one command; always returns a dict."""
        from .registry import COMMANDS
        t0 = time.perf_counter()

        spec = COMMANDS.get(cmd)
        if spec is None:
            suggestions = difflib.get_close_matches(cmd, list(COMMANDS), n=5, cutoff=0.55)
            return self._error(msg_id, ErrorCode.UNKNOWN_COMMAND,
                               "unknown command: %s" % cmd,
                               detail={"suggestions": suggestions}, cmd=cmd)

        # policy gate
        try:
            self.security.check_command(spec, params)
        except CommandError as exc:
            return self._error(msg_id, exc.code, exc.message, exc.detail, cmd=cmd)

        # parameter validation
        try:
            clean = self._validate_params(spec, params)
        except CommandError as exc:
            return self._error(msg_id, exc.code, exc.message, exc.detail, cmd=cmd)

        # execute (drop protocol-level underscore flags such as __confirm)
        clean = {k: v for k, v in clean.items() if not str(k).startswith("_")}
        fn = getattr(self.hub, spec.func_name, None)
        if fn is None:
            return self._error(msg_id, ErrorCode.INTERNAL,
                               "handler missing for %s" % cmd, cmd=cmd)
        status = "ok"
        try:
            result = fn(**clean)
            if result is None:
                result = {}
            response = {"id": msg_id, "ok": True, "result": result}
        except CommandError as exc:
            status = "error"
            response = self._error(msg_id, exc.code, exc.message, exc.detail, cmd=cmd)
        except Exception as exc:  # noqa: BLE001 - convert to structured error
            status = "error"
            tb = traceback.format_exc()
            self._log_internal(cmd, exc, tb)
            response = self._error(msg_id, ErrorCode.INTERNAL,
                                   "%s: %s" % (type(exc).__name__, exc),
                                   detail={"traceback": tb[-4000:]}, cmd=cmd)

        duration_ms = (time.perf_counter() - t0) * 1000.0
        self._record(ctx, spec, params, status, duration_ms,
                     response.get("error", {}).get("message"))
        per = self.stats["per_command"].setdefault(
            cmd, {"n": 0, "ms": 0.0, "errors": 0})
        per["n"] += 1
        per["ms"] += duration_ms
        if status != "ok":
            per["errors"] += 1
        if response.get("ok"):
            response.setdefault("meta", {})["cmd"] = cmd
        return response

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _absorb_meta(self, ctx, meta):
        if not isinstance(meta, dict):
            return
        ctx.client = str(meta.get("client") or ctx.client)[:80]
        ctx.client_version = str(meta.get("version") or ctx.client_version)[:40]
        if meta.get("gzip") is not None:
            ctx.gzip_ok = bool(meta.get("gzip"))
        if meta.get("mode") in ("frame", "text"):
            ctx.mode = meta["mode"]
        subs = meta.get("subscribe")
        if subs is not None:
            if isinstance(subs, str):
                subs = [subs]
            if isinstance(subs, (list, tuple)):
                ctx.subscriptions = {str(s)[:40] for s in subs if str(s)}
        if meta.get("unsubscribe"):
            for s in meta["unsubscribe"]:
                ctx.subscriptions.discard(str(s))

    def _validate_params(self, spec, params):
        if not params:
            params = {}
        signature = self._signatures.get(spec.name)
        if signature is None:
            fn = getattr(self.hub, spec.func_name, None)
            try:
                signature = inspect.signature(fn)
            except (TypeError, ValueError):
                signature = inspect.Signature()
            self._signatures[spec.name] = signature
        allowed = {name for name in signature.parameters
                   if name not in ("self", "ctx")}
        clean = {}
        unknown = []
        for key, value in params.items():
            if key in allowed or str(key).startswith(_RESERVED_PARAM_PREFIX):
                clean[key] = value
            else:
                unknown.append(key)
        missing = [name for name, param in signature.parameters.items()
                   if name not in ("self", "ctx")
                   and name not in clean
                   and param.default is inspect.Parameter.empty
                   and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                      inspect.Parameter.KEYWORD_ONLY)]
        if unknown:
            raise CommandError(
                ErrorCode.BAD_REQUEST,
                "unknown parameter(s) for %s: %s" % (spec.name, ", ".join(sorted(unknown))),
                detail={"accepted": sorted(allowed),
                        "hint": "see 'help' for the exact signature"})
        if missing:
            raise CommandError(
                ErrorCode.BAD_REQUEST,
                "missing required parameter(s) for %s: %s" % (spec.name, ", ".join(missing)),
                detail={"accepted": sorted(allowed)})
        return clean

    def _record(self, ctx, spec, params, status, duration_ms, error=None):
        self.journal.record(spec.name, params, status, duration_ms,
                            client=ctx.client)
        if spec.name not in ("ping", "hello", "help", "events.poll"):
            self.security.audit.record(
                spec.name, status, client=ctx.client, risk=spec.risk,
                duration_ms=duration_ms, params=params, error=error)
        if self.server is not None:
            self.server.note_command(spec.name, duration_ms, status)

    def _error(self, msg_id, code, message, detail=None, cmd=None):
        error = {"code": code, "message": message}
        if detail is not None:
            error["detail"] = detail
        meta = {}
        if cmd:
            meta["cmd"] = cmd
        return {"id": msg_id, "ok": False, "error": error, "meta": meta}

    def _log_internal(self, cmd, exc, tb):
        try:
            from . import compat
            compat.log("command %s failed: %s\n%s" % (cmd, exc, tb[-1500:]),
                       "critical")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def emit_event(self, event, data=None):
        seq = next(self.event_seq)
        item = {"seq": seq, "event": event, "ts": time.time(),
                "data": data or {}}
        self.events.append(item)
        if self.server is None:
            return item
        for ctx in self.server.iter_contexts():
            prefix = event.split(".")[0]
            if ctx.subscriptions and (
                    event in ctx.subscriptions or prefix in ctx.subscriptions):
                ctx.pending_events.append(item)
        return item

    def poll_events(self, since=0, limit=200):
        since = int(since or 0)
        limit = max(1, min(int(limit or 200), 1000))
        items = [e for e in self.events if e["seq"] > since]
        return {"events": items[-limit:], "latest": next(self.event_seq) - 1}

    def drain_pending(self, ctx):
        out = list(ctx.pending_events)
        ctx.pending_events.clear()
        return out

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------
    def hello_payload(self, ctx):
        from .registry import iter_specs
        specs = iter_specs()
        security = self.security.config
        server_info = {}
        if self.server is not None:
            server_info = self.server.status()
        return {
            "protocol": PROTOCOL_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "plugin_version": self.plugin_version,
            "server": server_info,
            "security": {
                "auth_enabled": security.auth_enabled,
                "read_only": security.read_only,
                "allow_execute": security.allow_execute,
                "confirm_destructive": security.confirm_destructive,
                "scopes": sorted(self.security.granted_scopes()),
            },
            "limits": {
                "max_message": MAX_MESSAGE_SIZE,
                "max_clients": security.max_clients,
            },
            "commands": {
                "count": len(specs),
                "names": [s.name for s in specs],
            },
            "features": ["frame", "text", "gzip", "batch", "events",
                         "jobs", "cursors", "checkpoints", "journal",
                         "http", "workflow", "audit"],
            "transport": {
                "frame": True,
                "http": True,
            },
        }
