"""Compatibility layer for QGIS 3.16+ .. 4.x and Qt5/Qt6 (PyQt5/PyQt6).

QGIS 4 moved most enums to scoped forms (``Qgis.Info`` -> ``Qgis.MessageLevel.Info``,
``QgsMapLayer.VectorLayer`` -> ``QgsMapLayer.LayerType.VectorLayer`` ...). This
module resolves both spellings at runtime through :func:`resolve`, so the rest of
the codebase never contains a version conditional.

The module is import-safe outside QGIS (``HAS_QGIS`` is False); anything that
touches QGIS classes should call :func:`require_qgis` first when it must fail
loudly, or simply run inside QGIS.

Everything here was written against the local installs: QGIS 3.44.15 (Qt 5.15,
Python 3.12) and QGIS 4.2.3 (Qt 6), and is defensive against 3.16..3.40.
"""

from __future__ import annotations

import os
import time

_MISSING = object()

try:  # pragma: no cover - exercised inside QGIS
    from qgis.core import (  # noqa: F401
        Qgis,
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsCoordinateTransformContext,
        QgsGeometry,
        QgsMessageLog,
        QgsPointXY,
        QgsProject,
        QgsRectangle,
        QgsSettings,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import QTimer  # noqa: F401
    HAS_QGIS = True
except Exception:  # pragma: no cover - plain-python test environments
    HAS_QGIS = False


def require_qgis():
    if not HAS_QGIS:
        raise RuntimeError(
            "This function must run inside QGIS (qgis.core is not importable)")


# ---------------------------------------------------------------------------
# Generic attribute / enum resolution
# ---------------------------------------------------------------------------
def resolve(obj, *paths, **kw):
    """Return the first existing attribute path on *obj*.

    ``resolve(QgsMapLayer, "LayerType.VectorLayer", "VectorLayer")`` handles
    QGIS 4 scoped enums and QGIS 3 flat names in one call. Raises LookupError
    unless ``default`` is supplied.
    """
    default = kw.pop("default", _MISSING)
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            cur = getattr(cur, part, _MISSING)
            if cur is _MISSING:
                ok = False
                break
        if ok:
            return cur
    if default is not _MISSING:
        return default
    raise LookupError("none of %r found on %r" % (paths, obj))


def version_int():
    if not HAS_QGIS:
        return 0
    try:
        return int(Qgis.QGIS_VERSION_INT)
    except Exception:  # noqa: BLE001
        return 0


def qgis_version_string():
    if not HAS_QGIS:
        return "not-in-qgis"
    try:
        return str(Qgis.QGIS_VERSION)
    except Exception:  # noqa: BLE001
        return "unknown"


def is_qgis4():
    return version_int() >= 40000


def qt_version_string():
    try:
        from qgis.PyQt.QtCore import QT_VERSION_STR
        return str(QT_VERSION_STR)
    except Exception:  # noqa: BLE001
        return "unknown"


def is_pyqt6():
    return qt_version_string().startswith("6")


# ---------------------------------------------------------------------------
# Message log / message bar
# ---------------------------------------------------------------------------
def _msg_level(kind):
    mapping = {
        "info": ("MessageLevel.Info", "Info"),
        "warning": ("MessageLevel.Warning", "Warning"),
        "critical": ("MessageLevel.Critical", "Critical"),
        "success": ("MessageLevel.Success", "Success"),
        "none": ("MessageLevel.NoLevel", "NoLevel"),
    }
    names = mapping.get(kind, mapping["info"])
    return resolve(Qgis, *names, default=None)


LOG_TAG = "Piterm QGIS AI"


def log(text, kind="info", tag=LOG_TAG):
    """Write to the QGIS message log (never raises)."""
    if not HAS_QGIS:
        return
    try:
        QgsMessageLog.logMessage(str(text), tag, _msg_level(kind) or 0)
    except Exception:  # noqa: BLE001
        pass


def push_message(text, kind="info", duration=5):
    """Show a message bar banner on the main window (never raises)."""
    try:
        from qgis.utils import iface as _iface
        bar = _iface.messageBar() if _iface else None
        if bar is None:
            return
        level = _msg_level(kind)
        if hasattr(bar, "pushMessage"):
            bar.pushMessage("Piterm QGIS AI", str(text), level, duration)
        else:  # very old signature fallback
            bar.pushMessage(str(text), level, duration)
    except Exception:  # noqa: BLE001
        log(text, kind)


# ---------------------------------------------------------------------------
# Project helpers
# ---------------------------------------------------------------------------
def project():
    return QgsProject.instance()


def read_project_guarded(path, timeout_ms=2500):
    """Load a project while auto-dismissing the modal 'Handle Unavailable
    Layers' dialog that would otherwise freeze a socket command forever.

    Returns True when the project was read without being cancelled.
    """
    require_qgis()
    from qgis.PyQt.QtWidgets import QApplication

    deadline = time.monotonic() + timeout_ms / 1000.0
    accepted = {"value": None}

    def _sweep():
        for widget in QApplication.topLevelWidgets():
            try:
                cls = widget.metaObject().className()
            except Exception:  # noqa: BLE001
                continue
            if cls == "QgsHandleBadLayers" and widget.isVisible():
                # Press the dialog's own "Add Without Resolving" default: reject()
                # keeps layers that are fine and drops the broken ones silently.
                try:
                    widget.reject()
                except Exception:  # noqa: BLE001
                    try:
                        widget.close()
                    except Exception:  # noqa: BLE001
                        pass
        if time.monotonic() < deadline:
            QTimer.singleShot(50, _sweep)

    QTimer.singleShot(50, _sweep)
    result = project().read(path)
    accepted["value"] = result
    return bool(result)


# ---------------------------------------------------------------------------
# Layer / geometry helpers
# ---------------------------------------------------------------------------
LAYER_TYPE_NAMES = {
    "VectorLayer": "vector",
    "RasterLayer": "raster",
    "MeshLayer": "mesh",
    "PointCloudLayer": "pointcloud",
    "VectorTileLayer": "vectortile",
    "AnnotationLayer": "annotation",
    "PluginLayer": "plugin",
    "GroupLayer": "group",
}


def layer_kind(layer):
    """Normalized layer kind string ("vector", "raster", ...)."""
    try:
        enum_value = layer.type()
    except Exception:  # noqa: BLE001
        return "unknown"
    for attr in dir(enum_value.__class__):
        pass
    # Match by comparing against each candidate enum value.
    for name, out in LAYER_TYPE_NAMES.items():
        try:
            if enum_value == resolve(layer.__class__, "%s.%s" % ("LayerType", name),
                                     name, default=None):
                return out
        except Exception:  # noqa: BLE001
            continue
    # Fallback: repr/name matching
    text = str(getattr(enum_value, "name", enum_value))
    for name, out in LAYER_TYPE_NAMES.items():
        if name in text:
            return out
    return "unknown"


def crs_from_any(value):
    """Build a QgsCoordinateReferenceSystem from user input, or None."""
    require_qgis()
    if value is None or value == "":
        return None
    if isinstance(value, QgsCoordinateReferenceSystem):
        return value if value.isValid() else None
    text = str(value).strip()
    if text.upper().startswith("EPSG:"):
        text = text.split(":", 1)[1]
    crs = QgsCoordinateReferenceSystem()
    if text.isdigit():
        if not crs.createFromId(int(text)):
            return None
    else:
        if not crs.createFromString(text):
            return None
    return crs if crs.isValid() else None


_TRANSFORM_CACHE = {}


def transform_between(src_crs, dst_crs, context=None):
    """Cached coordinate transform between two CRS."""
    require_qgis()
    if src_crs is None or dst_crs is None:
        return None
    src = src_crs.authid() or str(src_crs.toWkt())[:40]
    dst = dst_crs.authid() or str(dst_crs.toWkt())[:40]
    if src == dst:
        return None
    key = (src, dst, id(context) if context is not None else 0)
    tr = _TRANSFORM_CACHE.get(key)
    if tr is None:
        ctx = context or QgsCoordinateTransformContext()
        tr = QgsCoordinateTransform(src_crs, dst_crs, ctx)
        if len(_TRANSFORM_CACHE) > 128:
            _TRANSFORM_CACHE.clear()
        _TRANSFORM_CACHE[key] = tr
    return tr


def transform_rect(rect, src_crs, dst_crs, context=None):
    tr = transform_between(src_crs, dst_crs, context)
    if tr is None:
        return QgsRectangle(rect)
    try:
        return tr.transformBoundingBox(rect, handle180Crossover=True)
    except TypeError:
        return tr.transformBoundingBox(rect)


def transform_point(x, y, src_crs, dst_crs, context=None):
    tr = transform_between(src_crs, dst_crs, context)
    point = QgsPointXY(x, y)
    if tr is None:
        return point
    try:
        return tr.transform(point)
    except Exception:  # noqa: BLE001 - axis order quirks
        return tr.transform(QgsPointXY(y, x)) if False else point


def rect_to_dict(rect):
    if rect is None:
        return None
    return {"xmin": rect.xMinimum(), "ymin": rect.yMinimum(),
            "xmax": rect.xMaximum(), "ymax": rect.yMaximum(),
            "width": rect.width(), "height": rect.height()}


def point_to_list(point):
    return [point.x(), point.y()]


def geometry_from_any(spec):
    """Parse WKT / WKB(hex|base64) / GeoJSON / dict into a QgsGeometry."""
    require_qgis()
    if spec is None:
        return None
    geom = None
    if isinstance(spec, dict):
        if "wkt" in spec:
            geom = QgsGeometry.fromWkt(str(spec["wkt"]))
        elif "wkb_hex" in spec:
            geom = QgsGeometry.fromWkb(bytes.fromhex(str(spec["wkb_hex"])))
        elif "wkb_base64" in spec:
            import base64
            geom = QgsGeometry.fromWkb(base64.b64decode(str(spec["wkb_base64"])))
        elif "geojson" in spec:
            geom = _geom_from_geojson(spec["geojson"])
        elif spec.get("__b64__"):
            import base64
            geom = QgsGeometry.fromWkb(base64.b64decode(str(spec["__b64__"])))
        elif {"type", "coordinates"} <= set(spec):
            geom = _geom_from_geojson(spec)
    elif isinstance(spec, str):
        text = spec.strip()
        if text.startswith("{"):
            import json
            try:
                return _geom_from_geojson(json.loads(text))
            except ValueError:
                return None
        geom = QgsGeometry.fromWkt(text)
    if geom is None or geom.isNull():
        return None
    return geom


def _geom_from_geojson(obj):
    require_qgis()
    import json
    text = json.dumps(obj) if not isinstance(obj, str) else obj
    try:
        from qgis.core import QgsJsonUtils
        geom = QgsJsonUtils.geometryFromGeoJson(text)
        return geom if geom and not geom.isNull() else None
    except Exception:  # noqa: BLE001
        try:
            from qgis.core import QgsGeometry as _G
            creator = getattr(_G, "fromGeoJson", None)
            if creator is not None:
                geom = creator(text)
                return geom if geom and not geom.isNull() else None
        except Exception:  # noqa: BLE001
            pass
    return None


# ---------------------------------------------------------------------------
# Field types
# ---------------------------------------------------------------------------
_FIELD_TYPE_NAMES = {
    "string": ("String", "QString"),
    "int": ("Int", "Integer", "LongLong"),
    "int64": ("LongLong", "Int64"),
    "double": ("Double",),
    "bool": ("Bool", "Boolean"),
    "date": ("Date",),
    "datetime": ("DateTime",),
    "time": ("Time",),
    "binary": ("ByteArray", "QByteArray"),
    "json": ("Json", "Map", "String"),
}


def field_type_enum(kind):
    """Map a friendly type name to a QVariant/QMetaType enum value."""
    require_qgis()
    names = _FIELD_TYPE_NAMES.get(str(kind).lower())
    if names is None:
        raise ValueError("unknown field type: %s" % kind)
    try:
        from qgis.core import QMetaType  # QGIS 3.36+/4
        holder = resolve(QMetaType, "Type", default=None)
        if holder is not None:
            for name in names:
                value = getattr(holder, name, None)
                if value is not None:
                    return value
    except Exception:  # noqa: BLE001
        pass
    try:
        from qgis.PyQt.QtCore import QVariant
        for name in names:
            value = getattr(QVariant, name, None)
            if value is not None:
                return value
    except Exception:  # noqa: BLE001
        pass
    return names[0]


def field_type_name(field):
    """Friendly type name for a QgsField."""
    try:
        type_value = field.type()
    except Exception:  # noqa: BLE001
        return "unknown"
    name = getattr(type_value, "name", None) or str(type_value)
    low = name.lower()
    table = {
        "string": "string", "qstring": "string", "int": "int", "integer": "int",
        "longlong": "int64", "int64": "int64", "double": "double", "float": "double",
        "bool": "bool", "boolean": "bool", "date": "date", "datetime": "datetime",
        "time": "time", "bytearray": "binary", "qbytearray": "binary",
        "json": "json", "map": "json", "user": "user", "unknown": "unknown",
    }
    return table.get(low, low)
