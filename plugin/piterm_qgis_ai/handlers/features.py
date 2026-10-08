"""Feature commands: read, edit, select, identify, statistics, spatial ops,
cursors. Everything that touches rows of a vector layer lives here.

Editing semantics (learned the hard way in the reference implementation):

* a write goes through ``layer.addFeatures/changeAttributeValue/deleteFeatures``
  when an edit session is open (undoable, rollbackable) - the response says
  ``buffered: true``; otherwise the command opens a session, commits it, and
  says ``buffered: false``;
* geometries from another CRS are transformed only when ``crs`` is supplied;
* invalid geometries and Z-vs-2D mismatches are warnings, hard type mismatches
  (point on a polygon layer) are refusals.
"""

from __future__ import annotations

import math

from qgis.core import (QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
                       QgsRectangle, QgsVectorLayer, QgsWkbTypes)

from .. import compat
from ..errors import bad_request, invalid, not_found, state
from ..registry import command
from .base import HandlerBase, feature_to_json, value_to_json


class FeatureHandlers(HandlerBase):

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    @command(name="features.query", summary="Query features with expression / "
             "bbox / ids filters, pagination, ordering and geometry format "
             "control", risk="read", tags=("features",))
    def features_query(self, layer, filter=None, bbox=None, ids=None,
                       limit=100, offset=0, order_by=None, fields=None,
                       geometry="wkt", precision=6, selected_only=False):
        target = self.resolve_vector({"layer": layer})
        rect = self.bbox_from_params({"bbox": bbox, "bbox_crs": None}, "bbox",
                                     crs=target.crs())
        request = self.feature_request(
            target, expression=filter, bbox=rect,
            ids=ids if not selected_only else None,
            order_by=order_by, fields=fields)
        if selected_only:
            request.setFilterFids(target.selectedFeatureIds())
        offset = max(0, int(offset or 0))
        cap = max(1, min(int(limit or 100), 100000))
        features = []
        truncated = False
        iterator = target.getFeatures(request)
        skipped = 0
        for feature in iterator:
            if skipped < offset:
                skipped += 1
                continue
            if len(features) >= cap:
                truncated = True
                break
            features.append(feature_to_json(feature, target, geometry, precision,
                                            field_names=fields))
        return {
            "layer": target.id(),
            "name": target.name(),
            "count": len(features),
            "truncated": truncated,
            "offset": offset,
            "total": target.featureCount(),
            "geometry_format": geometry,
            "features": features,
        }

    @command(name="features.get", summary="Fetch one feature by fid",
             risk="read", tags=("features",))
    def features_get(self, layer, fid, geometry="wkt", precision=6):
        target = self.resolve_vector({"layer": layer})
        feature = target.getFeature(int(fid))
        if not feature.isValid():
            raise not_found("no feature with fid %s in %s" % (fid, target.name()))
        return {"layer": target.id(),
                "feature": feature_to_json(feature, target, geometry, precision)}

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------
    @command(name="features.insert", summary="Insert features (WKT/WKB/GeoJSON "
             "geometry, optional source CRS)", risk="write", tags=("features",))
    def features_insert(self, layer, features, crs=None, strict=False):
        target = self.resolve_vector({"layer": layer})
        if not isinstance(features, list) or not features:
            raise bad_request("'features' must be a non-empty list")
        transform = None
        if crs:
            src = self.resolve_crs(crs)
            transform = self.transform_to(src, target.crs())
        to_add = []
        warnings = []
        pre_editable = bool(target.isEditable())
        for index, item in enumerate(features):
            if not isinstance(item, dict):
                warnings.append({"index": index, "issue": "item is not an object"})
                continue
            feature = QgsFeature(target.fields())
            attributes = item.get("attributes") or {}
            for name, value in attributes.items():
                try:
                    field_name = self.field_index(target, name)
                except Exception:  # noqa: BLE001
                    warnings.append({"index": index,
                                     "issue": "unknown field %r (ignored)" % name})
                    continue
                try:
                    feature.setAttribute(str(field_name),
                                         _coerce_value(target.fields().field(field_name),
                                                       value))
                except Exception as exc:  # noqa: BLE001
                    warnings.append({"index": index,
                                     "issue": "field %s: %s" % (name, exc)})
            geometry_spec = item.get("geometry")
            if geometry_spec is not None:
                geometry = compat.geometry_from_any(geometry_spec)
                if geometry is None:
                    warnings.append({"index": index, "issue": "geometry not parseable"})
                else:
                    geometry = _conform_geometry(target, geometry, transform, warnings,
                                                 index, strict)
                    if geometry is not None:
                        feature.setGeometry(geometry)
            to_add.append(feature)
        if not to_add:
            raise invalid("no insertable features", detail={"warnings": warnings[:20]})
        with self.edit_scope(target, "insert %d features" % len(to_add)):
            ok = target.addFeatures(to_add)
            if not ok and strict:
                raise invalid("addFeatures returned failure")
        buffered = pre_editable
        ids = []
        for feature in to_add:
            try:
                feature_id = feature.id()
                if feature_id is not None and feature_id >= 0:
                    ids.append(feature_id)
            except Exception:  # noqa: BLE001
                pass
        all_ids = len(ids) == len(to_add)
        return {"layer": target.id(), "inserted": len(to_add),
                "fids": ids if all_ids else None,
                "fids_note": None if all_ids else
                             "provider assigns feature ids at commit time; "
                             "query the layer to read them",
                "buffered": bool(buffered), "warnings": warnings[:50]}

    @command(name="features.update", summary="Update attributes by fid",
             risk="write", tags=("features",))
    def features_update(self, layer, updates, strict=False):
        target = self.resolve_vector({"layer": layer})
        if not isinstance(updates, list) or not updates:
            raise bad_request("'updates' must be a non-empty list of "
                              "{fid, attributes}")
        results = []
        with self.edit_scope(target, "update %d features" % len(updates)):
            for item in updates:
                if not isinstance(item, dict) or item.get("fid") is None:
                    results.append({"fid": None, "ok": False,
                                    "error": "item needs fid"})
                    continue
                fid = int(item["fid"])
                if not target.getFeature(fid).isValid():
                    results.append({"fid": fid, "ok": False, "error": "fid not found"})
                    if strict:
                        raise not_found("fid %s not found" % fid)
                    continue
                errors = []
                for name, value in (item.get("attributes") or {}).items():
                    try:
                        field_name = self.field_index(target, name)
                        index = target.fields().indexFromName(field_name)
                        value = _coerce_value(target.fields().field(field_name), value)
                        if not target.changeAttributeValue(fid, index, value):
                            errors.append("%s: change rejected" % name)
                    except Exception as exc:  # noqa: BLE001
                        errors.append("%s: %s" % (name, exc))
                results.append({"fid": fid, "ok": not errors,
                                "errors": errors or None})
        return {"layer": target.id(),
                "updated": sum(1 for r in results if r["ok"]),
                "results": results[:200]}

    @command(name="features.update_geometry", summary="Replace geometries by fid",
             risk="write", tags=("features",))
    def features_update_geometry(self, layer, updates, crs=None, strict=False):
        target = self.resolve_vector({"layer": layer})
        if not isinstance(updates, list) or not updates:
            raise bad_request("'updates' must be a non-empty list of "
                              "{fid, geometry}")
        transform = None
        if crs:
            src = self.resolve_crs(crs)
            transform = self.transform_to(src, target.crs())
        warnings = []
        results = []
        with self.edit_scope(target, "update %d geometries" % len(updates)):
            for item in updates:
                fid = None
                try:
                    fid = int(item.get("fid")) if isinstance(item, dict) else None
                except (TypeError, ValueError):
                    fid = None
                if fid is None:
                    results.append({"fid": None, "ok": False, "error": "item needs fid"})
                    continue
                if not target.getFeature(fid).isValid():
                    results.append({"fid": fid, "ok": False, "error": "fid not found"})
                    if strict:
                        raise not_found("fid %s not found" % fid)
                    continue
                geometry = compat.geometry_from_any(item.get("geometry"))
                if geometry is None:
                    results.append({"fid": fid, "ok": False,
                                    "error": "geometry not parseable"})
                    continue
                geometry = _conform_geometry(target, geometry, transform, warnings,
                                             fid, strict)
                if geometry is None:
                    results.append({"fid": fid, "ok": False,
                                    "error": "geometry rejected (type mismatch)"})
                    continue
                if not target.changeGeometry(fid, geometry):
                    results.append({"fid": fid, "ok": False, "error": "change rejected"})
                    continue
                results.append({"fid": fid, "ok": True})
        return {"layer": target.id(),
                "updated": sum(1 for r in results if r["ok"]),
                "warnings": warnings[:50], "results": results[:200]}

    @command(name="features.delete", summary="Delete features by fids or an "
             "expression filter", risk="destructive", tags=("features",))
    def features_delete(self, layer, fids=None, filter=None, max_delete=200000):
        target = self.resolve_vector({"layer": layer})
        ids = []
        if fids:
            ids = [int(f) for f in fids]
        elif filter:
            request = self.feature_request(target, expression=filter,
                                           no_geometry=True, fields=[])
            for feature in target.getFeatures(request):
                ids.append(feature.id())
                if len(ids) > int(max_delete):
                    raise invalid(
                        "filter matches more than %d features; refine it or pass "
                        "fids explicitly" % int(max_delete))
        else:
            raise bad_request("pass fids=[...] or filter='expression'")
        if not ids:
            return {"layer": target.id(), "deleted": 0}
        with self.edit_scope(target, "delete %d features" % len(ids)):
            ok = target.deleteFeatures(ids)
        return {"layer": target.id(), "deleted": len(ids) if ok else 0,
                "requested": len(ids)}

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    @command(name="features.select", summary="Select features by expression, "
             "fids or bbox (mode: set/add/remove/intersect)", risk="write",
             tags=("features", "selection"))
    def features_select(self, layer, filter=None, fids=None, bbox=None,
                        mode="set", limit=200000):
        target = self.resolve_vector({"layer": layer})
        behavior = _selection_behavior(mode)
        if filter is not None:
            try:
                target.selectByExpression(str(filter), behavior)
            except TypeError:
                target.selectByExpression(str(filter))
        elif fids:
            target.selectByIds([int(f) for f in fids], behavior)
        elif bbox is not None:
            rect = self.bbox_from_params({"bbox": bbox}, "bbox", crs=target.crs())
            request = self.feature_request(target, bbox=rect, no_geometry=True,
                                           fields=[])
            ids = []
            for feature in target.getFeatures(request):
                ids.append(feature.id())
                if len(ids) > int(limit):
                    break
            target.selectByIds(ids, behavior)
        else:
            raise bad_request("pass filter=, fids= or bbox=")
        return {"layer": target.id(), "selected": len(target.selectedFeatureIds()),
                "mode": mode}

    @command(name="features.selection_get", summary="Selected feature ids "
             "(optionally with attributes)", risk="read",
             tags=("features", "selection"))
    def features_selection_get(self, layer, with_attributes=False, limit=100):
        target = self.resolve_vector({"layer": layer})
        ids = list(target.selectedFeatureIds())
        out = {"layer": target.id(), "count": len(ids), "fids": ids[:5000]}
        if with_attributes:
            features = []
            for fid in ids[: max(1, int(limit))]:
                feature = target.getFeature(fid)
                if feature.isValid():
                    features.append(feature_to_json(feature, target, "none"))
            out["features"] = features
        return out

    @command(name="features.selection_clear", summary="Clear the layer selection",
             risk="write", tags=("features", "selection"))
    def features_selection_clear(self, layer):
        target = self.resolve_vector({"layer": layer})
        target.removeSelection()
        return {"layer": target.id(), "selected": 0}

    @command(name="features.zoom_selection", summary="Zoom the canvas to selected "
             "features", risk="read", tags=("features", "selection"))
    def features_zoom_selection(self, layer, margin=1.1):
        target = self.resolve_vector({"layer": layer})
        canvas = self.canvas
        rect = target.boundingBoxOfSelected()
        if target.selectedFeatureCount() == 0 or rect is None or rect.isNull():
            raise state("nothing selected on %s" % target.name())
        rect = QgsRectangle(rect)
        if rect.width() <= 0 or rect.height() <= 0:
            # a single point selects a zero-area box - pad it so the zoom has
            # something to aim at
            try:
                base = canvas.mapSettings().extent().width()
            except Exception:  # noqa: BLE001
                base = 0.0
            pad = max(base * 0.005, 1e-6)
            if rect.width() <= 0:
                rect.setXMinimum(rect.xMinimum() - pad)
                rect.setXMaximum(rect.xMaximum() + pad)
            if rect.height() <= 0:
                rect.setYMinimum(rect.yMinimum() - pad)
                rect.setYMaximum(rect.yMaximum() + pad)
        dst = canvas.mapSettings().destinationCrs()
        crs = target.crs()
        if crs.isValid() and crs.authid() != dst.authid():
            rect = compat.transform_rect(QgsRectangle(rect), crs, dst,
                                         self.project.transformContext())
        factor = max(1.0, float(margin or 1.1))
        rect = QgsRectangle(rect.center().x() - rect.width() * factor / 2,
                            rect.center().y() - rect.height() * factor / 2,
                            rect.center().x() + rect.width() * factor / 2,
                            rect.center().y() + rect.height() * factor / 2)
        canvas.setExtent(rect)
        canvas.refresh()
        return {"zoomed": target.id(), "extent": compat.rect_to_dict(rect)}

    # ------------------------------------------------------------------
    # Identify / spatial
    # ------------------------------------------------------------------
    @command(name="features.identify", summary="Features at a map point across "
             "layers (map-click analogue)", risk="read", tags=("features",))
    def features_identify(self, x, y, layers=None, tolerance=0.0, crs=None,
                          limit_per_layer=5, max_hits=20, with_attributes=True):
        point = self.point_from_params({"x": x, "y": y})
        src_crs = self.resolve_crs(crs) if crs else self.project.crs()
        hit_geom = QgsGeometry.fromPointXY(point)
        if tolerance:
            hit_geom = hit_geom.buffer(float(tolerance), 8)
        targets = self._identify_targets(layers)
        hits = []
        for layer in targets:
            if len(hits) >= max_hits:
                break
            try:
                transform = self.transform_to(src_crs, layer.crs())
                local = QgsGeometry(hit_geom)
                if transform is not None:
                    local.transform(transform)
                request = self.feature_request(layer, bbox=local.boundingBox())
                count = 0
                for feature in layer.getFeatures(request):
                    geometry = feature.geometry()
                    if geometry is None or geometry.isNull():
                        continue
                    if not geometry.intersects(local):
                        continue
                    hits.append({
                        "layer": layer.id(),
                        "layer_name": layer.name(),
                        "fid": feature.id(),
                        "attributes": ({name: value_to_json(feature[name])
                                        for name in [f.name() for f in layer.fields()]}
                                       if with_attributes else None),
                    })
                    count += 1
                    if count >= limit_per_layer or len(hits) >= max_hits:
                        break
            except Exception as exc:  # noqa: BLE001
                hits.append({"layer": layer.id(), "error": str(exc)[:200]})
        return {"point": [point.x(), point.y()], "crs": src_crs.authid(),
                "count": len(hits), "hits": hits[:max_hits]}

    @command(name="features.spatial_query", summary="Features whose geometry "
             "satisfies a predicate against a query geometry (intersects, "
             "within, contains, ...)", risk="read", tags=("features", "spatial"))
    def features_spatial_query(self, layer, geometry, predicate="intersects",
                               crs=None, limit=100, bbox_prefilter=True,
                               geometry_format="wkt", precision=6):
        target = self.resolve_vector({"layer": layer})
        query_geom = compat.geometry_from_any(geometry)
        if query_geom is None:
            raise _geometry_error()
        if crs:
            src = self.resolve_crs(crs)
            transform = self.transform_to(src, target.crs())
            if transform is not None:
                query_geom = QgsGeometry(query_geom)
                query_geom.transform(transform)
        allowed = ("intersects", "within", "contains", "overlaps", "touches",
                   "crosses", "disjoint")
        if predicate not in allowed:
            raise bad_request("unknown predicate %r" % predicate,
                              detail={"accepted": list(allowed)})
        request = (self.feature_request(target, bbox=query_geom.boundingBox())
                   if bbox_prefilter else self.feature_request(target))
        features = []
        truncated = False
        cap = max(1, min(int(limit or 100), 100000))
        for feature in target.getFeatures(request):
            geometry_obj = feature.geometry()
            if geometry_obj is None or geometry_obj.isNull():
                continue
            try:
                matched = bool(getattr(geometry_obj, predicate)(query_geom))
            except AttributeError:
                raise bad_request("predicate %r not supported by this QGIS build"
                                  % predicate)
            if matched:
                if len(features) >= cap:
                    truncated = True
                    break
                features.append(feature_to_json(feature, target, geometry_format,
                                                precision))
        return {"layer": target.id(), "predicate": predicate,
                "count": len(features), "truncated": truncated,
                "features": features}

    @command(name="features.nearest", summary="K nearest features to a point "
             "(brute force; use filters on huge layers)", risk="read",
             tags=("features", "spatial"))
    def features_nearest(self, layer, x, y, k=1, max_distance=None, crs=None,
                         max_scan=300000, geometry_format="wkt"):
        import heapq
        target = self.resolve_vector({"layer": layer})
        point = self.point_from_params({"x": x, "y": y})
        src = self.resolve_crs(crs) if crs else self.project.crs()
        transform = self.transform_to(src, target.crs())
        local = QgsPointXY(point.x(), point.y())
        if transform is not None:
            local = transform.transform(local)
        point_geom = QgsGeometry.fromPointXY(local)
        heap = []
        scanned = 0
        for feature in target.getFeatures(self.feature_request(target,
                                                               no_geometry=False)):
            scanned += 1
            if scanned > int(max_scan):
                break
            geometry = feature.geometry()
            if geometry is None or geometry.isNull():
                continue
            distance = geometry.distance(point_geom)
            if max_distance is not None and distance > float(max_distance):
                continue
            item = (-distance, feature.id())
            if len(heap) < int(k):
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
        found = sorted(heap, key=lambda t: -t[0])
        features = []
        for neg_distance, fid in found:
            feature = target.getFeature(fid)
            if feature.isValid():
                entry = feature_to_json(feature, target, geometry_format)
                entry["distance"] = -neg_distance
                features.append(entry)
        return {"layer": target.id(), "scanned": min(scanned, int(max_scan)),
                "count": len(features), "features": features}

    # ------------------------------------------------------------------
    # Statistics & computation
    # ------------------------------------------------------------------
    @command(name="features.field_stats", summary="Aggregate statistics for a "
             "field (count/min/max/mean/sum/stdev/median...)", risk="read",
             tags=("features", "stats"))
    def features_field_stats(self, layer, field, stats=None):
        target = self.resolve_vector({"layer": layer})
        name = self.field_index(target, field)
        wanted = stats or ["count", "count_distinct", "count_missing", "min",
                           "max", "sum", "mean", "stdev", "median", "range"]
        if isinstance(wanted, str):
            wanted = [s.strip() for s in wanted.split(",") if s.strip()]
        out = {}
        aggregate = _aggregate_calculator(target)
        for stat in wanted:
            out[stat] = _aggregate_stat(aggregate, target, name, stat)
        return {"layer": target.id(), "field": name, "stats": out}

    @command(name="features.field_calculator", summary="Populate (or create and "
             "populate) a field from a QGIS expression", risk="write",
             tags=("features", "fields"))
    def features_field_calculator(self, layer, expression, field=None,
                                  new_field=None, type="double",
                                  selected_only=False):
        target = self.resolve_vector({"layer": layer})
        field_name = field or new_field
        if not field_name:
            raise bad_request("pass field= (existing) or new_field= (creates it)")
        expr = self.build_expression(expression, layer=target)
        fields = target.fields()
        created = False
        if fields.indexFromName(str(field_name)) < 0:
            if target.isEditable():
                raise state("layer has uncommitted edits; commit or rollback "
                            "before adding a field")
            with self.edit_scope(target, "add computed field %s" % field_name):
                from qgis.core import QgsField
                field_obj = QgsField(str(field_name),
                                     compat.field_type_enum(type))
                target.dataProvider().addAttributes([field_obj])
                target.updateFields()
            created = True
            fields = target.fields()
        name = self.field_index(target, field_name)
        index = fields.indexFromName(name)
        request = self.feature_request(
            target, no_geometry=not _expression_needs_geometry(expr),
            fields=[])
        if selected_only:
            request.setFilterFids(target.selectedFeatureIds())
        context = self.expression_context(target)
        updated = 0
        failures = 0
        first_error = None
        changed = 0
        with self.edit_scope(target, "field calculator %s" % name):
            for feature in target.getFeatures(request):
                context.setFeature(feature)
                value = expr.evaluate(context)
                if expr.hasEvalError():
                    failures += 1
                    if first_error is None:
                        first_error = expr.evalErrorString()
                    continue
                try:
                    value = _coerce_value(fields.field(index), value)
                    if target.changeAttributeValue(feature.id(), index, value):
                        changed += 1
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    if first_error is None:
                        first_error = str(exc)
                updated += 1
        return {"layer": target.id(), "field": name, "created": created,
                "evaluated": updated, "changed": changed, "failures": failures,
                "first_error": first_error}

    @command(name="features.transform_coordinates", summary="Transform points or "
             "a bbox between CRS (read-only helper)", risk="read",
             tags=("features", "crs"))
    def features_transform_coordinates(self, from_crs, to_crs, points=None,
                                       bbox=None):
        src = self.resolve_crs(from_crs)
        dst = self.resolve_crs(to_crs)
        transform = compat.transform_between(src, dst, self.project.transformContext())
        if points is not None:
            out = []
            for item in points:
                if isinstance(item, dict):
                    px, py = float(item.get("x")), float(item.get("y"))
                else:
                    px, py = float(item[0]), float(item[1])
                point = QgsPointXY(px, py)
                if transform is not None:
                    point = transform.transform(point)
                out.append([point.x(), point.y()])
            return {"from": src.authid(), "to": dst.authid(), "points": out}
        if bbox is not None:
            rect = self.bbox_from_params({"bbox": bbox}, "bbox", crs=src)
            if transform is not None:
                rect = compat.transform_rect(rect, src, dst,
                                             self.project.transformContext())
            return {"from": src.authid(), "to": dst.authid(),
                    "bbox": compat.rect_to_dict(rect)}
        raise bad_request("pass points=[...] or bbox=[...]")

    @command(name="features.geometry_info", summary="Geometry metrics: area, "
             "length, centroid, bbox, validity (planar or ellipsoidal)",
             risk="read", tags=("features", "geometry"))
    def features_geometry_info(self, layer=None, fid=None, geometry=None,
                               crs=None, ellipsoidal=False):
        target = self.resolve_layer({"layer": layer}, kind="vector",
                                    required=False) if layer else None
        geom = None
        if geometry is not None:
            geom = compat.geometry_from_any(geometry)
            if geom is None:
                raise _geometry_error()
            if crs and target is not None:
                src = self.resolve_crs(crs)
                transform = self.transform_to(src, target.crs())
                if transform is not None:
                    geom = QgsGeometry(geom)
                    geom.transform(transform)
        elif target is not None and fid is not None:
            feature = target.getFeature(int(fid))
            if not feature.isValid():
                raise not_found("no feature %s" % fid)
            geom = feature.geometry()
        if geom is None or geom.isNull():
            raise invalid("no geometry to inspect; pass geometry= or layer+fid")
        info = {
            "type": QgsWkbTypes.displayString(geom.wkbType()),
            "is_valid": bool(geom.isGeosValid()),
            "is_empty": bool(geom.isEmpty()),
            "is_simple": _safe_call(geom, "isSimple"),
            "parts": _count_parts(geom),
            "bbox": compat.rect_to_dict(geom.boundingBox()),
            "centroid": compat.point_to_list(geom.centroid().asPoint()),
            "area": geom.area(),
            "length": geom.length(),
            "perimeter": _safe_call(lambda: geom.constGet().perimeter()),
            "num_points": _count_points(geom),
            "measure": "planar",
        }
        if ellipsoidal and target is not None:
            try:
                from qgis.core import QgsDistanceArea
                da = QgsDistanceArea()
                da.setSourceCrs(target.crs(), self.project.transformContext())
                da.setEllipsoid(self.project.ellipsoid() or "WGS84")
                info["area_ellipsoidal"] = da.measureArea(geom)
                info["length_ellipsoidal"] = da.measureLength(geom)
                info["measure"] = "ellipsoidal (%s)" % (self.project.ellipsoid() or "WGS84")
            except Exception as exc:  # noqa: BLE001
                info["measure_error"] = str(exc)[:200]
        return info

    # ------------------------------------------------------------------
    # Cursors (chunked reads)
    # ------------------------------------------------------------------
    @command(name="features.cursor_open", summary="Open a cursor over a feature "
             "query for chunked reads", risk="read", tags=("features", "cursor"))
    def features_cursor_open(self, layer, filter=None, bbox=None, fields=None,
                             geometry="wkt", precision=6, selected_only=False):
        target = self.resolve_vector({"layer": layer})
        rect = self.bbox_from_params({"bbox": bbox}, "bbox", crs=target.crs())
        request = self.feature_request(target, expression=filter, bbox=rect,
                                       fields=fields)
        if selected_only:
            request.setFilterFids(target.selectedFeatureIds())
        fmt, prec = geometry, precision
        layer_obj = target

        def factory():
            for feature in layer_obj.getFeatures(request):
                yield feature_to_json(feature, layer_obj, fmt, prec,
                                      field_names=fields)

        result = self.srv.cursors.open("features", factory,
                                       {"layer": target.id(),
                                        "name": target.name()})
        return result

    @command(name="features.cursor_fetch", summary="Fetch the next chunk from a "
             "cursor", risk="read", tags=("features", "cursor"))
    def features_cursor_fetch(self, cursor, n=500):
        return self.srv.cursors.fetch(str(cursor), int(n))

    @command(name="features.cursor_close", summary="Close a cursor",
             risk="read", tags=("features", "cursor"))
    def features_cursor_close(self, cursor):
        return self.srv.cursors.close(str(cursor))

    @command(name="features.cursors_list", summary="Open cursors", risk="read",
             tags=("features", "cursor"))
    def features_cursors_list(self):
        return {"cursors": self.srv.cursors.list()}

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _identify_targets(self, layers):
        if layers:
            return [self.resolve_layer({"layer": l}) for l in layers]
        out = []
        for layer in self.project.mapLayers().values():
            if compat.layer_kind(layer) == "vector":
                try:
                    node = self.project.layerTreeRoot().findLayer(layer.id())
                    if node is not None and not node.isVisible():
                        continue
                except Exception:  # noqa: BLE001
                    pass
                out.append(layer)
        return out


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------
def _geometry_error():
    from ..errors import CommandError, ErrorCode
    return CommandError(ErrorCode.GEOMETRY, "geometry could not be parsed",
                        detail={"hint": "pass WKT, {wkb_hex:...} or GeoJSON"})


def _safe_call(target, name=None):
    """Call ``target()`` / ``getattr(target, name)()`` and swallow failures."""
    try:
        if name is None:
            return target()
        attr = getattr(target, name)
        return attr() if callable(attr) else attr
    except Exception:  # noqa: BLE001
        return None


def _count_parts(geom):
    try:
        return len(list(geom.constParts()))
    except Exception:  # noqa: BLE001
        return None


def _count_points(geom):
    try:
        return len(list(geom.constGet().vertices()))
    except Exception:  # noqa: BLE001
        try:
            return len(list(geom.vertices()))
        except Exception:  # noqa: BLE001
            return None


def _expression_needs_geometry(expression):
    for attr in ("needsGeometry",):
        fn = getattr(expression, attr, None)
        if fn is not None:
            try:
                return bool(fn())
            except Exception:  # noqa: BLE001
                return True
    return True


def _selection_behavior(mode):
    mode = str(mode or "set").lower()
    mapping = {
        "set": ("SelectionBehavior.SetSelection", "SetSelection"),
        "add": ("SelectionBehavior.AddToSelection", "AddToSelection"),
        "remove": ("SelectionBehavior.RemoveFromSelection", "RemoveFromSelection"),
        "subtract": ("SelectionBehavior.RemoveFromSelection", "RemoveFromSelection"),
        "intersect": ("SelectionBehavior.IntersectSelection", "IntersectSelection"),
    }
    names = mapping.get(mode)
    if names is None:
        raise bad_request("unknown selection mode %r" % mode,
                          detail={"accepted": sorted(mapping)})
    return compat.resolve(QgsVectorLayer, *names)


def _conform_geometry(layer, geometry, transform, warnings, index, strict):
    """Type-check / transform a geometry against the layer, or refuse."""
    if transform is not None:
        geometry = QgsGeometry(geometry)
        try:
            geometry.transform(transform)
        except Exception as exc:  # noqa: BLE001
            warnings.append({"index": index, "issue": "transform failed: %s" % exc})
            return None
    try:
        layer_geom_type = layer.geometryType()
        geom_type = geometry.type()
        if layer_geom_type != geom_type and str(layer.geometryType()) != "Unknown":
            message = ("geometry type mismatch: layer expects %s, got %s"
                       % (layer_geom_type, geom_type))
            if strict:
                raise invalid(message)
            warnings.append({"index": index, "issue": message})
            return None
    except AttributeError:
        pass
    if not geometry.isGeosValid():
        warnings.append({"index": index,
                         "issue": "invalid geometry (self-intersection?) stored as-is"})
    try:
        if layer.wkbType() != QgsWkbTypes.Unknown:
            layer_has_z = QgsWkbTypes.hasZ(layer.wkbType())
            geom_has_z = QgsWkbTypes.hasZ(geometry.wkbType())
            if layer_has_z and not geom_has_z:
                warnings.append({"index": index, "issue": "2D geometry on a Z layer"})
    except Exception:  # noqa: BLE001
        pass
    return geometry


def _coerce_value(field, value):
    if value is None:
        return None
    kind = compat.field_type_name(field)
    if kind in ("int", "int64"):
        if isinstance(value, str) and value.strip() == "":
            return None
        return int(float(value)) if not isinstance(value, int) else int(value)
    if kind == "double":
        if isinstance(value, str) and value.strip() == "":
            return None
        return float(value)
    if kind == "bool":
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes", "y", "on"):
                return True
            if lowered in ("false", "0", "no", "n", "off", ""):
                return False
        return bool(value)
    if kind in ("date", "datetime", "time") and isinstance(value, str):
        from qgis.PyQt.QtCore import QDate, QDateTime, QTime
        text = value.strip()
        if kind == "date":
            for fmt in ("yyyy-MM-dd", "yyyy/MM/dd", "dd.MM.yyyy"):
                date = QDate.fromString(text, fmt)
                if date.isValid():
                    return date
        elif kind == "datetime":
            for fmt in ("yyyy-MM-ddTHH:mm:ss", "yyyy-MM-dd HH:mm:ss",
                        "yyyy-MM-ddTHH:mm:ss.zzz", "yyyy-MM-dd"):
                dt = QDateTime.fromString(text, fmt)
                if dt.isValid():
                    return dt
        else:
            for fmt in ("HH:mm:ss", "HH:mm"):
                t = QTime.fromString(text, fmt)
                if t.isValid():
                    return t
        return value
    return value


def _aggregate_calculator(layer):
    try:
        from qgis.core import QgsAggregateCalculator
        return QgsAggregateCalculator(layer)
    except Exception:  # noqa: BLE001
        return None


def _aggregate_stat(calculator, layer, field_name, stat):
    if calculator is not None:
        name = {"count": "Count", "count_distinct": "CountDistinct",
                "count_missing": "CountMissing", "min": "Min", "max": "Max",
                "sum": "Sum", "mean": "Mean", "stdev": "StDev",
                "stdev_sample": "StDevSample", "median": "Median",
                "range": "Range", "first_quartile": "FirstQuartile",
                "third_quartile": "ThirdQuartile",
                "inter_quartile_range": "InterQuartileRange"}.get(stat)
        if name is None:
            return None
        from qgis.core import QgsAggregateCalculator as AC
        enum_value = compat.resolve(AC, "Aggregate.%s" % name, name, default=None)
        if enum_value is not None:
            try:
                value, ok = calculator.calculate(enum_value, str(field_name))
                return value_to_json(value) if ok else None
            except Exception:  # noqa: BLE001
                pass
    # manual fallback
    try:
        index = layer.fields().indexFromName(str(field_name))
    except Exception:  # noqa: BLE001
        return None
    values = []
    for feature in layer.getFeatures():
        value = feature[index]
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if stat == "count":
        return len(values)
    if not values:
        return None
    return {
        "min": min(values), "max": max(values), "sum": sum(values),
        "mean": sum(values) / len(values), "range": max(values) - min(values),
    }.get(stat)
