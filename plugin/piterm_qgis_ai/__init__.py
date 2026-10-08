"""Piterm QGIS AI - QGIS plugin entry (classFactory)."""


def classFactory(iface):  # noqa: N802 (QGIS API)
    from .plugin import PitermQgisAiPlugin
    return PitermQgisAiPlugin(iface)
