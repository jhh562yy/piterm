"""Security policy: token auth, scopes, read-only mode, path allow-lists,
rate limiting and the audit trail.

Design goals ("high control"):

* every wire command carries a risk level (``read < write < destructive < execute``);
* the policy decides, per command and per parameter, whether it may run;
* destructive commands can require an explicit ``__confirm=true``;
* file-touching commands can only reach allow-listed roots (deny always wins);
* optional shared-secret token with constant-time comparison and lockout;
* optional per-connection rate limiting;
* every decision that denies or performs a mutation lands in the audit log.

The module is stdlib-only; QGIS settings are read through a tiny provider so
tests can inject a dict.
"""

from __future__ import annotations

import collections
import fnmatch
import hmac
import json
import os
import tempfile
import time

from .constants import (ALL_SCOPES, DEFAULT_HOST, MAX_AUTH_FAILURES, MAX_CLIENTS,
                        RISK_DESTRUCTIVE, RISK_READ, SETTINGS_PREFIX)
from .errors import (CommandError, ErrorCode, forbidden, path_denied)


# ---------------------------------------------------------------------------
# Settings providers
# ---------------------------------------------------------------------------
class DictSettings:
    """In-memory settings provider (tests, headless defaults)."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class QgisSettingsProvider:
    """Lazy QgsSettings adapter; importable without QGIS."""

    def __init__(self):
        self._settings = None

    def _s(self):
        if self._settings is None:
            from qgis.core import QgsSettings
            self._settings = QgsSettings()
        return self._settings

    def get(self, key, default=None):
        try:
            value = self._s().value("%s/%s" % (SETTINGS_PREFIX, key), default)
            return value
        except Exception:  # noqa: BLE001
            return default

    def set(self, key, value):
        try:
            self._s().setValue("%s/%s" % (SETTINGS_PREFIX, key), value)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULTS = {
    "auth.enabled": False,
    "auth.token": "",
    "mode.read_only": False,
    "mode.allow_execute": False,
    "mode.confirm_destructive": False,
    "mode.host": DEFAULT_HOST,
    "mode.frame_port": None,     # filled from constants by the plugin
    "mode.http_port": None,
    "mode.autostart": False,
    "dangerous.allow_full_python": False,
    "paths.allow": ["~", ":TEMP:", ":PROJECT:"],
    "paths.deny": [],
    "limits.max_clients": MAX_CLIENTS,
    "limits.rate_per_sec": 0.0,
    "audit.enabled": True,
    "audit.path": "",
    # Where the source checkout lives; used by the Client-setup dialog to build
    # MCP snippets. The installer records it, operators can set it by hand.
    "meta.repo_root": "",
}


class SecurityConfig:
    def __init__(self, provider):
        self.provider = provider
        self.reload()

    def reload(self):
        p = self.provider
        # Every read goes through ``coerce_value``: QgsSettings hands Python
        # booleans back as the *strings* "true"/"false", and ``bool("false")`` is
        # True — which silently disabled the ability to switch a policy off.
        cfg = {}
        for key, default in DEFAULTS.items():
            try:
                raw = p.get(key, default)
            except Exception:  # noqa: BLE001
                raw = default
            cfg[key] = coerce_value(key, raw)
        self.auth_enabled = cfg["auth.enabled"]
        self.token = cfg["auth.token"]
        self.read_only = cfg["mode.read_only"]
        self.allow_execute = cfg["mode.allow_execute"]
        self.confirm_destructive = cfg["mode.confirm_destructive"]
        self.allow_full_python = cfg["dangerous.allow_full_python"]
        self.paths_allow = cfg["paths.allow"]
        self.paths_deny = cfg["paths.deny"]
        self.max_clients = cfg["limits.max_clients"] or MAX_CLIENTS
        self.rate_per_sec = cfg["limits.rate_per_sec"]
        self.audit_enabled = cfg["audit.enabled"]
        self.audit_path = cfg["audit.path"]

    def to_dict(self, redact_token=True):
        d = {
            "auth_enabled": self.auth_enabled,
            "read_only": self.read_only,
            "allow_execute": self.allow_execute,
            "confirm_destructive": self.confirm_destructive,
            "allow_full_python": self.allow_full_python,
            "paths_allow": list(self.paths_allow),
            "paths_deny": list(self.paths_deny),
            "max_clients": self.max_clients,
            "rate_per_sec": self.rate_per_sec,
            "audit_enabled": self.audit_enabled,
        }
        if redact_token:
            d["token_set"] = bool(self.token)
        else:
            d["token_set"] = bool(self.token)
            d["token"] = self.token
        return d


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except ValueError:
            pass
    return [part.strip() for part in text.split(";") if part.strip()]


_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "y", "t", "enabled"})
_FALSE_WORDS = frozenset({"", "0", "false", "no", "off", "n", "f", "disabled", "none", "null"})


def _as_bool(value):
    """Parse a settings boolean without the ``bool('false') is True`` trap.

    QgsSettings round-trips Python ``False`` as the string ``"false"``; PowerShell
    ini edits and JSON payloads arrive as strings too. Anything unrecognised but
    non-empty counts as True (matching QgsSettings' ``"2"``-style values).
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return True


def coerce_value(key, value):
    """Coerce a raw settings value to the type declared in :data:`DEFAULTS`.

    Single source of truth for settings reads (``SecurityConfig.reload``,
    ``system.config_get``) and writes (``system.config_set``). Unknown keys are
    returned untouched so callers can stay generic.
    """
    if key not in DEFAULTS:
        return value
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return _as_bool(value)
    if isinstance(default, list):
        return _as_list(value)
    if isinstance(default, str):
        return "" if value is None else str(value)
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)
    if isinstance(default, int):
        if value is None or value == "":
            return int(default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)
    if default is None:  # mode.frame_port / mode.http_port: optional ports
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return value


# ---------------------------------------------------------------------------
# Rate limiter (token bucket)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_sec):
        self.per_sec = float(per_sec or 0)
        self.tokens = self.per_sec
        self.updated = time.monotonic()

    def allow(self, cost=1.0):
        if self.per_sec <= 0:
            return True
        now = time.monotonic()
        self.tokens = min(self.per_sec, self.tokens + (now - self.updated) * self.per_sec)
        self.updated = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
_REDACT_KEYS = {"token", "password", "passwd", "pwd", "secret", "api_key", "code"}


class AuditLog:
    def __init__(self, manager):
        self.manager = manager
        self.ring = collections.deque(maxlen=2000)
        self.path = None
        self._fh = None
        self._written = 0
        self.enabled = True

    def configure(self, enabled, path):
        self.enabled = bool(enabled)
        if path and path != self.path:
            self.close()
            self.path = path
        if self.enabled and self.path:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
            except Exception:  # noqa: BLE001
                self.enabled = False

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None

    def record(self, command, status, client=None, risk=None, duration_ms=None,
               params=None, error=None):
        if not self.enabled:
            return
        entry = {
            "ts": time.time(),
            "cmd": command,
            "status": status,
            "client": client or "",
            "risk": risk or "",
        }
        if duration_ms is not None:
            entry["t_ms"] = round(duration_ms, 2)
        if params:
            entry["params"] = _trim_params(params)
        if error:
            entry["error"] = str(error)[:400]
        self.ring.append(entry)
        if self.path:
            self._append(entry)

    def _append(self, entry):
        try:
            if self._fh is None:
                self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._fh.flush()
            self._written += 1
            if self._written % 500 == 0:  # crude rotation by size
                if self._fh.tell() > 8 * 1024 * 1024:
                    self._fh.close()
                    self._fh = None
                    os.replace(self.path, self.path + ".1")
        except Exception:  # noqa: BLE001
            pass

    def tail(self, limit=200):
        limit = max(1, min(int(limit), 2000))
        return list(self.ring)[-limit:]


def _trim_params(params, depth=0):
    if depth > 3:
        return "…"
    if isinstance(params, dict):
        out = {}
        for key, value in params.items():
            if str(key).lower() in _REDACT_KEYS:
                if isinstance(value, str):
                    out[key] = "<redacted %d chars>" % len(value)
                else:
                    out[key] = "<redacted>"
            else:
                out[key] = _trim_params(value, depth + 1)
        return out
    if isinstance(params, (list, tuple)):
        items = [_trim_params(v, depth + 1) for v in params[:20]]
        if len(params) > 20:
            items.append("…(%d more)" % (len(params) - 20))
        return items
    if isinstance(params, str) and len(params) > 300:
        return params[:300] + "…"
    return params


# ---------------------------------------------------------------------------
# The policy engine
# ---------------------------------------------------------------------------
class SecurityManager:
    def __init__(self, provider=None, project_provider=None):
        self.provider = provider or DictSettings()
        self.project_provider = project_provider  # callable -> project dir or None
        self.config = SecurityConfig(self.provider)
        self.audit = AuditLog(self)
        self.refresh()

    def refresh(self):
        self.config.reload()
        self.audit.configure(self.config.audit_enabled, self.config.audit_path or None)

    # -- token --------------------------------------------------------------
    def token_ok(self, presented):
        if not self.config.auth_enabled:
            return True
        if not self.config.token:
            return True  # enabled but empty: treated as disabled, surfaced in UI
        if not isinstance(presented, str):
            return False
        return hmac.compare_digest(presented.encode("utf-8"),
                                   self.config.token.encode("utf-8"))

    # -- scopes -------------------------------------------------------------
    def granted_scopes(self):
        scopes = set(ALL_SCOPES)
        if self.config.read_only:
            scopes = {"read"}
        if not self.config.allow_execute:
            scopes.discard("execute")
        return scopes

    # -- command gate -------------------------------------------------------
    def check_command(self, spec, params):
        """Raise CommandError when the command must not run."""
        scopes = self.granted_scopes()
        if spec.risk != RISK_READ and "write" not in scopes:
            raise forbidden(
                "read-only mode is enabled; %s (%s) is blocked" % (spec.name, spec.risk),
                detail={"setting": "mode.read_only"})
        if spec.risk == "execute" and "execute" not in scopes:
            raise forbidden(
                "code execution is disabled; enable 'mode.allow_execute' to use %s"
                % spec.name, detail={"setting": "mode.allow_execute"})
        if (spec.name in ("python.exec_full",)
                and not self.config.allow_full_python):
            raise forbidden(
                "full Python is disabled; enable 'dangerous.allow_full_python' "
                "deliberately before using %s" % spec.name,
                detail={"setting": "dangerous.allow_full_python"})
        if (self.config.confirm_destructive
                and spec.risk in (RISK_DESTRUCTIVE,)
                and not (isinstance(params, dict) and params.get("__confirm") is True)):
            raise CommandError(
                ErrorCode.CONFIRM_REQUIRED,
                "%s is destructive; repeat with __confirm=true to execute"
                % spec.name, detail={"risk": spec.risk})
        return True

    # -- path gate ----------------------------------------------------------
    def resolve_path(self, path):
        text = os.path.expandvars(os.path.expanduser(str(path)))
        return os.path.abspath(text)

    def check_path(self, path, for_write=False):
        resolved = self.resolve_path(path)
        lowered = resolved.replace("\\", "/").lower()
        for pattern in self.config.paths_deny:
            pat = self.resolve_path(pattern).replace("\\", "/").lower()
            if _path_matches(lowered, pat):
                raise path_denied("path is deny-listed: %s" % resolved,
                                  detail={"pattern": pattern})
        allows = self.config.paths_allow
        if not allows:
            return resolved
        for pattern in allows:
            if str(pattern).strip() == "*":
                return resolved
            expanded = self._expand_placeholder(pattern)
            pat = self.resolve_path(expanded).replace("\\", "/").lower()
            if _path_matches(lowered, pat):
                return resolved
        raise path_denied(
            "path is outside the allow-list: %s" % resolved,
            detail={"allow": list(self.config.paths_allow),
                    "hint": "extend 'paths.allow' in Piterm settings"})

    def _expand_placeholder(self, pattern):
        if pattern == ":TEMP:":
            return tempfile.gettempdir()
        if pattern == ":PROJECT:":
            if self.project_provider:
                try:
                    directory = self.project_provider()
                    if directory:
                        return directory
                except Exception:  # noqa: BLE001
                    pass
            return tempfile.gettempdir()
        if pattern == ":HOME:":
            return "~"
        return pattern


def _path_matches(lowered_path, lowered_pattern):
    pattern = lowered_pattern.rstrip("/")
    if not pattern.endswith("*"):
        pattern = pattern + "/*"
    try:
        if lowered_path.startswith(pattern[:-1]):
            return True
    except Exception:  # noqa: BLE001
        pass
    return fnmatch.fnmatch(lowered_path, pattern)
