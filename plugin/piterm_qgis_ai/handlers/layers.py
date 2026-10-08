"""Layer commands: CRUD, ordering, groups, CRS, fields, joins, export, stats.

This is the largest handler group on purpose - layer management is where an AI
spends most of its calls, so every common operation is a first-class command
instead of an ``execute_code`` escape hatch.
"""

from __future__ import annotations

import os
import urllib.parse

from qgis.core import (QgsField, QgsLayerTreeGroup, QgsProject, QgsRasterLayer,
                       QgsRectangle, QgsVectorLayer)

from .. import compat
from ..errors import bad_request, invalid, io_error, not_found, state
from ..registry import command
from .base import HandlerBase, field_to_json, value_to_json


class LayerHandlers(HandlerBase):

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    @command(name="layers.list", summary="List layers with summary info",
             risk="read", tags=("layers",))
    def layers_list(self, kind=None, with_extent=True, with_counts=False):
        out = []
        for layer in self.project.mapLayers().values():
            if kind and compat.layer_kind(layer) != kind:
                continue
            summary = self.layer_summary(layer, with_extent=with_extent)
            if not with_counts:
                summary.pop("feature_count", None)
            out.append(summary)
        return {"count": len(out), "layers": out}

    @command(name="layers.tree", summary="Layer tree (groups + layers, recursive)",
             risk="read", tags=("layers",))
    def layers_tree(self):
        root = self.project.layerTreeRoot()

        def walk(node):
            if isinstance(node, QgsLayerTreeGroup):
                return {
                    "type": "group",
                    "name": node.name(),
                    "visible": node.isVisible(),
                    "expanded": node.isExpanded(),
                    "children": [walk(child) for child in node.children()],
                }
            layer = node.layer()
            return {
                "type": "layer",
                "name": node.name(),
                "id": layer.id() if layer else None,
                "kind": compat.layer_kind(layer) if layer else "unknown",
                "visible": node.isVisible(),
            }

        return {"tree": [walk(child) for child in root.children()]}

    @command(name="layers.find", summary="Find layers by name pattern "
             "(substring or fnmatch)", risk="read", tags=("layers",))
    def layers_find(self, pattern, kind=None, limit=50):
        import fnmatch
        text = str(pattern)
        out = []
        for layer in self.project.mapLayers().values():
            name = layer.name()
            if (text.casefold() in name.casefold()
                    or fnmatch.fnmatch(name, text)):
                if kind and compat.layer_kind(layer) != kind:
                    continue
                out.append(self.layer_summary(layer, with_extent=False))
        return {"count": len(out), "layers": out[:max(1, int(limit))]}

    @command(name="layers.info", summary="Detailed info for one layer (fields, "
             "editing, styles, joins)", risk="read", tags=("layers",))
    def layers_info(self, layer, with_fields=True):
        target = self.resolve_layer({"layer": layer})
        info = self.layer_summary(target)
        if with_fields and compat.layer_kind(target) == "vector":
            info["fields_detail"] = [field_to_json(f) for f in target.fields()]
            info["joins"] = [{"join_layer_id": j.joinLayerId(),
                              "join_field": j.joinFieldName(),
                              "target_field": j.targetFieldName(),
                              "prefix": j.prefix()}
                             for j in target.vectorJoins()]
            try:
                info["subset_string"] = target.subsetString()
            except Exception:  # noqa: BLE001
                pass
            try:
                info["undo_stack"] = {"count": target.undoStack().count(),
                                      "index": target.undoStack().index()}
            except Exception:  # noqa: BLE001
                pass
        return info

    @command(name="layers.count", summary="Feature count (optionally with an "
             "expression filter)", risk="read", tags=("layers",))
    def layers_count(self, layer, expression=None):
        target = self.resolve_vector({"layer": layer})
        if expression:
            request = self.feature_request(target, expression=expression,
                                           no_geometry=True, fields=[])
            return {"count": sum(1 for _ in target.getFeatures(request)),
                    "expression": expression}
        return {"count": target.featureCount(), "expression": None}

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    @command(name="layers.add_vector", summary="Add a vector layer from a path "
             "or datasource URI", risk="write", tags=("layers",))
    def layers_add_vector(self, path, name=None, provider="ogr"):
        source = str(path)
        if "|" not in source and ":" not in source.split("//")[0][:10]:
            source = self.check_path(source, for_write=False)
        display = name or os.path.splitext(os.path.basename(source.split("|")[0]))[0]
        layer = QgsVectorLayer(source, display, provider)
        if not layer.isValid():
            raise invalid("could not load vector layer: %s" % source,
                          detail={"provider": provider,
                                  "hint": "check the path/URI and provider"})
        self.project.addMapLayer(layer)
        return {"layer": self.layer_summary(layer, with_extent=False)}

    @command(name="layers.add_raster", summary="Add a raster layer from a path",
             risk="write", tags=("layers",))
    def layers_add_raster(self, path, name=None, provider="gdal"):
        source = self.check_path(path, for_write=False)
        display = name or os.path.splitext(os.path.basename(source))[0]
        layer = QgsRasterLayer(source, display, provider)
        if not layer.isValid():
            raise invalid("could not load raster layer: %s" % source,
                          detail={"provider": provider})
        self.project.addMapLayer(layer)
        return {"layer": self.layer_summary(layer, with_extent=False)}

    @command(name="layers.add_web", summary="Add an XYZ / WMS / WMTS tile layer "
             "or a WFS layer", risk="write", tags=("layers", "web"))
    def layers_add_web(self, url, kind="xyz", name=None, crs=None,
                       zmin=0, zmax=19):
        kind = str(kind).lower()
        display = name or "web layer"
        if kind == "xyz":
            encoded = urllib.parse.quote(str(url), safe="")
            uri = "type=xyz&zmin=%d&zmax=%d&url=%s" % (int(zmin), int(zmax), encoded)
            layer = QgsRasterLayer(uri, display, "wms")
        elif kind == "wms":
            layer = QgsRasterLayer(str(url), display, "wms")
        elif kind == "wmts":
            layer = QgsRasterLayer(str(url), display, "wmts")
        elif kind == "wfs":
            layer = QgsVectorLayer(str(url), display, "wfs")
        else:
            raise bad_request("unknown web layer kind %r" % kind,
                              detail={"accepted": ["xyz", "wms", "wmts", "wfs"]})
        if not layer.isValid():
            raise invalid("web layer is not valid: %s" % url,
                          detail={"kind": kind})
        if crs:
            value = self.resolve_crs(crs)
            try:
                layer.setCrs(value)
            except Exception:  # noqa: BLE001
                pass
        self.project.addMapLayer(layer)
        return {"layer": self.layer_summary(layer, with_extent=False)}

    @command(name="layers.add_memory", summary="Create an in-memory vector layer "
             "(fields optional)", risk="write", tags=("layers",))
    def layers_add_memory(self, name, geometry_type="point", crs="EPSG:4326",
                          fields=None):
        type_token = _memory_geometry_token(geometry_type)
        crs_value = self.resolve_crs(crs)
        uri = "%s?crs=%s" % (type_token, crs_value.authid().replace(":", ":"))
        layer = QgsVectorLayer(uri, str(name), "memory")
        if not layer.isValid():
            raise invalid("could not create memory layer (%s)" % uri)
        self.project.addMapLayer(layer)
        added_fields = []
        if fields:
            new_fields = []
            for spec in fields:
                if isinstance(spec, str):
                    spec = {"name": spec, "type": "string"}
                field = QgsField(str(spec.get("name")),
                                 compat.field_type_enum(spec.get("type", "string")),
                                 "", int(spec.get("length") or 0),
                                 int(spec.get("precision") or 0))
                if spec.get("alias"):
                    field.setAlias(str(spec["alias"]))
                new_fields.append(field)
                added_fields.append(str(spec.get("name")))
            with self.edit_scope(layer, "add fields"):
                layer.dataProvider().addAttributes(new_fields)
                layer.updateFields()
        return {"layer": self.layer_summary(layer, with_extent=False),
                "fields": added_fields}

    @command(name="layers.add_delimited", summary="Add a delimited text layer "
             "(CSV/TXT) from x/y columns", risk="write", tags=("layers",))
    def layers_add_delimited(self, path, x_field, y_field, delimiter=",",
                             crs="EPSG:4326", name=None):
        source = self.check_path(path, for_write=False)
        crs_value = self.resolve_crs(crs)
        query = urllib.parse.urlencode({
            "delimiter": delimiter, "xField": x_field, "yField": y_field,
            "crs": crs_value.authid(),
        })
        uri = "file:///%s?%s" % (source.replace("\\", "/"), query)
        display = name or os.path.splitext(os.path.basename(source))[0]
        layer = QgsVectorLayer(uri, display, "delimitedtext")
        if not layer.isValid():
            raise invalid("could not load delimited text layer",
                          detail={"uri": uri[:200]})
        self.project.addMapLayer(layer)
        return {"layer": self.layer_summary(layer, with_extent=False)}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    @command(name="layers.remove", summary="Remove a layer from the project",
             risk="destructive", tags=("layers",))
    def layers_remove(self, layer):
        target = self.resolve_layer({"layer": layer})
        layer_id = target.id()
        name = target.name()
        self.project.removeMapLayer(layer_id)
        return {"removed": layer_id, "name": name}

    @command(name="layers.duplicate", summary="Duplicate a layer (shares the "
             "data source)", risk="write", tags=("layers",))
    def layers_duplicate(self, layer, new_name=None):
        target = self.resolve_layer({"layer": layer})
        clone = target.clone()
        clone.setName(new_name or ("%s (copy)" % target.name()))
        self.project.addMapLayer(clone)
        return {"layer": self.layer_summary(clone, with_extent=False)}

    @command(name="layers.rename", summary="Rename a layer in the project",
             risk="write", tags=("layers",))
    def layers_rename(self, layer, name):
        target = self.resolve_layer({"layer": layer})
        target.setName(str(name))
        return {"renamed": target.id(), "name": target.name()}

    @command(name="layers.visibility", summary="Show/hide a layer in the layer "
             "tree", risk="write", tags=("layers",))
    def layers_visibility(self, layer, visible=True):
        target = self.resolve_layer({"layer": layer})
        node = self.project.layerTreeRoot().findLayer(target.id())
        if node is None:
            raise not_found("layer is not in the layer tree: %s" % target.name())
        setter = getattr(node, "setItemVisibilityChecked", None)
        (setter if setter else node.setVisible)(bool(visible))
        return {"layer": target.id(), "visible": bool(visible)}

    @command(name="layers.opacity", summary="Set layer opacity (0..1)",
             risk="write", tags=("layers",))
    def layers_opacity(self, layer, opacity):
        target = self.resolve_layer({"layer": layer})
        value = float(opacity)
        if value > 1.0:
            value = value / 100.0
        if not 0.0 <= value <= 1.0:
            raise invalid("opacity must be within 0..1 (or 0..100)")
        target.setOpacity(value)
        return {"layer": target.id(), "opacity": value}

    @command(name="layers.set_property", summary="Set name/opacity/scale range "
             "on a layer in one call", risk="write", tags=("layers",))
    def layers_set_property(self, layer, name=None, opacity=None,
                            min_scale=None, max_scale=None):
        target = self.resolve_layer({"layer": layer})
        changed = {}
        if name is not None:
            target.setName(str(name))
            changed["name"] = str(name)
        if opacity is not None:
            value = float(opacity)
            if value > 1.0:
                value = value / 100.0
            target.setOpacity(value)
            changed["opacity"] = value
        if min_scale is not None or max_scale is not None:
            if min_scale is not None:
                target.setMinimumScale(float(min_scale))
                changed["min_scale"] = float(min_scale)
            if max_scale is not None:
                target.setMaximumScale(float(max_scale))
                changed["max_scale"] = float(max_scale)
            target.triggerRepaint()
        return {"layer": target.id(), "changed": changed}

    @command(name="layers.order", summary="Set layer draw order (list of layer "
             "ids, top to bottom)", risk="write", tags=("layers",))
    def layers_order(self, layer_ids):
        if not isinstance(layer_ids, (list, tuple)) or not layer_ids:
            raise bad_request("layer_ids must be a non-empty list")
        root = self.project.layerTreeRoot()
        nodes = []
        for lid in layer_ids:
            node = root.findLayer(str(lid))
            if node is None:
                raise not_found("no layer tree node for id %r" % lid)
            nodes.append(node)
        for node in nodes:
            parent = node.parent()
            if parent is not None:
                parent.removeChildNode(node)
        for index, node in enumerate(nodes):
            root.insertChildNode(index, node)
        return {"order": [str(lid) for lid in layer_ids]}

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    @command(name="layers.group_create", summary="Create a layer group (optionally "
             "inside another group)", risk="write", tags=("layers", "groups"))
    def layers_group_create(self, name, parent_group=None, index=0):
        root = self.project.layerTreeRoot()
        parent = _find_group(root, parent_group) if parent_group else root
        if parent is None:
            raise not_found("no group named %r" % parent_group)
        group = QgsLayerTreeGroup(str(name))
        parent.insertChildNode(int(index), group)
        return {"group": str(name), "parent": parent.name(),
                "path": _node_path(group)}

    @command(name="layers.group_move_layer", summary="Move a layer into a group",
             risk="write", tags=("layers", "groups"))
    def layers_group_move_layer(self, layer, group, index=0):
        root = self.project.layerTreeRoot()
        target = self.resolve_layer({"layer": layer})
        node = root.findLayer(target.id())
        if node is None:
            raise not_found("layer is not in the layer tree")
        parent = _find_group(root, group)
        if parent is None:
            raise not_found("no group named %r" % group)
        old_parent = node.parent()
        if old_parent is not None:
            old_parent.removeChildNode(node)
        parent.insertChildNode(int(index), node)
        return {"moved": target.id(), "into": _node_path(parent)}

    @command(name="layers.group_remove", summary="Remove a layer group (optionally "
             "its layers too)", risk="destructive", tags=("layers", "groups"))
    def layers_group_remove(self, group, also_remove_layers=False):
        root = self.project.layerTreeRoot()
        node = _find_group(root, group)
        if node is None:
            raise not_found("no group named %r" % group)
        layer_ids = [l.id() for l in node.findLayers()] if also_remove_layers else []
        parent = node.parent()
        if parent is not None:
            parent.removeChildNode(node)
        if layer_ids:
            self.project.removeMapLayers(layer_ids)
        return {"removed": str(group), "removed_layers": layer_ids}

    # ------------------------------------------------------------------
    # CRS / source / filters
    # ------------------------------------------------------------------
    @command(name="layers.extent", summary="Layer extent (optionally in project "
             "CRS)", risk="read", tags=("layers",))
    def layers_extent(self, layer, project_crs=False):
        target = self.resolve_layer({"layer": layer})
        rect = QgsRectangle(target.extent())
        crs = target.crs()
        if project_crs:
            dst = self.project.crs()
            if crs.isValid() and crs.authid() != dst.authid():
                rect = compat.transform_rect(rect, crs, dst,
                                             self.project.transformContext())
                crs = dst
        return {"extent": compat.rect_to_dict(rect), "crs": crs.authid()}

    @command(name="layers.crs_get", summary="Layer CRS details", risk="read",
             tags=("layers", "crs"))
    def layers_crs_get(self, layer):
        target = self.resolve_layer({"layer": layer})
        crs = target.crs()
        return {"layer": target.id(), "authid": crs.authid(),
                "description": crs.description(),
                "geographic": crs.isGeographic(),
                "proj4": crs.toProj4()[:200]}

    @command(name="layers.crs_set", summary="Reinterpret a layer's CRS "
             "(does NOT reproject geometry)", risk="write", tags=("layers", "crs"))
    def layers_crs_set(self, layer, crs):
        target = self.resolve_layer({"layer": layer})
        value = self.resolve_crs(crs)
        target.setCrs(value)
        target.triggerRepaint()
        return {"layer": target.id(), "crs": value.authid(),
                "note": "coordinates were reinterpreted, not transformed"}

    @command(name="layers.source", summary="Layer datasource URI / public source",
             risk="read", tags=("layers",))
    def layers_source(self, layer):
        target = self.resolve_layer({"layer": layer})
        return {"layer": target.id(),
                "source": target.source(),
                "public_source": target.publicSource(),
                "provider": target.providerType()}

    @command(name="layers.set_source", summary="Swap a layer's datasource "
             "(changeDataSource)", risk="destructive", tags=("layers",))
    def layers_set_source(self, layer, source):
        target = self.resolve_layer({"layer": layer})
        src = str(source)
        ok = target.setDataSource(src, target.name(), target.providerType())
        if not ok:
            raise invalid("setDataSource failed", detail={"source": src[:200]})
        target.triggerRepaint()
        return {"layer": target.id(), "source": target.source()}

    @command(name="layers.subset_filter_set", summary="Apply a subset (filter) "
             "string to a vector layer", risk="write", tags=("layers",))
    def layers_subset_filter_set(self, layer, filter, expression=None):
        target = self.resolve_vector({"layer": layer})
        text = filter if filter is not None else expression
        if text is None:
            raise bad_request("pass filter=... (QGIS expression)")
        target.setSubsetString(str(text))
        return {"layer": target.id(), "subset": str(text)}

    @command(name="layers.subset_filter_clear", summary="Clear the subset filter",
             risk="write", tags=("layers",))
    def layers_subset_filter_clear(self, layer):
        target = self.resolve_vector({"layer": layer})
        target.setSubsetString("")
        return {"layer": target.id(), "subset": ""}

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    @command(name="layers.fields_list", summary="Field schema of a vector layer",
             risk="read", tags=("layers", "fields"))
    def layers_fields_list(self, layer):
        target = self.resolve_vector({"layer": layer})
        return {"layer": target.id(),
                "fields": [field_to_json(f) for f in target.fields()]}

    @command(name="layers.fields_add", summary="Add a field (string, int, double, "
             "bool, date, datetime, time, binary, json)", risk="write",
             tags=("layers", "fields"))
    def layers_fields_add(self, layer, name, type="string", length=None,
                          precision=None, alias=None, comment=None):
        target = self.resolve_vector({"layer": layer})
        field = QgsField(str(name), compat.field_type_enum(type), "",
                         int(length or 0), int(precision or 0))
        if alias:
            field.setAlias(str(alias))
        if comment:
            field.setComment(str(comment))
        with self.edit_scope(target, "add field %s" % name):
            target.dataProvider().addAttributes([field])
            target.updateFields()
        return {"layer": target.id(), "field": field_to_json(field)}

    @command(name="layers.fields_rename", summary="Rename a field",
             risk="write", tags=("layers", "fields"))
    def layers_fields_rename(self, layer, name, new_name):
        target = self.resolve_vector({"layer": layer})
        index = target.fields().indexFromName(str(name))
        if index < 0:
            self.field_index(target, name)
            index = target.fields().indexFromName(str(name))
        with self.edit_scope(target, "rename field %s" % name):
            if not target.renameAttribute(index, str(new_name)):
                raise invalid("renameAttribute failed")
        return {"layer": target.id(), "renamed": str(name), "to": str(new_name)}

    @command(name="layers.fields_delete", summary="Delete a field (and its data)",
             risk="destructive", tags=("layers", "fields"))
    def layers_fields_delete(self, layer, name):
        target = self.resolve_vector({"layer": layer})
        index = target.fields().indexFromName(str(name))
        if index < 0:
            raise not_found("no field %r" % name)
        with self.edit_scope(target, "delete field %s" % name):
            if not target.deleteAttribute(index):
                raise invalid("deleteAttribute failed")
        return {"layer": target.id(), "deleted": str(name)}

    # ------------------------------------------------------------------
    # Joins
    # ------------------------------------------------------------------
    @command(name="layers.joins_list", summary="Attribute joins on a layer",
             risk="read", tags=("layers", "joins"))
    def layers_joins_list(self, layer):
        target = self.resolve_vector({"layer": layer})
        joins = []
        for join in target.vectorJoins():
            joins.append({
                "join_layer_id": join.joinLayerId(),
                "join_field": join.joinFieldName(),
                "target_field": join.targetFieldName(),
                "prefix": join.prefix(),
                "memory_cache": bool(getattr(join, "isUsingMemoryCache", lambda: False)()),
            })
        return {"layer": target.id(), "joins": joins}

    @command(name="layers.joins_add", summary="Join another layer's attributes "
             "onto a vector layer", risk="write", tags=("layers", "joins"))
    def layers_joins_add(self, layer, join_layer, join_field, target_field=None,
                         prefix="", memory_cache=True):
        target = self.resolve_vector({"layer": layer})
        source = self.resolve_layer({"layer": join_layer})
        if compat.layer_kind(source) != "vector":
            raise invalid("join source must be a vector layer")
        target_field = target_field or join_field
        self.field_index(target, target_field)
        self.field_index(source, join_field)
        join_cls = _join_info_class()
        info = join_cls()
        info.setJoinLayer(source)
        info.setJoinFieldName(str(join_field))
        info.setTargetFieldName(str(target_field))
        info.setPrefix(str(prefix or ""))
        for setter in ("setUsingMemoryCache", "setMemoryCache"):
            fn = getattr(info, setter, None)
            if fn is not None:
                try:
                    fn(bool(memory_cache))
                    break
                except Exception:  # noqa: BLE001
                    continue
        target.addJoin(info)
        return {"layer": target.id(), "join": source.id(),
                "join_field": str(join_field), "target_field": str(target_field)}

    @command(name="layers.joins_remove", summary="Remove a join by join-layer id",
             risk="write", tags=("layers", "joins"))
    def layers_joins_remove(self, layer, join_layer_id=None, join_layer=None):
        target = self.resolve_vector({"layer": layer})
        jid = join_layer_id
        if jid is None and join_layer is not None:
            jid = self.resolve_layer({"layer": join_layer}).id()
        if not jid:
            raise bad_request("pass join_layer_id or join_layer")
        target.removeJoin(str(jid))
        return {"layer": target.id(), "removed_join": str(jid)}

    # ------------------------------------------------------------------
    # Export & values
    # ------------------------------------------------------------------
    @command(name="layers.export", summary="Export a layer to disk (vector or "
             "raster; optional reprojection/filter)", risk="write",
             tags=("layers", "io"))
    def layers_export(self, layer, path, driver=None, crs=None,
                      filter=None, selected_only=False, overwrite=True):
        from .io import export_layer_impl
        target = self.resolve_layer({"layer": layer})
        out = self.check_path(path, for_write=True)
        return export_layer_impl(self, target, out, driver=driver, crs=crs,
                                 filter_expression=filter,
                                 selected_only=bool(selected_only),
                                 overwrite=bool(overwrite))

    @command(name="layers.unique_values", summary="Distinct values of a field",
             risk="read", tags=("layers",))
    def layers_unique_values(self, layer, field, limit=100):
        target = self.resolve_vector({"layer": layer})
        name = self.field_index(target, field)
        index = target.fields().indexFromName(name)
        values = target.dataProvider().uniqueValues(index, int(limit))
        out = [value_to_json(v) for v in values]
        try:
            out.sort(key=lambda v: (v is None, str(v)))
        except Exception:  # noqa: BLE001
            pass
        return {"layer": target.id(), "field": name, "count": len(out),
                "values": out[: int(limit) if int(limit) > 0 else len(out)]}

    # ------------------------------------------------------------------
    # Active layer
    # ------------------------------------------------------------------
    @command(name="layers.active_get", summary="Currently active layer in the "
             "layer panel", risk="read", tags=("layers",))
    def layers_active_get(self):
        iface = self.iface
        layer = iface.activeLayer() if iface else None
        if layer is None:
            return {"active": None}
        return {"active": self.layer_summary(layer, with_extent=False)}

    @command(name="layers.active_set", summary="Set the active layer by id or name",
             risk="write", tags=("layers",))
    def layers_active_set(self, layer):
        target = self.resolve_layer({"layer": layer})
        iface = self.iface
        if iface is None:
            raise state("no GUI session; cannot set the active layer")
        iface.setActiveLayer(target)
        return {"active": target.id(), "name": target.name()}

    @command(name="layers.zoom_to", summary="Zoom the canvas to a layer extent",
             risk="read", tags=("layers",))
    def layers_zoom_to(self, layer, margin=0.02):
        target = self.resolve_layer({"layer": layer})
        canvas = self.canvas
        rect = QgsRectangle(target.extent())
        dst = canvas.mapSettings().destinationCrs()
        crs = target.crs()
        if crs.isValid() and crs.authid() != dst.authid():
            rect = compat.transform_rect(rect, crs, dst,
                                         self.project.transformContext())
        if rect.isNull() or rect.isEmpty():
            raise invalid("layer has no extent to zoom to")
        try:
            factor = 1.0 + max(0.0, float(margin or 0.0))
            rect = QgsRectangle(
                rect.center().x() - rect.width() * factor / 2,
                rect.center().y() - rect.height() * factor / 2,
                rect.center().x() + rect.width() * factor / 2,
                rect.center().y() + rect.height() * factor / 2)
        except Exception:  # noqa: BLE001
            pass
        canvas.setExtent(rect)
        canvas.refresh()
        return {"zoomed_to": target.id(), "extent": compat.rect_to_dict(rect)}


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------
def _memory_geometry_token(geometry_type):
    token = str(geometry_type or "point").strip()
    table = {
        "point": "Point", "line": "LineString", "linestring": "LineString",
        "polygon": "Polygon", "multipoint": "MultiPoint",
        "multiline": "MultiLineString", "multilinestring": "MultiLineString",
        "multipolygon": "MultiPolygon", "none": "None",
    }
    return table.get(token.casefold(), token)


def _find_group(root, name):
    if name is None:
        return root
    target = str(name)
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, QgsLayerTreeGroup):
            if node.name() == target:
                return node
            stack.extend(node.children())
    return None


def _node_path(node):
    parts = []
    current = node
    while current is not None and hasattr(current, "name"):
        parts.append(current.name())
        current = current.parent()
    return "/".join(reversed(parts))


def _join_info_class():
    try:
        from qgis.core import QgsVectorLayerJoinInfo as cls
        return cls
    except ImportError:
        from qgis.core import QgsVectorJoinInfo as cls  # type: ignore
        return cls
