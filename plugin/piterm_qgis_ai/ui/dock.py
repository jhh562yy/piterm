"""The Piterm dock widget: status, settings, clients, log, console, metrics.

Everything runs in-process on the Qt event loop; the dock never blocks the
bridge (console calls go through the same dispatcher, policy checks included).
"""

from __future__ import annotations

import json

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDockWidget, QFormLayout,
                                 QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QPlainTextEdit,
                                 QPushButton, QSpinBox, QTabWidget, QVBoxLayout,
                                 QWidget)

from .. import compat
from ..constants import (DEFAULT_FRAME_PORT, DEFAULT_HTTP_PORT, PLUGIN_VERSION,
                         SETTINGS_PREFIX)
from ..dispatcher import ConnectionContext


class PitermDock(QDockWidget):

    def __init__(self, iface, plugin):
        super().__init__("Piterm QGIS AI", iface.mainWindow())
        self.iface = iface
        self.plugin = plugin
        self.server = plugin.server
        self.setObjectName("PitermQgisAiDock")
        self._build_ui()
        self._metrics_timer = QTimer(self)
        self._metrics_timer.setInterval(4000)
        self._metrics_timer.timeout.connect(self._refresh_metrics)
        self._metrics_timer.start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)

        status_row = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_label = QLabel("stopped")
        self.toggle_button = QPushButton("Start server")
        self.toggle_button.clicked.connect(self._toggle_server)
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.toggle_button)
        layout.addLayout(status_row)

        settings_box = QGroupBox("Settings")
        form = QFormLayout(settings_box)
        self.host_edit = QLineEdit()
        self.frame_port_spin = QSpinBox()
        self.frame_port_spin.setRange(1024, 65535)
        self.frame_port_spin.setValue(DEFAULT_FRAME_PORT)
        self.http_port_spin = QSpinBox()
        self.http_port_spin.setRange(1024, 65535)
        self.http_port_spin.setValue(DEFAULT_HTTP_PORT)
        self.http_enabled_check = QCheckBox("Serve HTTP JSON-RPC")
        self.autostart_check = QCheckBox("Auto-start with QGIS")
        self.readonly_check = QCheckBox("Read-only mode (block writes)")
        self.execute_check = QCheckBox("Allow code execution (python.exec)")
        self.confirm_check = QCheckBox("Require __confirm for destructive ops")
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(_password_echo())
        self.token_edit.setPlaceholderText("shared secret (empty = disabled)")
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit, 1)
        generate_button = QPushButton("Generate")
        generate_button.clicked.connect(self._generate_token)
        token_row.addWidget(generate_button)
        self.auth_enabled_check = QCheckBox("Enforce token")
        token_box = QWidget()
        token_box.setLayout(token_row)
        for label, widget in (("Host", self.host_edit),
                              ("Frame port", self.frame_port_spin),
                              ("HTTP port", self.http_port_spin),
                              ("", self.http_enabled_check),
                              ("", self.autostart_check),
                              ("", self.readonly_check),
                              ("", self.execute_check),
                              ("", self.confirm_check),
                              ("Token", token_box),
                              ("", self.auth_enabled_check)):
            form.addRow(label, widget)
        apply_row = QHBoxLayout()
        apply_button = QPushButton("Apply")
        apply_button.clicked.connect(self._apply_settings)
        copy_button = QPushButton("Client setup…")
        copy_button.clicked.connect(lambda: self.plugin._show_configurator())
        apply_row.addWidget(apply_button)
        apply_row.addWidget(copy_button)
        apply_row.addStretch(1)
        wrapper = QWidget()
        wrapper.setLayout(apply_row)
        form.addRow("", wrapper)
        layout.addWidget(settings_box)

        self.tabs = QTabWidget()
        self.clients_list = QListWidget()
        kick_button = QPushButton("Disconnect selected")
        kick_button.clicked.connect(self._kick_selected)
        clients_page = QWidget()
        clients_layout = QVBoxLayout(clients_page)
        clients_layout.addWidget(self.clients_list, 1)
        clients_layout.addWidget(kick_button)
        self.tabs.addTab(clients_page, "Clients")

        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_filter_row = QHBoxLayout()
        self.log_filter = QComboBox()
        self.log_filter.addItems(["all", "info", "warning", "critical"])
        self.log_filter.currentTextChanged.connect(lambda _: self._reload_log())
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._clear_log)
        log_filter_row.addWidget(QLabel("Level"))
        log_filter_row.addWidget(self.log_filter, 1)
        log_filter_row.addWidget(clear_button)
        log_layout.addLayout(log_filter_row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        log_layout.addWidget(self.log_view, 1)
        self.tabs.addTab(log_page, "Log")

        console_page = QWidget()
        console_layout = QVBoxLayout(console_page)
        self.console_command = QLineEdit()
        self.console_command.setPlaceholderText("command, e.g. layers.list")
        _attach_completer(self.console_command)
        self.console_params = QPlainTextEdit()
        self.console_params.setPlaceholderText('params JSON, e.g. {"kind": "vector"}')
        self.console_params.setMaximumBlockCount(4000)
        run_button = QPushButton("Run command")
        run_button.clicked.connect(self._run_console)
        self.console_output = QPlainTextEdit()
        self.console_output.setReadOnly(True)
        self.console_output.setMaximumBlockCount(2000)
        console_layout.addWidget(self.console_command)
        console_layout.addWidget(self.console_params, 1)
        console_layout.addWidget(run_button)
        console_layout.addWidget(self.console_output, 2)
        self.tabs.addTab(console_page, "Console")

        self.metrics_view = QPlainTextEdit()
        self.metrics_view.setReadOnly(True)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self._refresh_metrics)
        metrics_page = QWidget()
        metrics_layout = QVBoxLayout(metrics_page)
        metrics_layout.addWidget(self.metrics_view, 1)
        metrics_layout.addWidget(refresh_button)
        self.tabs.addTab(metrics_page, "Metrics")

        layout.addWidget(self.tabs, 1)
        self.footer = QLabel("v%s" % PLUGIN_VERSION)
        layout.addWidget(self.footer)
        self.setWidget(root)
        self.setMinimumWidth(360)

    # ------------------------------------------------------------------
    # refresh
    # ------------------------------------------------------------------
    def refresh_all(self):
        settings = QgsSettings()
        prefix = SETTINGS_PREFIX
        self.host_edit.setText(str(settings.value("%s/mode.host" % prefix,
                                                  "127.0.0.1")))
        self.frame_port_spin.setValue(int(settings.value(
            "%s/mode.frame_port" % prefix, DEFAULT_FRAME_PORT, type=int)))
        self.http_port_spin.setValue(int(settings.value(
            "%s/mode.http_port" % prefix, DEFAULT_HTTP_PORT, type=int)))
        self.http_enabled_check.setChecked(bool(settings.value(
            "%s/mode.http_enabled" % prefix, True, type=bool)))
        self.autostart_check.setChecked(bool(settings.value(
            "%s/mode.autostart" % prefix, False, type=bool)))
        self.readonly_check.setChecked(bool(settings.value(
            "%s/mode.read_only" % prefix, False, type=bool)))
        self.execute_check.setChecked(bool(settings.value(
            "%s/mode.allow_execute" % prefix, False, type=bool)))
        self.confirm_check.setChecked(bool(settings.value(
            "%s/mode.confirm_destructive" % prefix, False, type=bool)))
        self.auth_enabled_check.setChecked(bool(settings.value(
            "%s/auth.enabled" % prefix, False, type=bool)))
        token = str(settings.value("%s/auth.token" % prefix, "") or "")
        if token and not self.token_edit.text():
            self.token_edit.setText(token)
        self._update_status()
        self._refresh_clients()
        self._refresh_metrics()
        self._reload_log()

    def _update_status(self):
        status = self.server.status()
        running = bool(status.get("running"))
        self.status_dot.setStyleSheet("color: %s;" % ("#16a34a" if running
                                                      else "#dc2626"))
        if running:
            ports = "frame %s" % status.get("frame_port")
            if status.get("http_port"):
                ports += " / http %s" % status.get("http_port")
            self.status_label.setText("running · %s · %d client(s)"
                                      % (ports, status.get("clients", 0)))
            self.toggle_button.setText("Stop server")
        else:
            errors = status.get("errors") or {}
            suffix = (" · " + "; ".join(errors.values())) if errors else ""
            self.status_label.setText("stopped%s" % suffix)
            self.toggle_button.setText("Start server")

    def _refresh_clients(self):
        try:
            payload = self.server.clients()
        except Exception:  # noqa: BLE001
            payload = {"clients": []}
        self.clients_list.clear()
        for client in payload.get("clients", []):
            label = "%s  %s  cmds=%s errs=%s  %ss" % (
                client.get("id"), client.get("client") or client.get("remote"),
                client.get("commands"), client.get("errors"),
                client.get("connected_s"))
            item = QListWidgetItem(label)
            item.setData(_user_role(), client.get("id"))
            self.clients_list.addItem(item)

    def _refresh_metrics(self):
        if not self.isVisible():
            return
        try:
            status = self.server.status()
            stats = dict(status.get("stats", {}))
            per = stats.pop("per_command", {})
            lines = ["uptime: %ss   commands: %s   errors: %s" % (
                status.get("uptime_s"), stats.get("commands"), stats.get("errors"))]
            lines.append("ports: frame=%s http=%s clients=%s" % (
                status.get("frame_port"), status.get("http_port"),
                status.get("clients")))
            jobs = self.server.jobs.list(limit=8)
            if jobs:
                lines.append("")
                lines.append("recent jobs:")
                for job in jobs:
                    lines.append("  %s %-10s %-9s %3s%% %s" % (
                        job["id"], job["kind"], job["state"],
                        job.get("progress"), job.get("title", "")[:40]))
            top = sorted(per.items(), key=lambda kv: kv[1].get("ms", 0),
                         reverse=True)[:10]
            if top:
                lines.append("")
                lines.append("slowest commands (total ms):")
                for name, entry in top:
                    lines.append("  %-32s n=%-5s ms=%-9.1f errs=%s" % (
                        name, entry.get("n"), entry.get("ms"),
                        entry.get("errors")))
            self.metrics_view.setPlainText("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self.metrics_view.setPlainText("metrics unavailable: %s" % exc)

    def _reload_log(self):
        level = self.log_filter.currentText()
        entries = self.server.messages(400, None if level == "all" else level)
        lines = []
        for entry in entries:
            import time as _time
            stamp = _time.strftime("%H:%M:%S", _time.localtime(entry["ts"]))
            lines.append("%s %-8s %s" % (stamp, entry["level"], entry["text"]))
        self.log_view.setPlainText("\n".join(lines[-400:]))

    # ------------------------------------------------------------------
    # events from the server
    # ------------------------------------------------------------------
    def handle_event(self, kind, payload):
        if kind == "clients":
            self._refresh_clients()
            self._update_status()
        elif kind == "server":
            self._update_status()
        elif kind == "log":
            import time as _time
            stamp = _time.strftime("%H:%M:%S", _time.localtime(payload.get("ts")))
            level = payload.get("level", "info")
            if self.log_filter.currentText() in ("all", level):
                self.log_view.appendPlainText("%s %-8s %s" % (
                    stamp, level, payload.get("text", "")))
        elif kind == "command":
            self.footer.setText("v%s · last: %s (%.1f ms)" % (
                PLUGIN_VERSION, payload.get("name"), payload.get("t_ms", 0)))

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _toggle_server(self):
        if self.server.running:
            self.plugin.stop_server()
        else:
            self.plugin.start_server()
        self._update_status()

    def _apply_settings(self):
        settings = QgsSettings()
        prefix = SETTINGS_PREFIX
        old = (str(settings.value("%s/mode.host" % prefix, "127.0.0.1")),
               int(settings.value("%s/mode.frame_port" % prefix,
                                  DEFAULT_FRAME_PORT, type=int)),
               int(settings.value("%s/mode.http_port" % prefix,
                                  DEFAULT_HTTP_PORT, type=int)))
        settings.setValue("%s/mode.host" % prefix, self.host_edit.text().strip()
                          or "127.0.0.1")
        settings.setValue("%s/mode.frame_port" % prefix,
                          self.frame_port_spin.value())
        settings.setValue("%s/mode.http_port" % prefix,
                          self.http_port_spin.value())
        settings.setValue("%s/mode.http_enabled" % prefix,
                          self.http_enabled_check.isChecked())
        settings.setValue("%s/mode.autostart" % prefix,
                          self.autostart_check.isChecked())
        settings.setValue("%s/mode.read_only" % prefix,
                          self.readonly_check.isChecked())
        settings.setValue("%s/mode.allow_execute" % prefix,
                          self.execute_check.isChecked())
        settings.setValue("%s/mode.confirm_destructive" % prefix,
                          self.confirm_check.isChecked())
        settings.setValue("%s/auth.enabled" % prefix,
                          self.auth_enabled_check.isChecked())
        settings.setValue("%s/auth.token" % prefix, self.token_edit.text())
        self.server.security.refresh()
        new = (settings.value("%s/mode.host" % prefix),
               settings.value("%s/mode.frame_port" % prefix, type=int),
               settings.value("%s/mode.http_port" % prefix, type=int))
        if self.server.running and old != new:
            self.plugin.stop_server()
            self.plugin.start_server()
        compat.push_message("Piterm settings applied", "info", 2)
        self._update_status()

    def _generate_token(self):
        import secrets
        self.token_edit.setText(secrets.token_urlsafe(32))
        self.auth_enabled_check.setChecked(True)

    def _kick_selected(self):
        item = self.clients_list.currentItem()
        if item is None:
            return
        conn_id = item.data(_user_role())
        if conn_id:
            self.server.kick(str(conn_id))
            self._refresh_clients()

    def _clear_log(self):
        self.server.message_log.items.clear()
        self.log_view.clear()

    def _run_console(self):
        command = self.console_command.text().strip()
        if not command:
            return
        try:
            params = json.loads(self.console_params.toPlainText() or "{}")
            if not isinstance(params, dict):
                raise ValueError("params must be a JSON object")
        except Exception as exc:  # noqa: BLE001
            self.console_output.setPlainText("params JSON error: %s" % exc)
            return
        token = str(QgsSettings().value("%s/auth.token" % SETTINGS_PREFIX, "") or "")
        message = {"type": command, "params": params, "id": "ui-console",
                   "meta": {"client": "qgis-dock-console", "version": PLUGIN_VERSION}}
        if token:
            message["token"] = token
        context = ConnectionContext("ui-console", "ui", remote="qgis-ui",
                                    write=lambda data: None,
                                    close=lambda: None)
        try:
            response = self.server.dispatcher.handle_message(context, message)
        except Exception as exc:  # noqa: BLE001
            self.console_output.setPlainText("dispatch error: %s" % exc)
            return
        self.console_output.setPlainText(json.dumps(response, ensure_ascii=False,
                                                    indent=2)[:20000])
        self._refresh_metrics()

    def closeEvent(self, event):
        # keep the dock alive (hiding is fine); plugin removes it on unload
        event.accept()


# ---------------------------------------------------------------------------
def _password_echo():
    return compat.resolve(QLineEdit, "EchoMode.Password", "Password")


def _user_role():
    return compat.resolve(Qt, "ItemDataRole.UserRole", "UserRole")


def _attach_completer(line_edit):
    try:
        from qgis.PyQt.QtWidgets import QCompleter
        from ..registry import iter_specs
        names = [spec.name for spec in iter_specs()]
        completer = QCompleter(names, line_edit)
        completer.setCaseSensitivity(compat.resolve(
            Qt, "CaseSensitivity.CaseInsensitive", "CaseInsensitive"))
        line_edit.setCompleter(completer)
    except Exception:  # noqa: BLE001
        pass
