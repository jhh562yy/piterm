"""Write Piterm bridge settings into the profile QgsSettings (autostart, ports).

Run with the QGIS python of the target generation:

    "C:\\Program Files\\QGIS 3.44.15\\bin\\python-qgis-ltr.bat" scripts/enable_autostart.py
    "C:\\Program Files\\QGIS 4.2.3\\bin\\python-qgis.bat"        scripts/enable_autostart.py --disable

Because QgsSettings uses the running generation's profile root, invoking the
3.44 python writes the QGIS3 profile and the 4.2.3 python the QGIS4 profile.
"""

from __future__ import annotations

import sys

from qgis.core import Qgis, QgsApplication, QgsSettings


def main(argv):
    disable = "--disable" in argv
    frame_port = 9877
    http_port = 9879
    for index, token in enumerate(argv):
        if token == "--frame-port" and index + 1 < len(argv):
            frame_port = int(argv[index + 1])
        if token == "--http-port" and index + 1 < len(argv):
            http_port = int(argv[index + 1])
    # Headless QgsApplication has no org/app name, so QgsSettings would land in
    # %APPDATA%\python\... instead of the real profile. Pin them before init.
    app_name = "QGIS4" if Qgis.QGIS_VERSION_INT >= 40000 else "QGIS3"
    QgsApplication.setOrganizationName("QGIS")
    QgsApplication.setOrganizationDomain("qgis.org")
    QgsApplication.setApplicationName(app_name)
    app = QgsApplication([], False)
    app.setApplicationName(app_name)
    app.initQgis()
    try:
        settings = QgsSettings()
        values = {
            "piterm_qgis_ai/mode.autostart": not disable,
            "piterm_qgis_ai/mode.host": "127.0.0.1",
            "piterm_qgis_ai/mode.frame_port": frame_port,
            "piterm_qgis_ai/mode.http_port": http_port,
            "piterm_qgis_ai/mode.http_enabled": True,
        }
        for key, value in values.items():
            settings.setValue(key, value)
        settings.sync()
        print("profile:", QgsApplication.qgisSettingsDirPath())
        print("autostart:", settings.value("piterm_qgis_ai/mode.autostart"))
        print("ports:", settings.value("piterm_qgis_ai/mode.frame_port"),
              "/", settings.value("piterm_qgis_ai/mode.http_port"))
    finally:
        app.exitQgis()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
