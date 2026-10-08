"""Client setup dialog: ready-made MCP configs for every popular client.

Snippets are generated from the live bridge settings (host/ports/token) and the
repo location, so a user can paste them into Claude Desktop, Cursor, VS Code,
Gemini/Qwen/Kimi CLI, Copilot CLI, LM Studio, opencode, Codex, Windsurf, Zed,
Hermes or give them to piterm's terminal agents.
"""

from __future__ import annotations

import json
import os
import time

from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                                 QPlainTextEdit, QPushButton, QVBoxLayout)

from ..constants import (DEFAULT_FRAME_PORT, DEFAULT_HTTP_PORT, PLUGIN_ID,
                         SETTINGS_PREFIX)


def _repo_root():
    try:
        return os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
    except Exception:  # noqa: BLE001
        return ""


def _paths():
    root = _repo_root()
    return {
        "repo": root,
        "min_script": os.path.join(root, "mcp", "piterm_qgis_mcp", "min.py"),
        "server_module": "piterm_qgis_mcp",
    }


def bridge_settings():
    settings = QgsSettings()
    return {
        "host": str(settings.value("%s/mode.host" % SETTINGS_PREFIX,
                                   "127.0.0.1") or "127.0.0.1"),
        "frame_port": int(settings.value("%s/mode.frame_port" % SETTINGS_PREFIX,
                                         DEFAULT_FRAME_PORT, type=int)),
        "http_port": int(settings.value("%s/mode.http_port" % SETTINGS_PREFIX,
                                        DEFAULT_HTTP_PORT, type=int)),
        "http_enabled": bool(settings.value("%s/mode.http_enabled" % SETTINGS_PREFIX,
                                            True, type=bool)),
        "token": str(settings.value("%s/auth.token" % SETTINGS_PREFIX, "") or ""),
        "auth_enabled": bool(settings.value("%s/auth.enabled" % SETTINGS_PREFIX,
                                            False, type=bool)),
    }


def build_snippets():
    bridge = bridge_settings()
    paths = _paths()
    env = {}
    if bridge["auth_enabled"] and bridge["token"]:
        env["PITERM_QGIS_TOKEN"] = bridge["token"]
    if bridge["host"] != "127.0.0.1":
        env["PITERM_QGIS_HOST"] = bridge["host"]
    if bridge["frame_port"] != DEFAULT_FRAME_PORT:
        env["PITERM_QGIS_PORT"] = str(bridge["frame_port"])

    def minimal_block(command_key="command"):
        payload = {
            "command": "python",
            "args": [paths["min_script"], "--mcp-stdio"],
        }
        if env:
            payload["env"] = env
        return payload

    def full_block():
        payload = {
            "command": "uv",
            "args": ["--directory", paths["repo"], "run", "--no-sync",
                     "python", "-m", paths["server_module"]],
        }
        if env:
            payload["env"] = env
        return payload

    def json_mcp(block):
        return json.dumps({"mcpServers": {"piterm-qgis": block}},
                          indent=2, ensure_ascii=False)

    codex_toml = "\n".join([
        "[mcp_servers.piterm-qgis]",
        'command = "python"',
        'args = [%s, "--mcp-stdio"]' % json.dumps(paths["min_script"]),
    ] + (["", "[mcp_servers.piterm-qgis.env]"] +
         ['%s = %s' % (k, json.dumps(v)) for k, v in env.items()]
         if env else []))

    hermes_bat = "\n".join([
        "@echo off",
        "REM Piterm QGIS AI launcher (clears Hermes venv contamination)",
        "set VIRTUAL_ENV=",
        "set PYTHONPATH=",
        "set PYTHONHOME=",
        "python \"%s\" --mcp-stdio" % paths["min_script"],
    ])

    hermes_yaml = "\n".join([
        "mcpServers:",
        "  piterm-qgis:",
        "    command: \"C:\\\\path\\\\to\\\\piterm-qgis-launch.bat\"",
        "    args: []",
    ])

    piterm_md = "\n".join([
        "# piterm terminal integration",
        "",
        "The bridge exposes the same three surfaces piterm already speaks:",
        "",
        "1. **MCP stdio** (full or minimal):",
        "   `python \"%s\" --mcp-stdio`" % paths["min_script"],
        "2. **HTTP JSON-RPC** (when enabled):",
        "   `POST http://%s:%s/rpc` with `{\"type\": \"system.ping\"}`" %
        (bridge["host"], bridge["http_port"]),
        "3. **Raw TCP frames/NDJSON**:",
        "   `%s:%s`, one JSON object per line works from any shell:" %
        (bridge["host"], bridge["frame_port"]),
        "   `echo '{\"type\":\"system.ping\"}' | ncat %s %s`" %
        (bridge["host"], bridge["frame_port"]),
        "",
        "Health check: `GET http://%s:%s/health`" %
        (bridge["host"], bridge["http_port"]),
    ])

    direct_md = "\n".join([
        "# No-MCP usage (skill / shell)",
        "",
        "Local bridge: %s:%s (TCP, NDJSON or framed)" % (bridge["host"],
                                                         bridge["frame_port"]),
        "",
        "python \"%s\" call layers.list '{}'" % paths["min_script"],
        "python \"%s\" ping" % paths["min_script"],
        "python \"%s\" screenshot out.png" % paths["min_script"],
    ])

    return {
        "claude_desktop": ("Claude Desktop (JSON)", json_mcp(full_block()),
                           os.path.join(os.environ.get("APPDATA", ""),
                                        "Claude", "claude_desktop_config.json")),
        "cursor": ("Cursor (JSON)",
                   json_mcp(minimal_block()),
                   os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json")),
        "vscode": ("VS Code (project mcp.json)", json_mcp(minimal_block()), ""),
        "gemini": ("Gemini CLI (JSON)",
                   json_mcp(minimal_block()),
                   os.path.join(os.path.expanduser("~"), ".gemini", "settings.json")),
        "qwen": ("Qwen Code (JSON)",
                 json_mcp(minimal_block()),
                 os.path.join(os.path.expanduser("~"), ".qwen", "settings.json")),
        "kimi": ("Kimi Code CLI (JSON)",
                 json_mcp(minimal_block()),
                 os.path.join(os.path.expanduser("~"), ".kimi-code", "mcp.json")),
        "copilot": ("GitHub Copilot CLI (JSON)",
                    json_mcp(minimal_block()),
                    os.path.join(os.path.expanduser("~"), ".copilot",
                                 "mcp-config.json")),
        "lmstudio": ("LM Studio (JSON)",
                     json_mcp(minimal_block()),
                     os.path.join(os.path.expanduser("~"), ".lmstudio", "mcp.json")),
        "opencode": ("opencode (JSON)", json.dumps({
            "mcp": {
                "piterm-qgis": {
                    "type": "local",
                    "command": minimal_block().get("command"),
                    "args": minimal_block().get("args"),
                    "enabled": True,
                }
            }
        }, indent=2, ensure_ascii=False),
            os.path.join(os.environ.get("APPDATA", ""), "opencode", "config.json")),
        "windsurf": ("Windsurf (JSON)", json_mcp(minimal_block()),
                     os.path.join(os.path.expanduser("~"), ".codeium", "windsurf",
                                  "mcp_config.json")),
        "codex": ("Codex CLI (TOML)", codex_toml,
                  os.path.join(os.path.expanduser("~"), ".codex", "config.toml")),
        "hermes": ("Hermes desktop (bat + YAML)", hermes_bat + "\n\n# config.yaml:\n"
                   + hermes_yaml, ""),
        "claude_code": ("Claude Code (command)", (
            "claude mcp add -s user piterm-qgis -- python \"%s\" --mcp-stdio"
            % paths["min_script"]), ""),
        "piterm": ("piterm terminal", piterm_md, ""),
        "direct": ("Direct / skill usage", direct_md, ""),
    }


class ConfiguratorDialog(QDialog):

    def __init__(self, parent, plugin):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle("Piterm QGIS AI · Client setup")
        self.resize(760, 560)
        self.snippets = build_snippets()
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Client"))
        self.client_combo = QComboBox()
        for key, (label, _content, _path) in self.snippets.items():
            self.client_combo.addItem(label, key)
        self.client_combo.currentIndexChanged.connect(self._refresh)
        row.addWidget(self.client_combo, 1)
        layout.addLayout(row)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        layout.addWidget(self.view, 1)
        buttons = QHBoxLayout()
        copy_button = QPushButton("Copy")
        copy_button.clicked.connect(self._copy)
        self.save_button = QPushButton("Save to file")
        self.save_button.clicked.connect(self._save)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(copy_button)
        buttons.addWidget(self.save_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self._refresh()

    def _current(self):
        key = self.client_combo.currentData()
        return self.snippets.get(key)

    def _refresh(self):
        current = self._current()
        if not current:
            return
        label, content, path = current
        self.view.setPlainText(content)
        self.save_button.setEnabled(bool(path))
        self.save_button.setToolTip(path or "no known config path for this client")

    def _copy(self):
        from qgis.PyQt.QtWidgets import QApplication
        current = self._current()
        if current:
            QApplication.clipboard().setText(current[1])

    def _save(self):
        current = self._current()
        if not current:
            return
        _label, content, path = current
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    existing = fh.read()
                with open(backup, "w", encoding="utf-8") as fh:
                    fh.write(existing)
                if path.endswith(".json"):
                    merged = _merge_json(existing, content)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(merged)
                    from .. import compat
                    compat.push_message("Piterm config merged into %s (backup: %s)"
                                        % (path, os.path.basename(backup)), "info", 3)
                    return
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            from .. import compat
            compat.push_message("Piterm config written to %s" % path, "info", 3)
        except Exception as exc:  # noqa: BLE001
            from .. import compat
            compat.push_message("Could not write config: %s" % exc, "warning", 4)


def _merge_json(existing_text, snippet_text):
    try:
        existing = json.loads(existing_text)
        snippet = json.loads(snippet_text)
    except ValueError:
        return snippet_text
    if not isinstance(existing, dict) or not isinstance(snippet, dict):
        return snippet_text
    for key, value in snippet.items():
        if isinstance(value, dict) and isinstance(existing.get(key), dict):
            existing[key].update(value)
        else:
            existing[key] = value
    return json.dumps(existing, indent=2, ensure_ascii=False)
