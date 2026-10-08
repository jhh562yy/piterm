"""Shared helpers for every command handler: layer lookup, parameter plumbing,
geometry serialization, edit sessions, expression building, CRS and path checks.

Handlers stay thin: they validate via these helpers and call PyQGIS. Anything a
second handler would need lives here.
"""

from __future__ import annotations

import base64
import math
import os
from contextlib import contextmanager

from qgis.core import (QgsCoordinateReferenceSystem, QgsExpression,
                       QgsExpressionContext, QgsExpressionContextUtils,
                       QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsProject,
                       QgsRectangle, QgsRasterLayer, QgsVectorLayer, QgsWkbTypes)

from .. import compat
from ..constants import DEFAULT_SYNC_TIMEOUT
from ..errors import CommandError, ErrorCode, bad_request, invalid, io_error, \
    not_found, state

# ---------------------------------------------------------------------------
# Value / feature serialization
# ---------------------------------------------------------------------------

def value_to_json(value):
    """Convert a provider value to something JSON-safe."""
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    for method_name in ("toPyDateTime", "toPyDate", "toPyTime"):
        converter = getattr(value, method_name, None)
        if callable(converter):
            try:
                converted = converter()
                if hasattr(converted, "isoformat"):
                    return converted.isoformat()
            except Exception:  # noqa: BLE001
                pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, (list, tuple)):
        return [value_to_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): value_to_json(v) for k, v in value.items()}
    return str(value)


def geometry_to_json(geom, fmt="wkt", precision=6):
    """Serialize a QgsGeometry in one of: wkt, wkb_base64, wkb_hex, geojson, none."""
    if geom is None or geom.isNull():
        return None
    fmt = (fmt or "wkt").lower()
    if fmt == "none":
        return None
    if fmt == "wkt":
        try:
            return geom.asWkt(precision)
        except TypeError:
            return geom.asWkt()
    if fmt in ("wkb_base64", "wkb", "base64"):
        return {"wkb_base64": base64.b64encode(bytes(geom.asWkb())).decode("ascii")}
    if fmt in ("wkb_hex", "hex"):
        return {"wkb_hex": bytes(geom.asWkb()).hex()}
    if fmt in ("geojson", "json"):
        try:
            text = geom.asJson(precision)
            import json as _json
            return _json.loads(text)
        except Exception:  # noqa: BLE001
            return {"wkt": geom.asWkt()}
    raise bad_request("unknown geometry format: %s" % fmt,
                      detail={"accepted": ["wkt", "wkb_base64", "wkb_hex",
                                           "geojson", "none"]})


def feature_to_json(feature, layer=None, geometry="wkt", precision=6,
                    transform=None, field_names=None, with_geometry=True):
    out = {"fid": feature.id()}
    if layer is not None:
        fields = layer.fields() if field_names is None else [
            layer.fields().field(n) for n in field_names
            if layer.fields().indexFromName(n) >= 0]
        attributes = {}
        for field in fields:
            try:
                attributes[field.name()] = value_to_json(feature[field.name()])
            except KeyError:
                attributes[field.name()] = None
        out["attributes"] = attributes
    if with_geometry:
        geom = feature.geometry()
        if transform is not None and geom is not None and not geom.isNull():
            geom = QgsGeometry(geom)
            try:
                geom.transform(transform)
            except Exception:  # noqa: BLE001
                pass
        out["geometry"] = geometry_to_json(geom, geometry, precision)
    return out


def field_to_json(field):
    return {
        "name": field.name(),
        "type": compat.field_type_name(field),
        "alias": field.alias() or "",
        "length": field.length(),
        "precision": field.precision(),
        "comment": (field.comment() or "")[:500],
        "type_name": str(getattr(field.type(), "name", field.type())),
    }


# ---------------------------------------------------------------------------
# Handler base
# ---------------------------------------------------------------------------
class HandlerBase:
    """Everything handlers share; the server is attached after construction."""

    srv = None  # type: ignore[assignment]

    # -- wiring ------------------------------------------------------------
    def attach_server(self, srv):
        self.srv = srv

    @property
    def iface(self):
        try:
            from qgis.utils import iface as _iface
            return _iface
        except Exception:  # noqa: BLE001
            return None

    @property
    def project(self):
        return QgsProject.instance()

    @property
    def canvas(self):
        iface = self.iface
        canvas = getattr(iface, "mapCanvas", None)
        canvas = canvas() if callable(canvas) else getattr(iface, "mapCanvas", None)
        if canvas is None:
            raise state("no map canvas available (headless session?)")
        return canvas

    # -- parameter helpers -------------------------------------------------
    @staticmethod
    def require(params, *names):
        missing = [n for n in names if params.get(n) is None]
        if missing:
            raise bad_request("missing parameter(s): %s" % ", ".join(missing))
        return [params[n] for n in names]

    # -- layer lookup ------------------------------------------------------
    def _all_layers(self):
        return list(self.project.mapLayers().values())

    def layers_matching(self, text, kind=None):
        text = str(text)
        matches = []
        for layer in self._all_layers():
            if layer.id() == text or layer.name() == text:
                matches.append(layer)
        if not matches:
            lowered = text.casefold()
            for layer in self._all_layers():
                if layer.name().casefold() == lowered:
                    matches.append(layer)
        if kind:
            matches = [l for l in matches if compat.layer_kind(l) == kind]
        return matches

    def resolve_layer(self, params, key="layer", kind=None, required=True,
                      project=None):
        """Resolve a layer from ``layer`` (id, name, or dict) / ``layer_id``."""
        project = project or self.project
        spec = params.get(key)
        if spec is None:
            spec = params.get("layer_id")
        if spec is None:
            if not required:
                return None
            raise bad_request(
                "missing layer reference; pass %r as layer id or name" % key)
        if isinstance(spec, dict):
            spec = spec.get("id") or spec.get("name")
        layer = project.mapLayer(str(spec))
        if layer is None:
            matches = self.layers_matching(spec, kind)
            if not matches:
                available = [l.name() for l in project.mapLayers().values()][:40]
                raise not_found("no layer matching %r" % spec,
                                detail={"available": available,
                                        "hint": "use name, id or call layers.list"})
            if len(matches) > 1:
                raise CommandError(
                    ErrorCode.AMBIGUOUS,
                    "%r matches %d layers; use a layer id" % (spec, len(matches)),
                    detail={"ids": [{"id": l.id(), "name": l.name()} for l in matches[:20]]})
            layer = matches[0]
        if kind and compat.layer_kind(layer) != kind:
            raise invalid("%s is a %s layer, expected %s"
                          % (layer.name(), compat.layer_kind(layer), kind),
                          detail={"id": layer.id()})
        return layer

    def resolve_vector(self, params, key="layer", required=True):
        return self.resolve_layer(params, key, "vector", required)

    def resolve_raster(self, params, key="layer", required=True):
        return self.resolve_layer(params, key, "raster", required)

    def resolve_active_layer(self, kind=None):
        iface = self.iface
        layer = iface.activeLayer() if iface else None
        if layer is None:
            raise state("no active layer in the layer panel")
        if kind and compat.layer_kind(layer) != kind:
            raise invalid("active layer is not a %s layer" % kind)
        return layer

    # -- fields ------------------------------------------------------------
    def field_index(self, layer, name, field_names=None):
        idx = layer.fields().indexFromName(str(name))
        if idx < 0:
            lowered = str(name).casefold()
            for field in layer.fields():
                if field.name().casefold() == lowered:
                    return field.name()
        if idx < 0:
            available = [f.name() for f in layer.fields()][:60]
            raise not_found("no field %r on layer %s" % (name, layer.name()),
                            detail={"fields": available})
        return str(name)

    # -- expressions -------------------------------------------------------
    def build_expression(self, text, layer=None, context=None):
        expression = QgsExpression(str(text))
        if expression.hasParserError():
            raise CommandError(ErrorCode.EXPRESSION,
                               "expression parse error: %s" % expression.parserErrorString(),
                               detail={"expression": str(text)[:300]})
        if layer is not None:
            ctx = context or self.expression_context(layer)
            if not expression.prepare(ctx):
                raise CommandError(
                    ErrorCode.EXPRESSION,
                    "expression cannot be evaluated here: %s"
                    % (expression.evalErrorString() or "unknown error"),
                    detail={"expression": str(text)[:300]})
        return expression

    def expression_context(self, layer=None):
        ctx = QgsExpressionContext()
        ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer)
                         if layer is not None
                         else QgsExpressionContextUtils.projectScope(self.project))
        return ctx

    def feature_request(self, layer, expression=None, bbox=None, ids=None,
                        limit=None, offset=None, order_by=None,
                        no_geometry=False, fields=None):
        """Build a QgsFeatureRequest from friendly parameters."""
        from qgis.core import QgsFeatureRequest
        request = QgsFeatureRequest()
        if expression:
            request.setFilterExpression(str(expression))
            request.setExpressionContext(self.expression_context(layer))
        if bbox is not None:
            request.setFilterRect(bbox)
        if ids:
            request.setFilterFids([int(i) for i in ids])
        if limit is not None:
            request.setLimit(int(limit))
        if offset:
            try:
                request.setOffset(int(offset))
            except AttributeError:
                pass
        if no_geometry:
            flag = compat.resolve(QgsFeatureRequest, "Flag.NoGeometry", "NoGeometry",
                                  default=None)
            if flag is not None:
                try:
                    request.setFlag(flag, True)
                except Exception:  # noqa: BLE001
                    try:
                        request.setFlags(flag)
                    except Exception:  # noqa: BLE001
                        pass
        if fields is not None:
            names = [str(f) for f in fields]
            request.setSubsetOfAttributes(names, layer.fields())
        if order_by:
            order = self._order_by_clause(order_by)
            if order is not None:
                request.setOrderBy(order)
        return request

    def _order_by_clause(self, order_by):
        from qgis.core import QgsFeatureRequest
        clause_cls = compat.resolve(QgsFeatureRequest, "OrderByClause", default=None)
        order_cls = compat.resolve(QgsFeatureRequest, "OrderBy", default=None)
        if clause_cls is None or order_cls is None:
            return None
        clauses = []
        items = order_by if isinstance(order_by, (list, tuple)) else [order_by]
        for item in items:
            if isinstance(item, str):
                name, ascending = item, True
                lowered = item.strip().lower()
                if lowered.endswith(" desc"):
                    name, ascending = item.strip()[:-5].strip(), False
                elif lowered.endswith(" asc"):
                    name = item.strip()[:-4].strip()
                clauses.append(clause_cls(name, ascending))
            elif isinstance(item, dict):
                clauses.append(clause_cls(
                    str(item.get("field", item.get("name", ""))),
                    bool(item.get("ascending", True))))
        return order_cls(clauses) if clauses else None

    # -- CRS ---------------------------------------------------------------
    def resolve_crs(self, value, fallback=None):
        crs = compat.crs_from_any(value)
        if crs is None and fallback is not None:
            return fallback
        if crs is None:
            raise CommandError(ErrorCode.CRS, "unknown CRS: %r" % (value,),
                               detail={"hint": "pass 'EPSG:4326' or an integer id"})
        return crs

    def transform_to(self, src_crs, dst_crs):
        if src_crs is None or dst_crs is None:
            return None
        if src_crs.authid() == dst_crs.authid():
            return None
        return compat.transform_between(src_crs, dst_crs,
                                        self.project.transformContext())

    # -- geometry input ----------------------------------------------------
    def geometry_from_params(self, params, key="geometry", layer=None):
        spec = params.get(key)
        if spec is None:
            for alt in ("wkt", "geometry_wkt", "wkb_hex", "geojson"):
                if params.get(alt) is not None:
                    spec = {alt.replace("geometry_", ""): params[alt]}
                    break
        if spec is None:
            return None
        geom = compat.geometry_from_any(spec)
        if geom is None:
            raise CommandError(ErrorCode.GEOMETRY,
                               "could not parse geometry (%s)" % key,
                               detail={"hint": "supply WKT, wkb_hex or GeoJSON"})
        if layer is not None and params.get("crs") is not None:
            src = self.resolve_crs(params["crs"])
            tr = self.transform_to(src, layer.crs())
            if tr is not None:
                geom = QgsGeometry(geom)
                try:
                    geom.transform(tr)
                except Exception as exc:  # noqa: BLE001
                    raise CommandError(ErrorCode.CRS, "geometry transform failed: %s" % exc)
        return geom

    def bbox_from_params(self, params, key="bbox", crs=None):
        """Accept [xmin,ymin,xmax,ymax] or {xmin..} in ``key``/``crs``."""
        spec = params.get(key)
        if spec is None:
            if all(params.get(k) is not None for k in ("xmin", "ymin", "xmax", "ymax")):
                spec = {k: params[k] for k in ("xmin", "ymin", "xmax", "ymax")}
            else:
                return None
        if isinstance(spec, dict):
            try:
                rect = QgsRectangle(float(spec["xmin"]), float(spec["ymin"]),
                                    float(spec["xmax"]), float(spec["ymax"]))
            except (KeyError, TypeError, ValueError):
                raise bad_request("bbox dict needs xmin/ymin/xmax/ymax numbers")
        elif isinstance(spec, (list, tuple)) and len(spec) >= 4:
            try:
                rect = QgsRectangle(float(spec[0]), float(spec[1]),
                                    float(spec[2]), float(spec[3]))
            except (TypeError, ValueError):
                raise bad_request("bbox must contain four numbers")
        else:
            raise bad_request("bbox must be [xmin,ymin,xmax,ymax] or an object")
        if rect.xMinimum() > rect.xMaximum():
            rect = QgsRectangle(rect.xMaximum(), rect.yMinimum(),
                                rect.xMinimum(), rect.yMaximum())
        if rect.yMinimum() > rect.yMaximum():
            rect = QgsRectangle(rect.xMinimum(), rect.yMaximum(),
                                rect.xMaximum(), rect.yMinimum())
        src = self.resolve_crs(params.get("bbox_crs") or params.get("crs")) \
            if (params.get("bbox_crs") or params.get("crs")) else None
        if src is not None and crs is not None:
            rect = compat.transform_rect(rect, src, crs, self.project.transformContext())
        return rect

    # -- paths -------------------------------------------------------------
    def check_path(self, path, for_write=False):
        return self.srv.security.check_path(path, for_write)

    # -- edit sessions -----------------------------------------------------
    @contextmanager
    def edit_scope(self, layer, description="Piterm edit", commit=True):
        """Undoable edit macro; commits only sessions this command opened."""
        started = False
        ok_path = False
        try:
            if not layer.isEditable():
                if not layer.startEditing():
                    raise state("cannot start editing on %s" % layer.name())
                started = True
            layer.beginEditCommand(description)
            yield layer
            layer.endEditCommand()
            ok_path = True
        except Exception:
            try:
                layer.destroyEditCommand()
            except Exception:  # noqa: BLE001
                pass
            if started:
                try:
                    layer.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            if started and ok_path and commit:
                result = layer.commitChanges()
                if isinstance(result, tuple):
                    ok, errors = result
                else:
                    ok = bool(result)
                    errors = list(getattr(layer, "commitErrors", lambda: [])() or [])
                if not ok:
                    raise io_error("commit failed: %s"
                                   % "; ".join(str(e) for e in errors[:5]))

    # -- summaries ---------------------------------------------------------
    def layer_summary(self, layer, with_extent=True):
        kind = compat.layer_kind(layer)
        out = {
            "id": layer.id(),
            "name": layer.name(),
            "kind": kind,
            "source": layer.publicSource()[:300],
            "crs": layer.crs().authid() or layer.crs().toWkt()[:80],
        }
        try:
            root = self.project.layerTreeRoot()
            node = root.findLayer(layer.id()) if root else None
            out["visible"] = bool(node.isVisible()) if node else None
            if node is not None:
                parent = node.parent()
                out["group"] = parent.name() if parent is not None and parent is not root else None
        except Exception:  # noqa: BLE001
            out["visible"] = None
        if with_extent:
            out["extent"] = compat.rect_to_dict(layer.extent())
        if kind == "vector":
            try:
                out["geometry_type"] = QgsWkbTypes.displayString(layer.wkbType())
            except Exception:  # noqa: BLE001
                out["geometry_type"] = "unknown"
            out["feature_count"] = layer.featureCount()
            out["fields"] = [f.name() for f in layer.fields()][:60]
            out["editable"] = bool(layer.isEditable())
            out["geom_type_name"] = str(getattr(layer.geometryType(), "name",
                                                layer.geometryType()))
        elif kind == "raster":
            try:
                provider = layer.dataProvider()
                out["width"] = layer.width()
                out["height"] = layer.height()
                out["band_count"] = layer.bandCount()
                out["provider"] = provider.name() if provider else ""
            except Exception:  # noqa: BLE001
                pass
        return out

    @staticmethod
    def point_from_params(params, x_key="x", y_key="y"):
        try:
            return QgsPointXY(float(params[x_key]), float(params[y_key]))
        except (KeyError, TypeError, ValueError):
            raise bad_request("missing or invalid %s/%s coordinates" % (x_key, y_key))

    @staticmethod
    def _measure_deadline(default=DEFAULT_SYNC_TIMEOUT):
        return default
