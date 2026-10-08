"""Single source of truth for names, defaults, ports, limits and risk levels.

Stdlib-only (no qgis imports) so it can be unit-tested outside QGIS and used by
the bundled MCP server / minimal client for protocol mirroring.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
PLUGIN_ID = "piterm_qgis_ai"
PLUGIN_TITLE = "Piterm QGIS AI"
PLUGIN_VERSION = "1.0.0"
SERVER_NAME = "Piterm Bridge"
PROTOCOL_NAME = "piterm-bridge"
PROTOCOL_VERSION = 2

# ---------------------------------------------------------------------------
# Network defaults
# ---------------------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_FRAME_PORT = 9877
DEFAULT_HTTP_PORT = 9879
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1"})

# ---------------------------------------------------------------------------
# Wire limits
# ---------------------------------------------------------------------------
MAX_MESSAGE_SIZE = 64 * 1024 * 1024          # inbound frame / text line cap (64 MiB)
MAX_OUTBOUND_PENDING = 256 * 1024 * 1024     # per-connection unsent backlog cap
MAX_CLIENTS = 16
MAX_AUTH_FAILURES = 5                        # per connection, then dropped
GZIP_THRESHOLD = 128 * 1024                  # compress responses above this size
LISTEN_BACKLOG = 16

# ---------------------------------------------------------------------------
# Sessions / journals / cursors
# ---------------------------------------------------------------------------
JOURNAL_MAX = 5000
CHECKPOINT_MAX = 31
CURSOR_TTL_SECONDS = 900
CURSOR_MAX_OPEN = 32
CURSOR_DEFAULT_CHUNK = 500

# ---------------------------------------------------------------------------
# Risk levels and scopes
# ---------------------------------------------------------------------------
RISK_READ = "read"
RISK_WRITE = "write"
RISK_DESTRUCTIVE = "destructive"
RISK_EXECUTE = "execute"
RISK_LEVELS = (RISK_READ, RISK_WRITE, RISK_DESTRUCTIVE, RISK_EXECUTE)

# Scope required to run a command whose risk level is X.
RISK_TO_SCOPE = {
    RISK_READ: "read",
    RISK_WRITE: "write",
    RISK_DESTRUCTIVE: "destructive",
    RISK_EXECUTE: "execute",
}

ALL_SCOPES = ("read", "write", "destructive", "execute")

# ---------------------------------------------------------------------------
# Settings prefix (QgsSettings) and storage layout
# ---------------------------------------------------------------------------
SETTINGS_PREFIX = "piterm_qgis_ai"

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
HEARTBEAT_INTERVAL_MS = 5000
FLUSH_INTERVAL_MS = 40
DEFAULT_SYNC_TIMEOUT = 55.0     # plugin-side budget for blocking handlers
DEFAULT_EXEC_TIMEOUT = 30.0     # python.exec deadline
DEFAULT_RENDER_DPI = 96


def all_ports_default() -> dict:
    """Default listener configuration shared by plugin and docs."""
    return {"frame": DEFAULT_FRAME_PORT, "http": DEFAULT_HTTP_PORT}
