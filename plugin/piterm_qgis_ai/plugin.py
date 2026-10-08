"""Piterm QGIS AI - plugin entry point.

Creates the toolbar action, menu, dock widget and owns the bridge server
lifecycle. Also bridges the QGIS message log into the server ring buffer so
agents can read what QGIS reported without touching the GUI.
"""

from __future__ import annotations

import os

from qgis.core import QgsApplication, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from . import compat
from .checkpoints import Checkpoints
from .constants import (DEFAULT_FRAME_PORT, DEFAULT_HTTP_PORT, PLUGIN_ID,
                        PLUGIN_TITLE, PLUGIN_VERSION, SETTINGS_PREFIX)
from .cursors import CursorManager
from .handlers import build_hub
from .jobs import JobManager
from .journal import Journal
from .security import QgisSettingsProvider
from .server import PitermServer


class PitermQgisAiPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.hub = None
        self.server = None
        self.dock = None
        self.actions = []
        self._menu_name = PLUGIN_TITLE
        self.storage_dir = ""
        self._log_connected = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def initGui(self):
        self.storage_dir = os.path.join(QgsApplication.qgisSettingsDirPath(),
                                        PLUGIN_ID)
        for sub in ("checkpoints", "audit", "logs"):
            try:
                os.makedirs(os.path.join(self.storage_dir, sub), exist_ok=True)
            except Exception:  # noqa: BLE001
                pass
        self._build_server()
        self._build_actions()
        self._build_dock()
        self._connect_message_log()
        self._maybe_autostart()
        compat.log("Piterm QGIS AI %s loaded" % PLUGIN_VERSION, "info")

    def unload(self):
        try:
            if self.server is not None:
                self.server.stop()
        except Exception:  # noqa: BLE001
            pass
        if self.dock is not None:
            try:
                self.iface.removeDockWidget(self.dock)
            except Exception:  # noqa: BLE001
                pass
            self.dock.deleteLater()
            self.dock = None
        for action in self.actions:
            try:
                self.iface.removePluginMenu(self._menu_name, action)
                self.iface.removeToolBarIcon(action)
            except Exception:  # noqa: BLE001
                pass
        self.actions = []
        compat.log("Piterm QGIS AI unloaded", "info")

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_server(self):
        hub = build_hub(
            journal=Journal(),
            jobs=JobManager(),
            cursors=CursorManager(),
            checkpoints=Checkpoints(os.path.join(self.storage_dir, "checkpoints")),
        )
        provider = QgisSettingsProvider()
        self.server = PitermServer(hub, self.iface, settings_provider=provider,
                                   parent=None, plugin_version=PLUGIN_VERSION)
        hub.jobs.event_cb = lambda event, data: self.server.dispatcher.emit_event(
            event, data)
        self.hub = hub
        settings = QgsSettings()
        if not settings.value("%s/audit.path" % SETTINGS_PREFIX, None):
            settings.setValue("%s/audit.path" % SETTINGS_PREFIX,
                              os.path.join(self.storage_dir, "audit", "audit.jsonl"))
        self.server.security.refresh()

    def _build_actions(self):
        main_action = QAction(self._icon(), PLUGIN_TITLE, self.iface.mainWindow())
        main_action.setCheckable(True)
        main_action.setToolTip("Piterm QGIS AI - start/stop the AI bridge")
        main_action.triggered.connect(self._toggle_from_action)
        self.actions.append(main_action)
        self.main_action = main_action

        reload_action = QAction("Reload plugin", self.iface.mainWindow())
        reload_action.triggered.connect(self._reload_self)
        config_action = QAction("Client setup…", self.iface.mainWindow())
        config_action.triggered.connect(lambda: self._show_configurator())
        checkpoints_action = QAction("Open storage folder", self.iface.mainWindow())
        checkpoints_action.triggered.connect(self._open_storage)
        about_action = QAction("About Piterm QGIS AI", self.iface.mainWindow())
        about_action.triggered.connect(self._show_about)

        for action in (reload_action, config_action, checkpoints_action, about_action):
            self.actions.append(action)
            self.iface.addPluginToMenu(self._menu_name, action)
        self.iface.addPluginToMenu(self._menu_name, main_action)
        self.iface.addToolBarIcon(main_action)

    def _build_dock(self):
        from .ui.dock import PitermDock
        self.dock = PitermDock(self.iface, self)
        area = compat.resolve(Qt, "DockWidgetArea.RightDockWidgetArea",
                              "RightDockWidgetArea")
        self.iface.addDockWidget(area, self.dock)
        self.server.ui_callback = self.dock.handle_event
        self.dock.refresh_all()

    def _connect_message_log(self):
        try:
            log = QgsMessageLog.instance()
            signal = getattr(log, "messageReceivedWithFormat", None)
            if signal is not None:
                signal.connect(self._capture_log_full)
            else:
                log.messageReceived.connect(self._capture_log_legacy)
            self._log_connected = True
        except Exception:  # noqa: BLE001
            self._log_connected = False

    def _capture_log_full(self, level, message, tag):
        level_name = str(getattr(level, "name", level)).lower()
        self.server.capture_qgis_message(str(message), level_name, str(tag))

    def _capture_log_legacy(self, message, tag, level):
        level_name = str(getattr(level, "name", level)).lower()
        self.server.capture_qgis_message(str(message), level_name, str(tag))

    def _maybe_autostart(self):
        settings = QgsSettings()
        if settings.value("%s/mode.autostart" % SETTINGS_PREFIX, False, type=bool):
            QTimer.singleShot(900, self.start_server)

    # ------------------------------------------------------------------
    # server control
    # ------------------------------------------------------------------
    def start_server(self):
        settings = QgsSettings()
        host = str(settings.value("%s/mode.host" % SETTINGS_PREFIX, "127.0.0.1"))
        frame_port = int(settings.value("%s/mode.frame_port" % SETTINGS_PREFIX,
                                        DEFAULT_FRAME_PORT, type=int))
        http_port = int(settings.value("%s/mode.http_port" % SETTINGS_PREFIX,
                                       DEFAULT_HTTP_PORT, type=int))
        http_enabled = bool(settings.value("%s/mode.http_enabled" % SETTINGS_PREFIX,
                                           True, type=bool))
        self.server.security.refresh()
        status = self.server.start(host, frame_port, http_port, http_enabled)
        self.main_action.setChecked(bool(status.get("running")))
        self.main_action.setIcon(self._icon(active=bool(status.get("running"))))
        if self.dock:
            self.dock.refresh_all()
        if status.get("errors"):
            compat.push_message("Piterm bridge failed to start: %s"
                                % "; ".join(status["errors"].values()), "warning")
        else:
            compat.push_message("Piterm bridge listening on %s (frame %s / http %s)"
                                % (host, status.get("frame_port"),
                                   status.get("http_port") or "off"), "success", 3)
        return status

    def stop_server(self):
        self.server.stop()
        self.main_action.setChecked(False)
        self.main_action.setIcon(self._icon(active=False))
        if self.dock:
            self.dock.refresh_all()
        return self.server.status()

    def toggle_server(self):
        if self.server.running:
            return self.stop_server()
        return self.start_server()

    def _toggle_from_action(self, checked):
        if checked and not self.server.running:
            self.start_server()
        elif not checked and self.server.running:
            self.stop_server()

    # ------------------------------------------------------------------
    # misc actions
    # ------------------------------------------------------------------
    def _reload_self(self):
        import qgis.utils as qu
        try:
            qu.unloadPlugin(PLUGIN_ID)
            qu.loadPlugin(PLUGIN_ID)
            qu.startPlugin(PLUGIN_ID)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.iface.mainWindow(), PLUGIN_TITLE,
                                "Reload failed: %s" % exc)

    def _show_configurator(self):
        from .ui.configurator import ConfiguratorDialog
        dialog = ConfiguratorDialog(self.iface.mainWindow(), self)
        dialog.exec() if hasattr(dialog, "exec") else dialog.show()

    def _open_storage(self):
        from qgis.PyQt.QtGui import QDesktopServices
        from qgis.PyQt.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.storage_dir))

    def _show_about(self):
        QMessageBox.information(
            self.iface.mainWindow(), PLUGIN_TITLE,
            "<b>Piterm QGIS AI %s</b><br><br>"
            "MCP/skill bridge for QGIS: %s commands, TCP frame + NDJSON + HTTP "
            "transports, job queue, checkpoints, journal replay and a full "
            "security policy layer.<br><br>"
            "Storage: %s" % (PLUGIN_VERSION, _command_count(), self.storage_dir))

    # ------------------------------------------------------------------
    # icon
    # ------------------------------------------------------------------
    def _icon(self, active=False):
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        try:
            antialias = compat.resolve(QPainter, "RenderHint.Antialiasing",
                                       "Antialiasing", default=None)
            if antialias is not None:
                painter.setRenderHint(antialias)
            painter.setBrush(QColor("#0b3a5b" if not active else "#0a7d55"))
            painter.setPen(QColor("#38bdf8" if not active else "#34d399"))
            painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
            painter.setPen(QColor("#e2e8f0"))
            font = QFont()
            font.setPointSize(30)
            font.setBold(True)
            painter.setFont(font)
            align = compat.resolve(Qt, "AlignmentFlag.AlignCenter", "AlignCenter")
            painter.drawText(pixmap.rect(), align, "π")
        finally:
            painter.end()
        return QIcon(pixmap)


def _command_count():
    try:
        from .registry import command_count
        return command_count()
    except Exception:  # noqa: BLE001
        return "?"
