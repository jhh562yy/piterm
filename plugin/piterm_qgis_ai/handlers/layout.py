"""Print layouts and atlas export: page setup, item authoring and PDF/PNG/SVG
export via QgsLayoutExporter."""

from __future__ import annotations

import os

from qgis.core import (QgsLayoutExporter, QgsLayoutItemAttributeTable,
                       QgsLayoutItemLabel, QgsLayoutItemLegend,
                       QgsLayoutItemMap, QgsLayoutItemPicture,
                       QgsLayoutItemScaleBar, QgsLayoutItemShape,
                       QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout,
                       QgsProject, QgsRectangle, QgsUnitTypes)

from .. import compat
from ..errors import bad_request, invalid, not_found
from ..registry import command
from .base import HandlerBase


class LayoutHandlers(HandlerBase):

    @command(name="layout.list", summary="Print layouts in the project",
             risk="read", tags=("layout",))
    def layout_list(self):
        manager = QgsProject.instance().layoutManager()
        out = []
        for name in manager.printLayouts():
            layout = name if isinstance(name, QgsPrintLayout) else None
            if layout is None:
                continue
            out.append(_layout_summary(layout))
        return {"count": len(out), "layouts": out}

    @command(name="layout.create", summary="Create a print layout (page preset "
             "or custom mm size)", risk="write", tags=("layout",))
    def layout_create(self, name, page="A4", width=None, height=None,
                      orientation="portrait"):
        project = QgsProject.instance()
        manager = project.layoutManager()
        for existing in manager.printLayouts():
            if existing.name() == str(name):
                raise invalid("a layout named %r already exists" % name)
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName(str(name))
        size = _page_size(page, orientation, width, height)
        if size is not None:
            try:
                layout.pageCollection().page(0).setPageSize(size)
            except Exception as exc:  # noqa: BLE001
                raise invalid("could not set page size: %s" % exc)
        manager.addLayout(layout)
        return _layout_summary(layout)

    @command(name="layout.remove", summary="Remove a print layout",
             risk="destructive", tags=("layout",))
    def layout_remove(self, name):
        manager = QgsProject.instance().layoutManager()
        layout = _find_layout(manager, name)
        manager.removeLayout(layout)
        return {"removed": str(name)}

    @command(name="layout.info", summary="Layout items (type, id, position, "
             "size)", risk="read", tags=("layout",))
    def layout_info(self, name=None):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        items = []
        try:
            iterator = layout.items()
        except Exception:  # noqa: BLE001
            iterator = []
        for item in iterator:
            entry = {"id": item.id(), "type": type(item).__name__}
            for attr in ("x", "y", "width", "height"):
                fn = getattr(item, attr, None)
                if callable(fn):
                    try:
                        entry[attr] = round(float(fn()), 2)
                    except Exception:  # noqa: BLE001
                        pass
            if isinstance(item, QgsLayoutItemMap):
                try:
                    entry["extent"] = compat.rect_to_dict(item.extent())
                except Exception:  # noqa: BLE001
                    pass
            items.append(entry)
        return {"layout": layout.name(), "items": items,
                "count": len(items)}

    @command(name="layout.add_map", summary="Add a map item (position/size in "
             "mm, extent optional)", risk="write", tags=("layout",))
    def layout_add_map(self, name, x=10, y=10, width=180, height=120,
                       bbox=None, map_id="main_map"):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        item = QgsLayoutItemMap(layout)
        item.setId(str(map_id))
        _place(item, x, y, width, height)
        layout.addLayoutItem(item)
        rect = None
        if bbox is not None:
            rect = self.bbox_from_params({"bbox": bbox}, "bbox",
                                         crs=self.project.crs())
        if rect is None:
            try:
                rect = QgsRectangle(self.canvas.extent())
            except Exception:  # noqa: BLE001
                rect = None
        if rect is not None:
            try:
                item.setExtent(rect)
            except Exception as exc:  # noqa: BLE001
                raise invalid("could not set map extent: %s" % exc)
        return {"layout": layout.name(), "item": item.id(),
                "extent": compat.rect_to_dict(rect) if rect else None}

    @command(name="layout.add_label", summary="Add a text label ([% expr %] "
             "supported)", risk="write", tags=("layout",))
    def layout_add_label(self, name, text, x=10, y=10, font_size=10,
                         width=None, height=None, label_id=""):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        item = QgsLayoutItemLabel(layout)
        item.setText(str(text))
        try:
            item.setFontSize(float(font_size))
        except Exception:  # noqa: BLE001
            pass
        if label_id:
            item.setId(str(label_id))
        _place(item, x, y, width, height or max(8.0, float(font_size) * 1.8))
        layout.addLayoutItem(item)
        return {"layout": layout.name(), "item": item.id()}

    @command(name="layout.add_legend", summary="Add a legend linked to a map "
             "item", risk="write", tags=("layout",))
    def layout_add_legend(self, name, x=10, y=140, width=60, height=60,
                          map_id="main_map", title=None, legend_id=""):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        item = QgsLayoutItemLegend(layout)
        if legend_id:
            item.setId(str(legend_id))
        linked = _find_map_item(layout, map_id)
        if linked is not None:
            try:
                item.setLinkedMap(linked)
            except Exception:  # noqa: BLE001
                pass
        if title:
            try:
                item.setTitleEnabled(True)
                item.setTitle(str(title))
            except Exception:  # noqa: BLE001
                pass
        _place(item, x, y, width, height)
        layout.addLayoutItem(item)
        return {"layout": layout.name(), "item": item.id(),
                "linked_map": map_id if linked is not None else None}

    @command(name="layout.add_scalebar", summary="Add a scale bar linked to a "
             "map item", risk="write", tags=("layout",))
    def layout_add_scalebar(self, name, x=10, y=270, width=60, height=12,
                            map_id="main_map", style=None, scalebar_id=""):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        item = QgsLayoutItemScaleBar(layout)
        if scalebar_id:
            item.setId(str(scalebar_id))
        linked = _find_map_item(layout, map_id)
        if linked is not None:
            for setter in ("setLinkedMap", "setLinkedMapItem"):
                fn = getattr(item, setter, None)
                if callable(fn):
                    try:
                        fn(linked)
                        break
                    except Exception:  # noqa: BLE001
                        continue
        if style:
            for setter in ("setStyle", "applyDefaultStyle"):
                fn = getattr(item, setter, None)
                if callable(fn):
                    try:
                        fn(str(style))
                        break
                    except Exception:  # noqa: BLE001
                        continue
        for method in ("applyDefaultSize", "update"):
            fn = getattr(item, method, None)
            if callable(fn):
                try:
                    fn()
                    break
                except Exception:  # noqa: BLE001
                    continue
        _place(item, x, y, width, height)
        layout.addLayoutItem(item)
        return {"layout": layout.name(), "item": item.id()}

    @command(name="layout.add_picture", summary="Add a picture/SVG (logo, north "
             "arrow)", risk="write", tags=("layout",))
    def layout_add_picture(self, name, x=10, y=10, width=20, height=20,
                           path=None, kind="north_arrow", picture_id=""):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        item = QgsLayoutItemPicture(layout)
        if picture_id:
            item.setId(str(picture_id))
        if path:
            source = self.check_path(path, for_write=False)
        else:
            presets = {
                "north_arrow": ":/images/north_arrows/layout_default_north_arrow.svg",
                "logo": ":/images/logo/qgis_logo.svg",
            }
            source = presets.get(str(kind or "north_arrow").lower(),
                                 presets["north_arrow"])
        try:
            item.setPicturePath(source)
        except Exception as exc:  # noqa: BLE001
            raise invalid("could not set picture: %s" % exc)
        _place(item, x, y, width, height)
        layout.addLayoutItem(item)
        return {"layout": layout.name(), "item": item.id(), "source": source}

    @command(name="layout.add_table", summary="Add an attribute table for a "
             "layer", risk="write", tags=("layout",))
    def layout_add_table(self, name, layer, x=10, y=100, width=100, height=80,
                         max_features=100, table_id=""):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        target = self.resolve_vector({"layer": layer})
        item = QgsLayoutItemAttributeTable(layout)
        if table_id:
            item.setId(str(table_id))
        item.setVectorLayer(target)
        for setter, value in (("setMaximumNumberOfFeatures", int(max_features)),):
            fn = getattr(item, setter, None)
            if callable(fn):
                try:
                    fn(value)
                except Exception:  # noqa: BLE001
                    pass
        _place(item, x, y, width, height)
        layout.addLayoutItem(item)
        return {"layout": layout.name(), "item": item.id(), "layer": target.id()}

    @command(name="layout.add_shape", summary="Add a rectangle/ellipse/triangle "
             "shape", risk="write", tags=("layout",))
    def layout_add_shape(self, name, kind="rectangle", x=10, y=10, width=50,
                         height=30, shape_id=""):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        item = QgsLayoutItemShape(layout)
        if shape_id:
            item.setId(str(shape_id))
        enum_value = compat.resolve(
            QgsLayoutItemShape, "Shape.%s" % str(kind).capitalize(),
            "Shape.%s" % str(kind).lower(), "ShapeType.%s" % str(kind).capitalize(),
            str(kind).capitalize(), default=None)
        if enum_value is not None:
            try:
                item.setShapeType(enum_value)
            except Exception:  # noqa: BLE001
                pass
        _place(item, x, y, width, height)
        layout.addLayoutItem(item)
        return {"layout": layout.name(), "item": item.id(), "shape": str(kind)}

    @command(name="layout.item_move_resize", summary="Move/resize a layout item "
             "(mm)", risk="write", tags=("layout",))
    def layout_item_move_resize(self, name, item_id, x=None, y=None, width=None,
                                height=None):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        item = layout.itemById(str(item_id))
        if item is None:
            raise not_found("no layout item %r" % item_id,
                            detail={"items": [i.id() for i in layout.items()][:50]})
        units = _mm_units()
        changed = {}
        if x is not None or y is not None:
            current_x, current_y = item.x(), item.y()
            point = QgsLayoutPoint(float(current_x if x is None else x),
                                   float(current_y if y is None else y), units)
            item.attemptMove(point)
            changed["position"] = [item.x(), item.y()]
        if width is not None or height is not None:
            current_w, current_h = item.rect().width(), item.rect().height()
            size = QgsLayoutSize(float(current_w if width is None else width),
                                 float(current_h if height is None else height),
                                 units)
            item.attemptResize(size)
            changed["size"] = [item.rect().width(), item.rect().height()]
        return {"layout": layout.name(), "item": str(item_id), "changed": changed}

    @command(name="layout.export", summary="Export a layout to PDF/PNG/SVG",
             risk="write", tags=("layout", "io"))
    def layout_export(self, name, path, format=None, dpi=300):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        out = self.check_path(path, for_write=True)
        directory = os.path.dirname(out)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fmt = (format or os.path.splitext(out)[1].lstrip(".") or "pdf").lower()
        exporter = QgsLayoutExporter(layout)
        success_value = compat.resolve(QgsLayoutExporter, "ExportResult.Success",
                                       "Success", default=0)
        result = _export(exporter, layout, out, fmt, int(dpi))
        if result != success_value:
            raise invalid("layout export failed (code %s)" % result,
                          detail={"format": fmt, "path": out})
        return {"exported": out, "format": fmt, "result": result}

    @command(name="layout.atlas_configure", summary="Configure an atlas "
             "(coverage layer, page name, sort/filter)", risk="write",
             tags=("layout", "atlas"))
    def layout_atlas_configure(self, name, coverage_layer, enabled=True,
                               page_name_field=None, sort_expression=None,
                               filter_expression=None, filter_features=False):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        target = self.resolve_vector({"layer": coverage_layer})
        atlas = layout.atlas()
        atlas.setCoverageLayer(target)
        atlas.setEnabled(bool(enabled))
        if page_name_field:
            field_name = self.field_index(target, page_name_field)
            setter = getattr(atlas, "setPageNameField", None)
            if setter is not None:
                try:
                    setter(target.fields().field(field_name))
                except Exception:  # noqa: BLE001
                    pass
        if sort_expression is not None:
            for setter in ("setSortExpression", "setSortKey"):
                fn = getattr(atlas, setter, None)
                if callable(fn):
                    try:
                        fn(str(sort_expression))
                        break
                    except Exception:  # noqa: BLE001
                        continue
        if filter_expression is not None:
            fn = getattr(atlas, "setFilterExpression", None)
            if callable(fn):
                try:
                    fn(str(filter_expression))
                    atlas.setFilterFeatures(bool(filter_features))
                except Exception:  # noqa: BLE001
                    pass
        return {"layout": layout.name(), "coverage": target.id(),
                "enabled": bool(enabled),
                "features": target.featureCount()}

    @command(name="layout.atlas_export", summary="Export an atlas (multi-page "
             "PDF or one image per feature)", risk="write",
             tags=("layout", "atlas", "io"))
    def layout_atlas_export(self, name, path, format=None, dpi=300):
        layout = _find_layout(QgsProject.instance().layoutManager(), name)
        atlas = layout.atlas()
        if not atlas.enabled():
            raise invalid("atlas is not enabled; call layout.atlas_configure first")
        out = self.check_path(path, for_write=True)
        directory = os.path.dirname(out)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fmt = (format or os.path.splitext(out)[1].lstrip(".") or "pdf").lower()
        exporter = QgsLayoutExporter(layout)
        success_value = compat.resolve(QgsLayoutExporter, "ExportResult.Success",
                                       "Success", default=0)
        pages = 0
        if fmt == "pdf":
            result = _export(exporter, layout, out, "pdf", int(dpi))
            if result != success_value:
                raise invalid("atlas PDF export failed (code %s)" % result)
            try:
                pages = atlas.count()
            except Exception:  # noqa: BLE001
                pages = None
        else:
            base, ext = os.path.splitext(out)
            if not ext:
                ext = "." + fmt
            try:
                atlas.beginRender()
                while atlas.nextFeature():
                    number = atlas.currentFeatureNumber()
                    target_path = "%s_%03d%s" % (base, int(number), ext)
                    result = _export(exporter, layout, target_path, fmt, int(dpi))
                    if result != success_value:
                        raise invalid("atlas image export failed at feature %s"
                                      % number)
                    pages += 1
            finally:
                try:
                    atlas.endRender()
                except Exception:  # noqa: BLE001
                    pass
        return {"exported": out, "format": fmt, "pages": pages}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _mm_units():
    return compat.resolve(QgsUnitTypes, "LayoutUnit.LayoutMillimeters",
                          "LayoutMillimeters")


def _find_layout(manager, name):
    layouts = [l for l in manager.printLayouts() if isinstance(l, QgsPrintLayout)]
    if name:
        for layout in layouts:
            if layout.name() == str(name):
                return layout
        raise not_found("no print layout named %r" % name,
                        detail={"layouts": [l.name() for l in layouts]})
    if len(layouts) == 1:
        return layouts[0]
    if not layouts:
        raise not_found("the project has no print layouts",
                        detail={"hint": "call layout.create first"})
    raise bad_request("several layouts exist; pass name=",
                      detail={"layouts": [l.name() for l in layouts]})


def _layout_summary(layout):
    page = None
    try:
        page_item = layout.pageCollection().page(0)
        size = page_item.pageSize()
        page = {"width": round(size.width(), 2), "height": round(size.height(), 2),
                "units": "mm"}
    except Exception:  # noqa: BLE001
        pass
    return {"name": layout.name(), "page": page,
            "items": len(list(layout.items()))}


def _place(item, x, y, width, height):
    units = _mm_units()
    item.attemptMove(QgsLayoutPoint(float(x), float(y), units))
    if width is not None and height is not None:
        item.attemptResize(QgsLayoutSize(float(width), float(height), units))


def _find_map_item(layout, map_id):
    for item in layout.items():
        if isinstance(item, QgsLayoutItemMap):
            if not map_id or item.id() == str(map_id):
                return item
    return None


def _page_size(preset, orientation, width, height):
    if width and height:
        return QgsLayoutSize(float(width), float(height), _mm_units())
    presets = {"A4": (210.0, 297.0), "A3": (297.0, 420.0),
               "A5": (148.0, 210.0), "LETTER": (215.9, 279.4),
               "LEGAL": (215.9, 355.6), "A2": (420.0, 594.0),
               "A1": (594.0, 841.0), "A0": (841.0, 1189.0)}
    key = str(preset or "A4").upper()
    if key not in presets:
        raise bad_request("unknown page preset %r" % preset,
                          detail={"accepted": sorted(presets) + ["custom w/h"]})
    w, h = presets[key]
    if str(orientation).lower() == "landscape":
        w, h = h, w
    return QgsLayoutSize(w, h, _mm_units())


def _export(exporter, layout, path, fmt, dpi):
    fmt = fmt.lower()
    if fmt == "pdf":
        settings_class = compat.resolve(QgsLayoutExporter, "PdfExportSettings",
                                        default=None)
        settings = settings_class() if settings_class is not None else None
        if settings is not None:
            try:
                settings.dpi = int(dpi)
            except Exception:  # noqa: BLE001
                pass
            return exporter.exportToPdf(path, settings)
        return exporter.exportToPdf(path)
    if fmt in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
        settings_class = compat.resolve(QgsLayoutExporter, "ImageExportSettings",
                                        default=None)
        settings = settings_class() if settings_class is not None else None
        if settings is not None:
            try:
                settings.dpi = int(dpi)
            except Exception:  # noqa: BLE001
                pass
            return exporter.exportToImage(path, settings)
        return exporter.exportToImage(path)
    if fmt == "svg":
        return exporter.exportToSvg(path)
    raise bad_request("unsupported layout export format %r" % fmt)
