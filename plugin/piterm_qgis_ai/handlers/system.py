"""System commands: connectivity, discovery, diagnostics, config, metrics,
clients, audit, events, batching and shutdown."""

from __future__ import annotations

import os
import platform
import sys
import time

from qgis.core import Qgis, QgsApplication, QgsProject

from .. import compat
from ..constants import (PLUGIN_ID, PLUGIN_VERSION, PROTOCOL_NAME, PROTOCOL_VERSION,
                         SETTINGS_PREFIX)
from ..errors import bad_request, invalid, not_found
from ..registry import catalog, iter_specs
from .base import HandlerBase
from ..registry import command


class SystemHandlers(HandlerBase):

    # ------------------------------------------------------------------
    # Connectivity / discovery
    # ------------------------------------------------------------------
    @command(name="system.ping", summary="Connectivity check", risk="read",
             tags=("system",), returns="{pong: true}")
    def system_ping(self, echo=None):
        out = {"pong": True, "time": time.time(), "plugin": PLUGIN_VERSION}
        if echo is not None:
            out["echo"] = echo
        return out

    @command(name="system.hello", summary="Handshake: protocol, capabilities, "
             "command list, security posture", risk="read", tags=("system",))
    def system_hello(self, echo=None):
        payload = self.srv.dispatcher.hello_payload(
            self.srv.dispatcher.current_ctx)
        payload["pong"] = True
        if echo is not None:
            payload["echo"] = echo
        return payload

    @command(name="system.capabilities",
             summary="Full command catalog with parameters (for agents)", risk="read",
             tags=("system",))
    def system_capabilities(self, tag=None, risk=None):
        items = catalog(with_params=True)
        if tag:
            items = [i for i in items if tag in i.get("tags", [])]
        if risk:
            items = [i for i in items if i.get("risk") == risk]
        return {"count": len(items), "commands": items}

    @command(name="system.help",
             summary="Search commands by name/summary; with_params adds signatures",
             risk="read", tags=("system",))
    def system_help(self, query=None, tag=None, risk=None, with_params=False,
                    limit=120):
        import difflib
        items = iter_specs()
        if tag:
            items = [s for s in items if tag in s.tags]
        if risk:
            items = [s for s in items if s.risk == risk]
        if query:
            q = str(query).casefold()
            hits = [s for s in items
                    if q in s.name.casefold() or q in s.summary.casefold()]
            if not hits:
                names = difflib.get_close_matches(str(query), [s.name for s in items],
                                                  n=8, cutoff=0.45)
                hits = [s for s in items if s.name in names]
            items = hits
        limit = max(1, min(int(limit or 120), 400))
        return {
            "count": len(items),
            "commands": [s.to_dict(with_params=bool(with_params)) for s in items[:limit]],
        }

    @command(name="system.info", summary="QGIS / runtime installation info",
             risk="read", tags=("system",))
    def system_info(self):
        info = {
            "qgis_version": compat.qgis_version_string(),
            "qgis_version_int": compat.version_int(),
            "is_qgis4": compat.is_qgis4(),
            "qt_version": compat.qt_version_string(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "prefix_path": _safe(lambda: QgsApplication.prefixPath()),
            "profile_path": _safe(lambda: QgsApplication.qgisSettingsDirPath()),
            "plugin_dir": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "locale": _safe(lambda: Qgis.locale()),
        }
        return info

    @command(name="system.diagnose",
             summary="Full-stack health check: plugin/server/QGIS, policy, jobs",
             risk="read", tags=("system",))
    def system_diagnose(self):
        from ..registry import command_count
        server = self.srv.status()
        security = self.srv.security.config.to_dict()
        checks = []
        if not server.get("running"):
            checks.append({"level": "error", "text": "bridge server is not running"})
        if security.get("auth_enabled") and not security.get("token_set"):
            checks.append({"level": "warning",
                           "text": "auth enabled but token is empty (treated as disabled)"})
        if not security.get("auth_enabled"):
            checks.append({"level": "info",
                           "text": "no token; loopback-only binding is enforced"})
        if security.get("read_only"):
            checks.append({"level": "info", "text": "read-only mode is active"})
        if not security.get("allow_execute"):
            checks.append({"level": "info",
                           "text": "code execution disabled (mode.allow_execute=false)"})
        try:
            layer_count = len(QgsProject.instance().mapLayers())
        except Exception:  # noqa: BLE001
            layer_count = None
        return {
            "ok": not any(c["level"] == "error" for c in checks),
            "plugin": {"id": PLUGIN_ID, "version": PLUGIN_VERSION},
            "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            "qgis": {"version": compat.qgis_version_string(),
                     "qt": compat.qt_version_string(),
                     "python": sys.version.split()[0]},
            "server": server,
            "security": security,
            "commands": command_count(),
            "project_layers": layer_count,
            "jobs": {"count": len(self.srv.jobs.list(limit=1000))},
            "journal": {"entries": len(self.srv.journal.entries)},
            "checkpoints": {"count": len(self.srv.checkpoints.list())},
            "checks": checks,
        }

    # ------------------------------------------------------------------
    # Configuration & metrics
    # ------------------------------------------------------------------
    @command(name="system.config_get", summary="Read bridge configuration",
             risk="read", tags=("system",))
    def system_config_get(self):
        from ..security import DEFAULTS, coerce_value
        provider = self.srv.security.provider
        values = {}
        for key, default in DEFAULTS.items():
            value = coerce_value(key, provider.get(key, default))
            if key == "auth.token":
                values[key] = "<set>" if value else ""
            else:
                values[key] = value
        return {"settings_prefix": SETTINGS_PREFIX, "config": values,
                "security": self.srv.security.config.to_dict()}

    @command(name="system.config_set",
             summary="Update a bridge setting (auth, ports, policy, paths)",
             risk="write", tags=("system",))
    def system_config_set(self, key, value):
        from ..security import DEFAULTS, _FALSE_WORDS, _TRUE_WORDS, coerce_value
        if key not in DEFAULTS:
            raise not_found("unknown setting %r" % key,
                            detail={"known": sorted(DEFAULTS.keys())})
        default = DEFAULTS[key]
        if isinstance(default, bool) and isinstance(value, str) \
                and value.strip().lower() not in (_TRUE_WORDS | _FALSE_WORDS):
            raise bad_request("setting %s expects a boolean" % key)
        if key in ("mode.frame_port", "mode.http_port"):
            try:
                port = None if value in (None, "") else int(value)
            except (TypeError, ValueError):
                raise invalid("port must be an integer within 1024..65535")
            if port is not None and not (1024 <= port <= 65535):
                raise invalid("port must be within 1024..65535")
            parsed = port
        else:
            parsed = coerce_value(key, value)
        self.srv.security.provider.set(key, parsed)
        self.srv.security.refresh()
        return {"key": key, "value": "<set>" if key == "auth.token" else parsed,
                "security": self.srv.security.config.to_dict()}

    @command(name="system.client_setup",
             summary="Ready-to-paste MCP client configs and the resolved client "
                     "entry point (ask the bridge how to connect to itself)",
             risk="read", tags=("system", "mcp"), returns="{clients: {...}}")
    def system_client_setup(self, client=None):
        from ..ui.configurator import _paths, build_snippets
        paths = _paths()
        snippets = build_snippets(paths)
        clients = {}
        for key, (label, content, target) in snippets.items():
            clients[key] = {"label": label, "content": content, "target": target}
        status = self.srv.status()
        security = self.srv.security.config
        payload = {
            "entry": {
                "min_script": paths.get("min_script"),
                "repo_root": paths.get("repo_root"),
                "source": paths.get("source"),
                "full_available": bool(paths.get("full_available")),
                "note": paths.get("note"),
            },
            "bridge": {
                "host": status.get("host"),
                "frame_port": status.get("frame_port"),
                "http_port": status.get("http_port"),
                "auth_enabled": bool(security.auth_enabled),
                "env": {"PITERM_QGIS_HOST": status.get("host"),
                        "PITERM_QGIS_PORT": status.get("frame_port")},
            },
            "clients": clients,
        }
        if client:
            chosen = clients.get(str(client))
            if chosen is None:
                raise not_found("unknown client %r" % client,
                                detail={"known": sorted(clients.keys())})
            payload["selected"] = dict(chosen, key=str(client))
        return payload

    @command(name="system.metrics",
             summary="Command counters, timings and per-command stats", risk="read",
             tags=("system",))
    def system_metrics(self):
        stats = dict(self.srv.dispatcher.stats)
        per = stats.pop("per_command", {})
        top = sorted(per.items(), key=lambda kv: kv[1].get("ms", 0), reverse=True)[:12]
        return {
            "totals": stats,
            "top_by_time": [{"cmd": k, **v} for k, v in top],
            "uptime_s": round(time.time() - self.srv.dispatcher.started, 1),
            "server": self.srv.status(),
        }

    @command(name="system.messages",
             summary="Tail of the QGIS message log captured by the bridge",
             risk="read", tags=("system",))
    def system_messages(self, limit=200, level=None):
        return {"messages": self.srv.messages(limit, level)}

    # ------------------------------------------------------------------
    # Clients / audit / events
    # ------------------------------------------------------------------
    @command(name="system.clients", summary="List connected clients", risk="read",
             tags=("system",))
    def system_clients(self):
        return self.srv.clients()

    @command(name="system.clients_kick", summary="Disconnect one client",
             risk="write", tags=("system",))
    def system_clients_kick(self, id):
        return self.srv.kick(id)

    @command(name="system.audit_tail", summary="Tail of the audit log", risk="read",
             tags=("system",))
    def system_audit_tail(self, limit=200):
        return {"entries": self.srv.security.audit.tail(limit)}

    @command(name="system.events_poll",
             summary="Poll push events (job progress, log lines) since a sequence",
             risk="read", tags=("system",))
    def system_events_poll(self, since=0, limit=200):
        return self.srv.dispatcher.poll_events(since, limit)

    # ------------------------------------------------------------------
    # Batch & shutdown
    # ------------------------------------------------------------------
    @command(name="system.batch",
             summary="Run several commands in one round-trip (per-item results)",
             risk="read", tags=("system",))
    def system_batch(self, commands, stop_on_error=False):
        if not isinstance(commands, list) or not commands:
            raise bad_request("'commands' must be a non-empty list")
        blocked = {"system.batch", "system.shutdown"}
        dispatcher = self.srv.dispatcher
        ctx = dispatcher.current_ctx
        results = []
        for index, item in enumerate(commands):
            if not isinstance(item, dict):
                results.append({"index": index, "ok": False,
                                "error": {"code": "E_BAD_REQUEST",
                                          "message": "each item must be an object"}})
                if stop_on_error:
                    break
                continue
            ctype = item.get("type") or item.get("command")
            if not ctype:
                results.append({"index": index, "ok": False,
                                "error": {"code": "E_BAD_REQUEST",
                                          "message": "item without 'type'"}})
                if stop_on_error:
                    break
                continue
            if ctype in blocked:
                results.append({"index": index, "type": ctype, "ok": False,
                                "error": {"code": "E_FORBIDDEN",
                                          "message": "%s cannot run inside a batch" % ctype}})
                if stop_on_error:
                    break
                continue
            response = dispatcher.execute_single(ctx, ctype,
                                                 item.get("params") or {},
                                                 item.get("id"))
            entry = {"index": index, "type": ctype, "ok": bool(response.get("ok"))}
            if response.get("ok"):
                entry["result"] = response.get("result")
            else:
                entry["error"] = response.get("error")
            results.append(entry)
            if not response.get("ok") and stop_on_error:
                break
        ok_count = sum(1 for r in results if r.get("ok"))
        return {"count": len(results), "ok_count": ok_count, "results": results}

    @command(name="system.shutdown",
             summary="Stop the bridge server (plugin stays loaded)", risk="write",
             tags=("system",))
    def system_shutdown(self):
        self.srv.log("shutdown requested over the wire", "warning")
        deferred = False
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(250, self.srv.stop)  # let the reply go out first
            deferred = True
        except Exception:  # noqa: BLE001
            self.srv.stop()
        return {"stopped": True, "deferred": deferred,
                "hint": "restart from the Piterm dock or plugin menu"}


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None
