"""Raster: metadata, statistics, histograms, sampling, color maps, band min/max,
raster calculator and zonal statistics (via Processing)."""

from __future__ import annotations

import os
import tempfile
import time

from qgis.core import (QgsColorRampShader, QgsContrastEnhancement, QgsPointXY,
                       QgsRaster, QgsRasterBandStats, QgsRasterLayer,
                       QgsRasterShader, QgsRectangle, QgsSingleBandGrayRenderer,
                       QgsSingleBandPseudoColorRenderer)

try:  # QGIS 3.x keeps the raster calculator in qgis.analysis
    from qgis.core import QgsRasterCalculator, QgsRasterCalculatorEntry  # noqa: F401
except ImportError:
    from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry  # noqa: F401

from .. import compat
from ..errors import bad_request, invalid, not_found
from ..registry import command
from .base import HandlerBase, value_to_json


class RasterHandlers(HandlerBase):

    @command(name="raster.info", summary="Raster size, bands, data types, nodata "
             "(optional stats)", risk="read", tags=("raster",))
    def raster_info(self, layer, with_stats=False, sample_size=0):
        target = self.resolve_raster({"layer": layer})
        provider = target.dataProvider()
        bands = []
        for band in range(1, target.bandCount() + 1):
            entry = {"band": band}
            try:
                entry["data_type"] = str(provider.dataType(band))
            except Exception:  # noqa: BLE001
                entry["data_type"] = None
            try:
                entry["nodata"] = provider.sourceNoDataValue(band)
            except Exception:  # noqa: BLE001
                entry["nodata"] = None
            if with_stats:
                try:
                    stats = provider.bandStatistics(band, _stats_enum(),
                                                   QgsRectangle(), int(sample_size))
                    entry["stats"] = {"min": stats.minimumValue,
                                      "max": stats.maximumValue,
                                      "mean": stats.mean,
                                      "std_dev": stats.stdDev,
                                      "count": stats.elementCount}
                except Exception as exc:  # noqa: BLE001
                    entry["stats_error"] = str(exc)[:200]
            bands.append(entry)
        return {"layer": target.id(), "name": target.name(),
                "width": target.width(), "height": target.height(),
                "band_count": target.bandCount(),
                "extent": compat.rect_to_dict(target.extent()),
                "crs": target.crs().authid(),
                "source": target.publicSource()[:300], "bands": bands}

    @command(name="raster.stats", summary="Band statistics (min/max/mean/stdev)",
             risk="read", tags=("raster", "stats"))
    def raster_stats(self, layer, band=1, sample_size=0):
        target = self.resolve_raster({"layer": layer})
        band = int(band)
        if not 1 <= band <= target.bandCount():
            raise invalid("band %d out of range (1..%d)" % (band, target.bandCount()))
        stats = target.dataProvider().bandStatistics(band, _stats_enum(),
                                                     QgsRectangle(), int(sample_size))
        return {"layer": target.id(), "band": band,
                "min": stats.minimumValue, "max": stats.maximumValue,
                "mean": stats.mean, "std_dev": stats.stdDev,
                "sum": getattr(stats, "sum", None),
                "count": stats.elementCount,
                "range": stats.range if hasattr(stats, "range") else None}

    @command(name="raster.histogram", summary="Band histogram", risk="read",
             tags=("raster", "stats"))
    def raster_histogram(self, layer, band=1, bins=64):
        target = self.resolve_raster({"layer": layer})
        try:
            histogram = target.dataProvider().histogram(int(band), int(bins))
        except Exception as exc:  # noqa: BLE001
            raise invalid("histogram failed: %s" % exc)
        if histogram is None:
            raise invalid("provider returned no histogram")
        values = list(getattr(histogram, "histogramVector", []) or [])
        return {"layer": target.id(), "band": int(band),
                "min": getattr(histogram, "minimum", None),
                "max": getattr(histogram, "maximum", None),
                "count": len(values), "histogram": values}

    @command(name="raster.sample", summary="Sample pixel values at points",
             risk="read", tags=("raster",))
    def raster_sample(self, layer, points, band=None, crs=None):
        target = self.resolve_raster({"layer": layer})
        if not isinstance(points, list) or not points:
            raise bad_request("pass points=[{x,y}, ...]")
        src = self.resolve_crs(crs) if crs else self.project.crs()
        transform = self.transform_to(src, target.crs())
        provider = target.dataProvider()
        out = []
        identify_enum = compat.resolve(QgsRaster, "IdentifyFormat.Value",
                                       "IdentifyFormatValue")
        for item in points:
            if isinstance(item, dict):
                px, py = float(item.get("x")), float(item.get("y"))
            else:
                px, py = float(item[0]), float(item[1])
            point = QgsPointXY(px, py)
            if transform is not None:
                point = transform.transform(point)
            entry = {"point": [px, py], "layer_point": [point.x(), point.y()]}
            if not target.extent().contains(point):
                entry["values"] = None
                entry["outside_extent"] = True
                out.append(entry)
                continue
            try:
                result = provider.identify(point, identify_enum, QgsRectangle())
                values = result.results() if result is not None else {}
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)[:200]
                out.append(entry)
                continue
            cleaned = {}
            for band_no, value in (values or {}).items():
                if band is not None and int(band_no) != int(band):
                    continue
                try:
                    nodata = provider.sourceNoDataValue(band_no)
                    if nodata is not None and value is not None \
                            and abs(float(value) - float(nodata)) < 1e-9:
                        value = None
                except Exception:  # noqa: BLE001
                    pass
                cleaned[str(band_no)] = value_to_json(value)
            entry["values"] = cleaned
            entry["outside_extent"] = False
            out.append(entry)
        return {"layer": target.id(), "crs": src.authid(), "points": out}

    @command(name="raster.colormap_get", summary="Pseudocolor entries of the "
             "current renderer", risk="read", tags=("raster", "style"))
    def raster_colormap_get(self, layer):
        target = self.resolve_raster({"layer": layer})
        renderer = target.renderer()
        entries = []
        ramp_name = None
        try:
            if isinstance(renderer, QgsSingleBandPseudoColorRenderer):
                function = renderer.shader().rasterShaderFunction()
                ramp_name = function.type()
                for item in function.colorRampItemList():
                    color = item.color
                    entries.append({
                        "value": item.value,
                        "color": color.name(),
                        "label": item.label,
                    })
        except Exception as exc:  # noqa: BLE001
            raise invalid("could not read the color map: %s" % exc)
        return {"layer": target.id(), "renderer": type(renderer).__name__,
                "ramp_type": ramp_name, "entries": entries}

    @command(name="raster.colormap_set", summary="Apply a pseudocolor renderer "
             "from explicit entries", risk="write", tags=("raster", "style"))
    def raster_colormap_set(self, layer, entries, band=1,
                            classification="interpolated",
                            min_value=None, max_value=None):
        target = self.resolve_raster({"layer": layer})
        if not isinstance(entries, list) or not entries:
            raise bad_request("pass entries=[{value,color,label}, ...]")
        function = QgsColorRampShader()
        items = []
        for entry in entries:
            try:
                item = QgsColorRampShader.ColorRampItem(
                    float(entry.get("value")), _color(entry.get("color")),
                    str(entry.get("label", "")))
                items.append(item)
            except Exception as exc:  # noqa: BLE001
                raise bad_request("bad color entry %r: %s" % (entry, exc))
        if len(items) < 2:
            raise bad_request("need at least two color entries")
        try:
            function.setColorRampItemList(items)
        except Exception as exc:  # noqa: BLE001
            raise invalid("setColorRampItemList failed: %s" % exc)
        ramp_type = compat.resolve(
            QgsColorRampShader, "Type.%s" % classification.capitalize(),
            classification.capitalize(), default=None)
        if ramp_type is not None:
            try:
                function.setColorRampType(ramp_type)
            except Exception:  # noqa: BLE001
                pass
        if min_value is not None:
            function.setMinimumValue(float(min_value))
        if max_value is not None:
            function.setMaximumValue(float(max_value))
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(function)
        renderer = QgsSingleBandPseudoColorRenderer(target.dataProvider(),
                                                    int(band), shader)
        try:
            target.setRenderer(renderer)
            target.triggerRepaint()
        except Exception as exc:  # noqa: BLE001
            raise invalid("could not apply renderer: %s" % exc)
        return {"layer": target.id(), "band": int(band), "entries": len(items),
                "classification": classification}

    @command(name="raster.minmax_set", summary="Set band min/max (gray contrast "
             "or pseudocolor range)", risk="write", tags=("raster", "style"))
    def raster_minmax_set(self, layer, band=1, min=None, max=None):
        target = self.resolve_raster({"layer": layer})
        renderer = target.renderer()
        notes = []
        if isinstance(renderer, QgsSingleBandPseudoColorRenderer):
            function = renderer.shader().rasterShaderFunction()
            if min is not None:
                function.setMinimumValue(float(min))
            if max is not None:
                function.setMaximumValue(float(max))
            notes.append("pseudocolor range updated")
        elif isinstance(renderer, QgsSingleBandGrayRenderer):
            enhancement = QgsContrastEnhancement(
                target.dataProvider().dataType(int(band)))
            if min is None or max is None:
                stats = target.dataProvider().bandStatistics(
                    int(band), _stats_enum(), QgsRectangle(), 0)
                min = stats.minimumValue if min is None else min
                max = stats.maximumValue if max is None else max
            enhancement.setMinimumValue(float(min))
            enhancement.setMaximumValue(float(max))
            algorithm = compat.resolve(QgsContrastEnhancement,
                                       "StretchToMinimumMaximum", "StretchToMinimumMaximum",
                                       default=None)
            if algorithm is not None:
                try:
                    enhancement.setContrastEnhancementAlgorithm(algorithm)
                except Exception:  # noqa: BLE001
                    pass
            renderer.setContrastEnhancement(enhancement)
            notes.append("gray contrast updated")
        else:
            notes.append("renderer %s does not support min/max adjustment"
                         % type(renderer).__name__)
        target.triggerRepaint()
        return {"layer": target.id(), "band": int(band), "min": min, "max": max,
                "notes": notes}

    @command(name="raster.renderer_info", summary="Current raster renderer "
             "summary", risk="read", tags=("raster", "style"))
    def raster_renderer_info(self, layer):
        target = self.resolve_raster({"layer": layer})
        renderer = target.renderer()
        info = {"layer": target.id(), "renderer": type(renderer).__name__,
                "type": renderer.type()}
        for attr in ("band", "classificationMin", "classificationMax",
                     "opacity"):
            fn = getattr(renderer, attr, None)
            if callable(fn):
                try:
                    info[attr] = value_to_json(fn())
                except Exception:  # noqa: BLE001
                    pass
        return info

    @command(name="raster.calculator", summary="Raster algebra via "
             "QgsRasterCalculator ('A@1 - B@1')", risk="write",
             tags=("raster", "analysis"))
    def raster_calculator(self, expression, layers, output=None, band=None,
                          bbox=None, width=None, height=None, crs=None,
                          load_result=True):
        if not isinstance(layers, list) or not layers:
            raise bad_request("pass layers=[{layer, var?}, ...]")
        entries = []
        first = None
        for index, spec in enumerate(layers):
            if isinstance(spec, dict):
                layer_ref = spec.get("layer")
                var = spec.get("var") or ("R%d" % (index + 1))
            else:
                layer_ref = spec
                var = "R%d" % (index + 1)
            target = self.resolve_raster({"layer": layer_ref})
            first = first or target
            bands = [int(band)] if band else list(range(1, target.bandCount() + 1))
            for band_no in bands:
                entry = QgsRasterCalculatorEntry()
                entry.ref = "%s@%d" % (var, band_no)
                entry.raster = target
                entry.bandNumber = band_no
                entries.append(entry)
        extent = self.bbox_from_params({"bbox": bbox}, "bbox",
                                       crs=first.crs()) if bbox else QgsRectangle(first.extent())
        target_crs = self.resolve_crs(crs) if crs else first.crs()
        cols = int(width) if width else first.width()
        rows = int(height) if height else first.height()
        if output:
            out_path = self.check_path(output, for_write=True)
        else:
            out_path = os.path.join(tempfile.gettempdir(),
                                    "piterm_calc_%d.tif" % int(time.time()))
        if not out_path.lower().endswith((".tif", ".tiff", ".img", ".asc")):
            raise bad_request("output must be a raster file (.tif/.img/.asc)")
        calculator = QgsRasterCalculator(str(expression), out_path, "GTiff",
                                         extent, target_crs, cols, rows, entries)
        code = calculator.processCalculation()
        if code != 0:
            raise invalid("raster calculator failed (code %s)" % code)
        layer = QgsRasterLayer(out_path, os.path.basename(out_path), "gdal")
        loaded = None
        if layer.isValid() and load_result:
            self.project.addMapLayer(layer)
            loaded = self.layer_summary(layer, with_extent=False)
        return {"expression": expression, "output": out_path,
                "width": cols, "height": rows, "crs": target_crs.authid(),
                "loaded_layer": loaded}

    @command(name="raster.zonal_stats", summary="Zonal statistics: raster stats "
             "per polygon (native:zonalstatisticsfb)", risk="write",
             tags=("raster", "analysis"))
    def raster_zonal_stats(self, zones, raster, band=1, statistics=None,
                           prefix="", output=None, load_result=True):
        from .processing import run_processing_sync
        zones_layer = self.resolve_vector({"layer": zones})
        raster_layer = self.resolve_raster({"layer": raster})
        stat_map = {"count": 0, "sum": 1, "mean": 2, "median": 3, "stdev": 4,
                    "min": 5, "max": 6, "range": 7, "minority": 8,
                    "majority": 9, "variety": 10, "variance": 11}
        if statistics is None:
            stats = [0, 1, 2, 5, 6]
        else:
            if isinstance(statistics, str):
                statistics = [s.strip() for s in statistics.split(",") if s.strip()]
            stats = []
            for item in statistics:
                if isinstance(item, int):
                    stats.append(item)
                elif item.lower() in stat_map:
                    stats.append(stat_map[item.lower()])
                else:
                    raise bad_request("unknown statistic %r" % item,
                                      detail={"accepted": sorted(stat_map)})
        params = {
            "INPUT": zones_layer,
            "INPUT_RASTER": raster_layer,
            "RASTER_BAND": int(band),
            "COLUMN_PREFIX": str(prefix or "stat_"),
            "STATISTICS": stats,
            "OUTPUT": output if output else "memory:zonal_stats",
        }
        return run_processing_sync(self, "native:zonalstatisticsfb", params,
                                   timeout=55, load_results=bool(load_result))

    @command(name="raster.export", summary="Export a raster to disk (optional "
             "reprojection/extent)", risk="write", tags=("raster", "io"))
    def raster_export(self, layer, path, crs=None, bbox=None, width=None,
                      height=None, resampling="near", overwrite=True):
        from .io import export_layer_impl
        target = self.resolve_raster({"layer": layer})
        out = self.check_path(path, for_write=True)
        return export_layer_impl(self, target, out, crs=crs, bbox=bbox,
                                 width=width, height=height,
                                 resampling=resampling, overwrite=overwrite)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _stats_enum():
    return compat.resolve(QgsRasterBandStats, "Statistic.All", "All")


def _color(value):
    from qgis.PyQt.QtGui import QColor
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return QColor(int(value[0]), int(value[1]), int(value[2]),
                      int(value[3]) if len(value) > 3 else 255)
    return QColor(str(value))
