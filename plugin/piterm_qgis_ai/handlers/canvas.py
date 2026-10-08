"""Canvas: navigation (extent/scale/rotation/zoom), screenshots and server-side
map rendering.

Rendering clones the canvas ``mapSettings()`` instead of rebuilding state from
``project.mapLayers()``: a rebuild draws hidden layers, which lets an opaque
hidden layer cover the visible ones (a real bug in simpler bridges).
"""

from __future__ import annotations

import base64
import os
import time

from qgis.core import (QgsMapRendererParallelJob, QgsMapSettings, QgsPointXY,
                       QgsProject, QgsRectangle)

from .. import compat
from ..constants import DEFAULT_RENDER_DPI
from ..errors import bad_request, invalid, state
from ..registry import command
from .base import HandlerBase


class CanvasHandlers(HandlerBase):

    @command(name="canvas.extent_get", summary="Current canvas extent and CRS",
             risk="read", tags=("canvas",))
    def canvas_extent_get(self):
        canvas = self.canvas
        settings = canvas.mapSettings()
        return {"extent": compat.rect_to_dict(canvas.extent()),
                "crs": settings.destinationCrs().authid(),
                "scale": canvas.scale(),
                "rotation": canvas.rotation(),
                "size": [settings.outputSize().width(), settings.outputSize().height()]}

    @command(name="canvas.extent_set", summary="Set the canvas extent "
             "(optional bbox CRS, optional margin)", risk="write", tags=("canvas",))
    def canvas_extent_set(self, bbox, crs=None, margin=0.0):
        canvas = self.canvas
        dst = canvas.mapSettings().destinationCrs()
        rect = self.bbox_from_params({"bbox": bbox, "bbox_crs": crs}, "bbox", crs=dst)
        if rect is None:
            raise bad_request("pass bbox=[xmin,ymin,xmax,ymax]")
        factor = 1.0 + max(0.0, float(margin or 0.0))
        if factor != 1.0:
            rect = QgsRectangle(rect.center().x() - rect.width() * factor / 2,
                                rect.center().y() - rect.height() * factor / 2,
                                rect.center().x() + rect.width() * factor / 2,
                                rect.center().y() + rect.height() * factor / 2)
        canvas.setExtent(rect)
        canvas.refresh()
        return {"extent": compat.rect_to_dict(rect), "crs": dst.authid()}

    @command(name="canvas.scale_get", summary="Canvas scale, rotation and "
             "magnification", risk="read", tags=("canvas",))
    def canvas_scale_get(self):
        canvas = self.canvas
        return {"scale": canvas.scale(), "rotation": canvas.rotation(),
                "magnification": canvas.magnificationFactor()}

    @command(name="canvas.scale_set", summary="Set scale (denominator) and/or "
             "rotation (degrees)", risk="write", tags=("canvas",))
    def canvas_scale_set(self, scale=None, rotation=None):
        canvas = self.canvas
        changed = {}
        if scale is not None:
            _scale_fn = (getattr(canvas, "setScale", None)
                         or getattr(canvas, "zoomScale", None))
            if _scale_fn is None:
                raise state("this QGIS build cannot set the canvas scale")
            _scale_fn(float(scale))
            changed["scale"] = float(scale)
        if rotation is not None:
            canvas.setRotation(float(rotation))
            changed["rotation"] = float(rotation)
        canvas.refresh()
        return {"changed": changed, "scale": canvas.scale(),
                "rotation": canvas.rotation()}

    @command(name="canvas.center_set", summary="Center the canvas on a point "
             "(optional scale)", risk="write", tags=("canvas",))
    def canvas_center_set(self, x, y, scale=None, crs=None):
        canvas = self.canvas
        dst = canvas.mapSettings().destinationCrs()
        point = self.point_from_params({"x": x, "y": y})
        src = self.resolve_crs(crs) if crs else dst
        transform = self.transform_to(src, dst)
        if transform is not None:
            point = transform.transform(point)
        if scale is not None:
            _scale_fn = (getattr(canvas, "setScale", None)
                         or getattr(canvas, "zoomScale", None))
            if _scale_fn is None:
                raise state("this QGIS build cannot set the canvas scale")
            _scale_fn(float(scale))
        canvas.setCenter(point)
        canvas.refresh()
        return {"center": compat.point_to_list(point), "scale": canvas.scale()}

    @command(name="canvas.rotation_set", summary="Rotate the canvas (degrees)",
             risk="write", tags=("canvas",))
    def canvas_rotation_set(self, degrees):
        canvas = self.canvas
        canvas.setRotation(float(degrees))
        canvas.refresh()
        return {"rotation": canvas.rotation()}

    @command(name="canvas.zoom_features", summary="Zoom to specific feature ids",
             risk="read", tags=("canvas",))
    def canvas_zoom_features(self, layer, fids, margin=0.1):
        target = self.resolve_vector({"layer": layer})
        ids = [int(f) for f in (fids or [])]
        if not ids:
            raise bad_request("pass fids=[...]")
        rect = None
        request = self.feature_request(target, ids=ids)
        for feature in target.getFeatures(request):
            geometry = feature.geometry()
            if geometry is None or geometry.isNull():
                continue
            bbox = geometry.boundingBox()
            rect = QgsRectangle(bbox) if rect is None else _combine(rect, bbox)
        if rect is None or rect.isNull():
            raise state("no geometry found for those fids")
        canvas = self.canvas
        dst = canvas.mapSettings().destinationCrs()
        crs = target.crs()
        if crs.isValid() and crs.authid() != dst.authid():
            rect = compat.transform_rect(rect, crs, dst,
                                         self.project.transformContext())
        factor = 1.0 + max(0.0, float(margin or 0.0))
        rect = QgsRectangle(rect.center().x() - rect.width() * factor / 2,
                            rect.center().y() - rect.height() * factor / 2,
                            rect.center().x() + rect.width() * factor / 2,
                            rect.center().y() + rect.height() * factor / 2)
        canvas.setExtent(rect)
        canvas.refresh()
        return {"zoomed": target.id(), "fids": ids,
                "extent": compat.rect_to_dict(rect)}

    @command(name="canvas.zoom_full", summary="Zoom to the full project extent",
             risk="read", tags=("canvas",))
    def canvas_zoom_full(self):
        canvas = self.canvas
        iface = self.iface
        if iface is not None:
            try:
                iface.zoomFull()
                return {"zoomed": "full", "extent": compat.rect_to_dict(canvas.extent())}
            except Exception:  # noqa: BLE001
                pass
        rect = _full_extent(self.project, canvas.mapSettings().destinationCrs())
        if rect is None:
            raise state("no layer extents available to zoom to")
        canvas.setExtent(rect)
        canvas.refresh()
        return {"zoomed": "full", "extent": compat.rect_to_dict(rect)}

    @command(name="canvas.refresh", summary="Repaint the canvas", risk="write",
             tags=("canvas",))
    def canvas_refresh(self):
        self.canvas.refresh()
        return {"refreshed": True}

    @command(name="canvas.screenshot", summary="Grab the canvas widget as PNG "
             "(inline base64 or file)", risk="read", tags=("canvas",))
    def canvas_screenshot(self, path=None, max_size=2000, format="png"):
        canvas = self.canvas
        pixmap = canvas.grab()
        if pixmap is None or pixmap.isNull():
            raise state("canvas grab returned nothing")
        return _pixmap_out(self, pixmap, path, "canvas.", max_size, format)

    @command(name="canvas.render", summary="Server-side map render (bbox/size/"
             "dpi control, never blocks the canvas)", risk="read",
             tags=("canvas",))
    def canvas_render(self, bbox=None, width=1200, height=800, dpi=None,
                      path=None, format="png", transparent=False, timeout=55,
                      max_size=0):
        canvas = self.canvas
        settings = QgsMapSettings(canvas.mapSettings())
        dst = settings.destinationCrs()
        if bbox is not None:
            rect = self.bbox_from_params({"bbox": bbox}, "bbox", crs=dst)
            if rect is not None:
                settings.setExtent(rect)
        width = max(64, min(int(width or 1200), 20000))
        height = max(64, min(int(height or 800), 20000))
        settings.setOutputSize(_qsize(width, height))
        settings.setOutputDpi(int(dpi or DEFAULT_RENDER_DPI))
        try:
            settings.setBackgroundColor(canvas.mapSettings().backgroundColor())
            if transparent:
                from qgis.PyQt.QtGui import QColor
                settings.setBackgroundColor(QColor(0, 0, 0, 0))
        except Exception:  # noqa: BLE001
            pass
        job = QgsMapRendererParallelJob(settings)
        loop = _event_loop()
        finished = {"done": False}

        def _mark_finished():
            finished["done"] = True
            loop.quit()

        job.finished.connect(_mark_finished)
        timer = _timer(int(float(timeout or 55) * 1000), loop.quit)
        job.start()
        loop.exec()
        try:
            timer.stop()
        except Exception:  # noqa: BLE001
            pass
        if not finished["done"]:
            try:
                job.cancel()
                job.waitForFinished()
            except Exception:  # noqa: BLE001
                pass
            raise invalid("render exceeded %.0fs" % float(timeout or 55))
        image = job.renderedImage()
        warnings = []
        try:
            for err in job.errors():
                warnings.append({"layer": getattr(err, "layerID", ""),
                                 "message": str(getattr(err, "message", err))[:200]})
        except Exception:  # noqa: BLE001
            pass
        if image is None or image.isNull():
            raise invalid("render produced no image", detail={"warnings": warnings})
        payload = _image_out(self, image, path, "render.", max_size, format)
        payload["warnings"] = warnings
        payload["size"] = [image.width(), image.height()]
        return payload

    @command(name="canvas.render_3d", summary="Grab an open 3D map view "
             "(best effort)", risk="read", tags=("canvas", "3d"))
    def canvas_render_3d(self, path=None, view_index=0, max_size=1600):
        iface = self.iface
        views = []
        if iface is not None:
            for getter in ("mapCanvases", "mapCanvases3D"):
                fn = getattr(iface, getter, None)
                if callable(fn):
                    try:
                        views.extend(list(fn()))
                    except Exception:  # noqa: BLE001
                        continue
        candidates = [v for v in views if "3d" in type(v).__name__.lower()
                      or "3D" in type(v).__name__]
        if not candidates:
            raise state("no open 3D map view found",
                        detail={"hint": "open View > 3D Map Views first"})
        index = max(0, min(int(view_index or 0), len(candidates) - 1))
        view = candidates[index]
        pixmap = view.grab()
        if pixmap is None or pixmap.isNull():
            raise state("3D view grab returned nothing")
        result = _pixmap_out(self, pixmap, path, "render3d.", max_size, "png")
        result["view_index"] = index
        result["open_views"] = len(candidates)
        return result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _combine(rect, other):
    out = QgsRectangle(rect)
    out.combineExtentWith(other)
    return out


def _full_extent(project, dst_crs):
    rect = None
    for layer in project.mapLayers().values():
        try:
            if not layer.isValid():
                continue
            layer_rect = layer.extent()
            if layer_rect.isNull() or layer_rect.isEmpty():
                continue
            crs = layer.crs()
            if crs.isValid() and dst_crs.isValid() and crs.authid() != dst_crs.authid():
                layer_rect = compat.transform_rect(QgsRectangle(layer_rect), crs,
                                                   dst_crs,
                                                   project.transformContext())
            rect = QgsRectangle(layer_rect) if rect is None else _combine(rect, layer_rect)
        except Exception:  # noqa: BLE001
            continue
    return rect


def _qsize(width, height):
    from qgis.PyQt.QtCore import QSize
    return QSize(int(width), int(height))


def _event_loop():
    from qgis.PyQt.QtCore import QEventLoop
    return QEventLoop()


def _timer(ms, callback):
    from qgis.PyQt.QtCore import QTimer
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(callback)
    timer.start(max(1000, int(ms)))
    return timer


def _image_bytes(image, fmt="png", quality=90):
    from qgis.PyQt.QtCore import QBuffer, QByteArray
    buffer = QBuffer()
    open_flag = compat.resolve(QBuffer, "OpenModeFlag.WriteOnly", "WriteOnly")
    buffer.open(open_flag)
    image.save(buffer, fmt.upper(), int(quality))
    return bytes(buffer.data())


def _image_out(handler, image, path, prefix, max_size, fmt):
    fmt = (fmt or "png").lower()
    if max_size and max(image.width(), image.height()) > int(max_size):
        image = _scaled(image, int(max_size))
    if path:
        resolved = handler.check_path(path, for_write=True)
        directory = os.path.dirname(resolved)
        if directory:
            os.makedirs(directory, exist_ok=True)
        ok = image.save(resolved, fmt.upper())
        if not ok:
            raise invalid("could not write image to %s" % resolved)
        return {"path": resolved, "format": fmt,
                "size": [image.width(), image.height()]}
    data = _image_bytes(image, fmt)
    return {"format": fmt, "size": [image.width(), image.height()],
            "base64": base64.b64encode(data).decode("ascii"),
            "bytes": len(data)}


def _pixmap_out(handler, pixmap, path, prefix, max_size, fmt):
    image = pixmap.toImage()
    return _image_out(handler, image, path, prefix, max_size, fmt)


def _scaled(image, max_size):
    keep = compat.resolve(_qt(), "AspectRatioMode.KeepAspectRatio",
                          "KeepAspectRatio")
    smooth = compat.resolve(_qt(), "TransformationMode.SmoothTransformation",
                            "SmoothTransformation")
    return image.scaled(int(max_size), int(max_size), keep, smooth)


def _qt():
    from qgis.PyQt.QtCore import Qt
    return Qt
