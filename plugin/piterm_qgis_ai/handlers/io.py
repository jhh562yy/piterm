"""I/O commands: import any file as a layer, export layers, download URLs,
read/write text files, and format conversion via Processing.

``export_layer_impl`` is shared with ``layers.export`` / ``raster.export`` so the
reprojection logic exists exactly once:

* vector -> QgsVectorFileWriter v3 (fast path), reprojection through a
  temporary ``native:reprojectlayer`` memory layer when ``crs`` is given;
* raster -> Processing ``gdal:warpreproject`` (reproject/extent/resize) or
  ``gdal:translate`` (plain copy).
"""

from __future__ import annotations

import os
import time
import urllib.parse
import urllib.request

from qgis.core import QgsProject, QgsRasterLayer, QgsVectorFileWriter, QgsVectorLayer

from .. import compat
from ..errors import bad_request, invalid, io_error, not_found
from ..registry import command
from .base import HandlerBase


class IoHandlers(HandlerBase):

    @command(name="io.import_file", summary="Import a file as a vector or raster "
             "layer (auto-detected)", risk="write", tags=("io",))
    def io_import_file(self, path, kind="auto", name=None, provider=None):
        source = self.check_path(path, for_write=False)
        if not os.path.exists(source):
            raise not_found("file not found: %s" % source)
        display = name or os.path.splitext(os.path.basename(source))[0]
        layer = None
        if kind in ("auto", "vector"):
            layer = QgsVectorLayer(source, display, provider or "ogr")
            if layer.isValid():
                self.project.addMapLayer(layer)
                return {"kind": "vector",
                        "layer": self.layer_summary(layer, with_extent=False)}
            layer = None
        if kind in ("auto", "raster"):
            layer = QgsRasterLayer(source, display, provider or "gdal")
            if layer.isValid():
                self.project.addMapLayer(layer)
                return {"kind": "raster",
                        "layer": self.layer_summary(layer, with_extent=False)}
        raise invalid("could not load %s as vector or raster" % source,
                      detail={"kind": kind, "provider": provider})

    @command(name="io.export_layer", summary="Export a layer to disk (shared "
             "logic with layers.export / raster.export)", risk="write",
             tags=("io",))
    def io_export_layer(self, layer, path, driver=None, crs=None, filter=None,
                        selected_only=False, overwrite=True, bbox=None,
                        width=None, height=None, resampling="near"):
        target = self.resolve_layer({"layer": layer})
        out = self.check_path(path, for_write=True)
        return export_layer_impl(self, target, out, driver=driver, crs=crs,
                                 filter_expression=filter,
                                 selected_only=bool(selected_only),
                                 overwrite=bool(overwrite), bbox=bbox,
                                 width=width, height=height,
                                 resampling=resampling)

    @command(name="io.fetch_url", summary="Download a URL to disk (size-capped) "
             "and optionally load it as a layer", risk="write",
             tags=("io", "web"))
    def io_fetch_url(self, url, path=None, add_as_layer=True, max_bytes=200000000,
                     timeout=120, headers=None):
        import tempfile
        text = str(url)
        if not text.lower().startswith(("http://", "https://")):
            raise bad_request("only http/https URLs are allowed")
        if path:
            target_path = self.check_path(path, for_write=True)
        else:
            parsed = urllib.parse.urlparse(text)
            suffix = os.path.splitext(parsed.path)[1][:10] or ".bin"
            target_path = os.path.join(tempfile.gettempdir(),
                                       "piterm_fetch_%d%s" % (int(time.time()), suffix))
        request = urllib.request.Request(text, headers={
            "User-Agent": "Piterm-QGIS-AI/1.0",
            **{str(k): str(v) for k, v in (headers or {}).items()},
        })
        directory = os.path.dirname(target_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        written = 0
        try:
            from qgis.PyQt.QtCore import QCoreApplication
            with urllib.request.urlopen(request, timeout=float(timeout)) as response:
                info = {"status": getattr(response, "status", 200),
                        "content_type": response.headers.get("Content-Type", "")}
                with open(target_path, "wb") as fh:
                    last_pump = 0.0
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > int(max_bytes):
                            fh.close()
                            raise invalid("download exceeded max_bytes=%s" % max_bytes)
                        fh.write(chunk)
                        now = time.monotonic()
                        if now - last_pump > 0.05:
                            QCoreApplication.processEvents()
                            last_pump = now
        except Exception as exc:  # noqa: BLE001
            raise io_error("download failed: %s" % exc)
        result = {"url": text, "path": target_path, "bytes": written, "info": info}
        if add_as_layer:
            try:
                outcome = self.io_import_file(target_path, kind="auto")
                result["layer"] = outcome.get("layer")
                result["kind"] = outcome.get("kind")
            except Exception as exc:  # noqa: BLE001
                result["layer_error"] = str(exc)[:200]
        return result

    @command(name="io.read_text", summary="Read a text file (size-capped) for "
             "inspection", risk="read", tags=("io",))
    def io_read_text(self, path, max_bytes=200000, encoding="utf-8"):
        source = self.check_path(path, for_write=False)
        if not os.path.exists(source):
            raise not_found("file not found: %s" % source)
        try:
            with open(source, "rb") as fh:
                raw = fh.read(max(1, int(max_bytes)))
        except Exception as exc:  # noqa: BLE001
            raise io_error("read failed: %s" % exc)
        truncated = os.path.getsize(source) > len(raw)
        text = raw.decode(encoding, "replace")
        return {"path": source, "bytes": len(raw), "truncated": truncated,
                "text": text}

    @command(name="io.write_text", summary="Write/append a text file (inside the "
             "allow-list)", risk="write", tags=("io",))
    def io_write_text(self, path, text, append=False, encoding="utf-8"):
        target = self.check_path(path, for_write=True)
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        mode = "a" if append else "w"
        try:
            with open(target, mode, encoding=encoding) as fh:
                fh.write(str(text))
        except Exception as exc:  # noqa: BLE001
            raise io_error("write failed: %s" % exc)
        return {"path": target, "bytes": os.path.getsize(target),
                "appended": bool(append)}

    @command(name="io.convert", summary="Convert a file's format via GDAL "
             "(vector: gdal:convertformat, raster: gdal:translate)",
             risk="write", tags=("io",))
    def io_convert(self, path_in, path_out, kind="auto"):
        source = self.check_path(path_in, for_write=False)
        target = self.check_path(path_out, for_write=True)
        if not os.path.exists(source):
            raise not_found("input not found: %s" % source)
        from .processing import run_processing_sync
        layer = None
        vector = QgsVectorLayer(source, "check", "ogr")
        if vector.isValid():
            layer = vector
            algorithm = "gdal:convertformat"
        else:
            raster = QgsRasterLayer(source, "check", "gdal")
            if not raster.isValid():
                raise invalid("input is neither a readable vector nor raster: %s"
                              % source)
            layer = raster
            algorithm = "gdal:translate"
        outcome = run_processing_sync(self, algorithm,
                                      {"INPUT": layer, "OUTPUT": target},
                                      timeout=55, load_results=False)
        return {"converted": target, "algorithm": algorithm,
                "exists": os.path.exists(target),
                "log_tail": outcome.get("log_tail", [])[-5:]}


# ---------------------------------------------------------------------------
# shared export implementation
# ---------------------------------------------------------------------------
def export_layer_impl(handler, layer, path, driver=None, crs=None,
                      filter_expression=None, selected_only=False,
                      overwrite=True, bbox=None, width=None, height=None,
                      resampling="near"):
    kind = compat.layer_kind(layer)
    if kind == "vector":
        return _export_vector(handler, layer, path, driver, crs,
                              filter_expression, selected_only, overwrite)
    if kind == "raster":
        return _export_raster(handler, layer, path, crs, bbox, width, height,
                              resampling)
    raise invalid("export supports vector and raster layers, got %s" % kind)


def _export_vector(handler, layer, path, driver, crs, filter_expression,
                   selected_only, overwrite):
    working = layer
    notes = []
    if crs:
        from .processing import run_processing_sync
        outcome = run_processing_sync(
            handler, "native:reprojectlayer",
            {"INPUT": layer, "TARGET_CRS": crs, "OUTPUT": "memory:piterm_export"},
            timeout=55, load_results=False)
        reprojected = None
        result = outcome.get("result") or {}
        if isinstance(result.get("OUTPUT"), dict):
            reprojected = QgsProject.instance().mapLayer(result["OUTPUT"].get("id", ""))
        if reprojected is None:
            raise invalid("reprojection step did not produce a layer")
        working = reprojected
        notes.append("reprojected via native:reprojectlayer")
    extension = os.path.splitext(path)[1].lstrip(".").lower()
    if not driver:
        try:
            driver = QgsVectorFileWriter.driverForExtension(extension)
        except Exception:  # noqa: BLE001
            driver = None
    options = QgsVectorFileWriter.SaveVectorOptions()
    if driver:
        options.driverName = driver
    options.layerName = os.path.splitext(os.path.basename(path))[0][:60] or "layer"
    options.fileEncoding = "UTF-8"
    if filter_expression:
        options.filterExpression = str(filter_expression)
    if selected_only:
        options.onlySelectedFeatures = True
    if overwrite:
        action = compat.resolve(QgsVectorFileWriter,
                                "ActionOnExistingFile.CreateOrOverwriteFile",
                                "CreateOrOverwriteFile", default=None)
        if action is not None:
            try:
                options.actionOnExistingFile = action
            except Exception:  # noqa: BLE001
                pass
    context = QgsProject.instance().transformContext()
    try:
        result = QgsVectorFileWriter.writeAsVectorFormatV3(working, path, context,
                                                           options)
    except AttributeError:
        result = QgsVectorFileWriter.writeAsVectorFormatV2(working, path, context,
                                                           options)
    code = result[0] if isinstance(result, tuple) else int(result)
    if code != 0:
        raise io_error("vector export failed (writer code %s)" % code)
    return {"path": path, "format": driver or extension,
            "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            "reprojected": bool(crs), "notes": notes}


def _export_raster(handler, layer, path, crs, bbox, width, height, resampling):
    from .processing import run_processing_sync
    if crs or bbox or width or height:
        params = {"INPUT": layer, "TARGET_CRS": crs or layer.crs(),
                  "RESAMPLING": _resampling_index(resampling),
                  "OUTPUT": path}
        if bbox:
            rect = handler.bbox_from_params({"bbox": bbox}, "bbox", crs=layer.crs())
            params["EXTRA"] = "-te %f %f %f %f" % (rect.xMinimum(),
                                                   rect.yMinimum(),
                                                   rect.xMaximum(),
                                                   rect.yMaximum())
        outcome = run_processing_sync(handler, "gdal:warpreproject", params,
                                      timeout=55, load_results=False)
        algorithm = "gdal:warpreproject"
    else:
        outcome = run_processing_sync(handler, "gdal:translate",
                                      {"INPUT": layer, "OUTPUT": path},
                                      timeout=55, load_results=False)
        algorithm = "gdal:translate"
    if not os.path.exists(path):
        raise io_error("raster export did not produce %s" % path,
                       detail={"log_tail": outcome.get("log_tail", [])[-10:]})
    return {"path": path, "algorithm": algorithm,
            "bytes": os.path.getsize(path),
            "log_tail": outcome.get("log_tail", [])[-5:]}


def _resampling_index(name):
    table = {"near": 0, "bilinear": 1, "cubic": 2, "cubicspline": 3,
             "lanczos": 4, "average": 5, "mode": 6, "max": 7, "min": 8,
             "med": 9, "q1": 10, "q3": 11, "sum": 12}
    key = str(name or "near").lower()
    if key not in table:
        raise bad_request("unknown resampling %r" % name,
                          detail={"accepted": sorted(table)})
    return table[key]
