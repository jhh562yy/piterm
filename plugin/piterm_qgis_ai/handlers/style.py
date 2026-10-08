"""Styling: renderers (single/categorized/graduated), symbol tweaks, labeling,
QML import/export, class breaks and color ramps."""

from __future__ import annotations

import math
import os

from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsFeature,
                       QgsGraduatedSymbolRenderer, QgsPalLayerSettings,
                       QgsRendererCategory, QgsRendererRange, QgsStyle,
                       QgsSymbol, QgsTextBufferSettings, QgsTextFormat,
                       QgsVectorLayerSimpleLabeling, QgsWkbTypes)

from .. import compat
from ..errors import bad_request, invalid, io_error, not_found
from ..registry import command
from .base import HandlerBase


class StyleHandlers(HandlerBase):

    # ------------------------------------------------------------------
    # Renderer
    # ------------------------------------------------------------------
    @command(name="style.renderer_get", summary="Renderer type and a summary of "
             "categories/classes", risk="read", tags=("style",))
    def style_renderer_get(self, layer):
        target = self.resolve_vector({"layer": layer})
        renderer = target.renderer()
        out = {"layer": target.id(), "renderer": type(renderer).__name__,
               "type": renderer.type(),
               "opacity": target.opacity(),
               "labels_enabled": target.labelsEnabled()}
        try:
            if isinstance(renderer, QgsCategorizedSymbolRenderer):
                out["field"] = renderer.classAttribute()
                out["categories"] = [
                    {"value": cat.value(), "label": cat.label(),
                     "symbol": _symbol_summary(cat.symbol())}
                    for cat in renderer.categories()][:200]
            elif isinstance(renderer, QgsGraduatedSymbolRenderer):
                out["field"] = renderer.classAttribute()
                out["mode"] = str(getattr(renderer.mode(), "name", renderer.mode()))
                out["classes"] = [
                    {"lower": rng.lowerValue(), "upper": rng.upperValue(),
                     "label": rng.label(), "symbol": _symbol_summary(rng.symbol())}
                    for rng in renderer.ranges()][:200]
            else:
                out["symbol"] = _symbol_summary(renderer.symbol())
        except Exception as exc:  # noqa: BLE001
            out["renderer_error"] = str(exc)[:200]
        return out

    @command(name="style.renderer_set", summary="Apply single / categorized / "
             "graduated symbology", risk="write", tags=("style",))
    def style_renderer_set(self, layer, mode="single", field=None, classes=5,
                           classification="equal_interval", ramp=None,
                           color=None, size=None, width=None,
                           categories=None, category_limit=25):
        target = self.resolve_vector({"layer": layer})
        mode = str(mode or "single").lower()
        symbol = _default_symbol(target)
        if color:
            _apply_symbol_style(symbol, target, color=color, size=size, width=width)
        elif mode == "single":
            _apply_symbol_style(symbol, target, size=size, width=width)
        if mode == "single":
            renderer = _single_renderer(target, symbol)
        elif mode == "categorized":
            if not field:
                raise bad_request("categorized mode needs field=")
            field = self.field_index(target, field)
            renderer = self._categorized_renderer(target, field, symbol, ramp,
                                                  categories, category_limit)
        elif mode == "graduated":
            if not field:
                raise bad_request("graduated mode needs field=")
            field = self.field_index(target, field)
            renderer = self._graduated_renderer(target, field, int(classes),
                                                classification, symbol, ramp)
        else:
            raise bad_request("unknown mode %r" % mode,
                              detail={"accepted": ["single", "categorized",
                                                   "graduated"]})
        target.setRenderer(renderer)
        target.triggerRepaint()
        return {"layer": target.id(), "mode": mode,
                "renderer": type(renderer).__name__}

    def _categorized_renderer(self, layer, field, base_symbol, ramp, categories,
                              limit):
        cats = []
        if categories:
            for item in categories:
                symbol = _clone_symbol(base_symbol)
                if item.get("color"):
                    _apply_symbol_style(symbol, layer, color=item["color"])
                cats.append(QgsRendererCategory(item.get("value"),
                                                symbol,
                                                str(item.get("label", item.get("value")))))
        else:
            index = layer.fields().indexFromName(field)
            values = list(layer.dataProvider().uniqueValues(index, int(limit) or -1))
            if len(values) > int(limit):
                raise invalid(
                    "field %s has %d distinct values (limit %d); raise "
                    "category_limit or pass explicit categories"
                    % (field, len(values), int(limit)))
            color_ramp = _get_ramp(ramp)
            colors = _sample_ramp(color_ramp, max(1, len(values)))
            for position, value in enumerate(values):
                symbol = _clone_symbol(base_symbol)
                _apply_symbol_style(symbol, layer, color=colors[position])
                cats.append(QgsRendererCategory(value, symbol, str(value)))
        return QgsCategorizedSymbolRenderer(field, cats)

    def _graduated_renderer(self, layer, field, classes, classification, symbol,
                            ramp):
        """Build a graduated renderer manually.

        ``QgsGraduatedSymbolRenderer.createRenderer`` crashes natively on some
        QGIS builds (3.44 headless reproduced here), so breaks are computed in
        Python and ranges assembled explicitly - deterministic across versions.
        """
        if classes < 2 or classes > 50:
            raise bad_request("classes must be within 2..50")
        values = _numeric_values(self, layer, field)
        if not values:
            raise invalid("field %s has no numeric values to classify" % field)
        breaks, method_used = _class_breaks(values, int(classes), classification)
        color_ramp = _get_ramp(ramp)
        colors = _sample_ramp(color_ramp, len(breaks))
        ranges = []
        for position, (lower, upper) in enumerate(breaks):
            part_symbol = _clone_symbol(symbol)
            _apply_symbol_style(part_symbol, layer, color=colors[position])
            label = ("%.6g - %.6g" % (lower, upper)) if lower != upper \
                else ("%.6g" % lower)
            ranges.append(QgsRendererRange(lower, upper, part_symbol, label))
        renderer = QgsGraduatedSymbolRenderer(field, ranges)
        _apply_mode(renderer, classification)
        try:
            renderer.setSourceSymbol(_clone_symbol(symbol))
        except Exception:  # noqa: BLE001
            pass
        try:
            renderer._piterm_method = method_used  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return renderer

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------
    @command(name="style.symbol_set", summary="Tweak symbol color/size/width/"
             "opacity (single & categorized renderers)", risk="write",
             tags=("style",))
    def style_symbol_set(self, layer, color=None, size=None, width=None,
                         outline_color=None, opacity=None):
        target = self.resolve_vector({"layer": layer})
        renderer = target.renderer()
        changed = 0
        touched_multi = False
        if isinstance(renderer, QgsCategorizedSymbolRenderer):
            for cat in renderer.categories():
                _apply_symbol_style(cat.symbol(), target, color=color, size=size,
                                    width=width, outline_color=outline_color,
                                    opacity=opacity)
                changed += 1
            touched_multi = True
        elif isinstance(renderer, QgsGraduatedSymbolRenderer):
            for rng in renderer.ranges():
                _apply_symbol_style(rng.symbol(), target, color=color, size=size,
                                    width=width, outline_color=outline_color,
                                    opacity=opacity)
                changed += 1
            touched_multi = True
        else:
            symbol = renderer.symbol()
            if _apply_symbol_style(symbol, target, color=color, size=size,
                                   width=width, outline_color=outline_color,
                                   opacity=opacity):
                changed = 1
        if touched_multi:
            try:
                renderer.updateSymbols(True)
            except Exception:  # noqa: BLE001
                pass
        if opacity is not None:
            value = float(opacity)
            if value > 1.0:
                value = value / 100.0
            target.setOpacity(value)
        target.triggerRepaint()
        return {"layer": target.id(), "symbols_changed": changed}

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------
    @command(name="style.labeling_get", summary="Label configuration",
             risk="read", tags=("style", "labels"))
    def style_labeling_get(self, layer):
        target = self.resolve_vector({"layer": layer})
        out = {"layer": target.id(), "enabled": target.labelsEnabled(),
               "labeling": None}
        labeling = target.labeling()
        if labeling is not None:
            out["labeling"] = type(labeling).__name__
            try:
                settings = labeling.settings()
                out["field"] = settings.fieldName
                out["is_expression"] = bool(settings.isExpression)
                if settings.format() is not None:
                    out["font_size"] = settings.format().size()
                    color = settings.format().color()
                    out["color"] = color.name() if color is not None else None
                buffer_settings = settings.format().buffer() if settings.format() else None
                if buffer_settings is not None:
                    out["buffer_enabled"] = buffer_settings.enabled()
                    out["buffer_size"] = buffer_settings.size()
            except Exception as exc:  # noqa: BLE001
                out["settings_error"] = str(exc)[:200]
        return out

    @command(name="style.labeling_set", summary="Configure labels (field, size, "
             "color, halo, placement)", risk="write", tags=("style", "labels"))
    def style_labeling_set(self, layer, enabled=True, field=None,
                           font_size=None, color=None, buffer_color=None,
                           buffer_size=None, placement=None, is_expression=False):
        target = self.resolve_vector({"layer": layer})
        if not enabled:
            target.setLabelsEnabled(False)
            target.triggerRepaint()
            return {"layer": target.id(), "enabled": False}
        if not field:
            raise bad_request("labeling needs field=")
        settings = QgsPalLayerSettings()
        settings.fieldName = str(field)
        settings.isExpression = bool(is_expression)
        text_format = QgsTextFormat()
        if font_size is not None:
            text_format.setSize(float(font_size))
        if color is not None:
            text_format.setColor(_color(color))
        if buffer_size is not None or buffer_color is not None:
            buffer_settings = QgsTextBufferSettings()
            buffer_settings.setEnabled(True)
            buffer_settings.setSize(float(buffer_size or 1.0))
            if buffer_color is not None:
                buffer_settings.setColor(_color(buffer_color))
            text_format.setBuffer(buffer_settings)
        settings.setFormat(text_format)
        if placement:
            flag = compat.resolve(
                QgsPalLayerSettings, "Placement.%s" % placement, placement,
                default=None)
            if flag is not None:
                settings.placement = flag
        try:
            target.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            target.setLabelsEnabled(True)
            target.triggerRepaint()
        except Exception as exc:  # noqa: BLE001
            raise invalid("could not apply labeling: %s" % exc)
        return {"layer": target.id(), "enabled": True, "field": str(field)}

    # ------------------------------------------------------------------
    # QML / style transfer / ramps / breaks
    # ------------------------------------------------------------------
    @command(name="style.qml_apply", summary="Apply a QML style from a file or "
             "inline string", risk="write", tags=("style", "qml"))
    def style_qml_apply(self, layer, path=None, qml=None):
        target = self.resolve_vector({"layer": layer}, key="layer")
        if qml is None and path is None:
            raise bad_request("pass path= or qml=")
        if qml is not None:
            try:
                message, ok = target.loadNamedStyle(_write_temp(qml))
            except Exception as exc:  # noqa: BLE001
                raise invalid("inline QML could not be applied: %s" % exc)
        else:
            source = self.check_path(path, for_write=False)
            if not os.path.exists(source):
                raise not_found("style file not found: %s" % source)
            message, ok = target.loadNamedStyle(source)
        if ok is False:
            raise invalid("QGIS refused the style: %s" % message)
        target.triggerRepaint()
        return {"layer": target.id(), "applied": True, "message": str(message)[:300]}

    @command(name="style.qml_save", summary="Save a layer style as QML",
             risk="write", tags=("style", "qml"))
    def style_qml_save(self, layer, path):
        target = self.resolve_layer({"layer": layer})
        out = self.check_path(path, for_write=True)
        message, ok = target.saveNamedStyle(out)
        if ok is False:
            raise io_error("QGIS refused to save the style: %s" % message)
        return {"layer": target.id(), "path": out}

    @command(name="style.copy", summary="Copy the style from one layer to "
             "another", risk="write", tags=("style",))
    def style_copy(self, source_layer, target_layer):
        source = self.resolve_layer({"layer": source_layer})
        target = self.resolve_layer({"layer": target_layer})
        try:
            from qgis.core import QgsMapLayerStyle
            style = QgsMapLayerStyle()
            style.readFromLayer(source)
            style.writeToLayer(target)
        except (AttributeError, ImportError):
            manager = getattr(target, "setStyleManager", None)
            source_manager = getattr(source, "styleManager", None)
            if not callable(manager) or not callable(source_manager):
                raise invalid("this QGIS build cannot copy styles")
            manager(source_manager().clone())
        target.triggerRepaint()
        return {"copied": source.id(), "into": target.id()}

    @command(name="style.ramps_list", summary="Available color ramps",
             risk="read", tags=("style",))
    def style_ramps_list(self, search=None, limit=80):
        style = _default_style()
        if style is None:
            raise state_no_style()
        names = list(style.colorRampNames())
        if search:
            needle = str(search).casefold()
            names = [n for n in names if needle in n.casefold()]
        return {"count": len(names), "ramps": names[:max(1, int(limit))]}

    @command(name="style.classification_breaks", summary="Compute class breaks "
             "for a field", risk="read", tags=("style",))
    def style_classification_breaks(self, layer, field, classes=5,
                                    classification="equal_interval"):
        target = self.resolve_vector({"layer": layer})
        name = self.field_index(target, field)
        symbol = _default_symbol(target)
        try:
            renderer = self._graduated_renderer(target, name, int(classes),
                                                classification, symbol, None)
        except Exception as exc:  # noqa: BLE001
            raise invalid("could not compute breaks: %s" % exc)
        breaks = [{"lower": rng.lowerValue(), "upper": rng.upperValue(),
                   "label": rng.label()} for rng in renderer.ranges()]
        return {"layer": target.id(), "field": name, "classes": len(breaks),
                "breaks": breaks}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def state_no_style():
    from ..errors import CommandError, ErrorCode
    return CommandError(ErrorCode.STATE, "no default QGIS style database available")


def _default_style():
    for getter in ("defaultStyle",):
        fn = getattr(QgsStyle, getter, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    try:
        return QgsApplication.defaultStyle() if hasattr(QgsApplication, "defaultStyle") else None
    except Exception:  # noqa: BLE001
        return None


def _get_ramp(name):
    if not name:
        return None
    style = _default_style()
    if style is None:
        return None
    ramp = style.colorRamp(str(name))
    if ramp is None:
        names = list(style.colorRampNames())[:30]
        raise not_found("no color ramp named %r" % name,
                        detail={"available_sample": names})
    return ramp


def _sample_ramp(ramp, count):
    from qgis.PyQt.QtGui import QColor
    if ramp is None:
        base = [QColor("#1f77b4"), QColor("#ff7f0e"), QColor("#2ca02c"),
                QColor("#d62728"), QColor("#9467bd"), QColor("#8c564b")]
        return [base[i % len(base)] for i in range(count)]
    colors = []
    for index in range(count):
        try:
            ratio = 0.0 if count == 1 else index / float(count - 1)
            color = ramp.color(ratio)
            colors.append(color if color.isValid() else QColor("#808080"))
        except Exception:  # noqa: BLE001
            colors.append(QColor("#808080"))
    return colors


def _default_symbol(layer):
    symbol = getattr(layer.renderer(), "symbol", None)
    if symbol is not None:
        try:
            return symbol().clone()
        except Exception:  # noqa: BLE001
            pass
    return QgsSymbol.defaultSymbol(layer.geometryType())


def _clone_symbol(symbol):
    try:
        return symbol.clone()
    except Exception:  # noqa: BLE001
        return symbol


def _single_renderer(layer, symbol):
    from qgis.core import QgsSingleSymbolRenderer
    return QgsSingleSymbolRenderer(symbol)


def _apply_symbol_style(symbol, layer, color=None, size=None, width=None,
                        outline_color=None, opacity=None):
    if symbol is None:
        return False
    changed = False
    layers_count = symbol.symbolLayerCount() if hasattr(symbol, "symbolLayerCount") else 0
    for index in range(layers_count):
        layer_part = symbol.symbolLayer(index)
        for attr, value in (("setColor", color), ("setStrokeColor", outline_color),
                            ("setFillColor", color if symbol.type() == 2 else None)):
            if value is None:
                continue
            setter = getattr(layer_part, attr, None)
            if callable(setter):
                try:
                    setter(_color(value))
                    changed = True
                except Exception:  # noqa: BLE001
                    continue
        if size is not None:
            for setter_name in ("setSize", "setWidth"):
                setter = getattr(layer_part, setter_name, None)
                if callable(setter):
                    try:
                        setter(float(size))
                        changed = True
                        break
                    except Exception:  # noqa: BLE001
                        continue
        if width is not None:
            setter = getattr(layer_part, "setWidth", None)
            if callable(setter):
                try:
                    setter(float(width))
                    changed = True
                except Exception:  # noqa: BLE001
                    pass
        if opacity is not None:
            value = float(opacity)
            if value > 1.0:
                value = value / 100.0
            setter = getattr(layer_part, "setOpacity", None)
            if callable(setter):
                try:
                    setter(value)
                    changed = True
                except Exception:  # noqa: BLE001
                    pass
    if size is not None and symbol.type() == 0:
        for setter_name in ("setSize", "setWidth"):
            setter = getattr(symbol, setter_name, None)
            if callable(setter):
                try:
                    setter(float(size))
                    changed = True
                    break
                except Exception:  # noqa: BLE001
                    continue
    return changed


def _symbol_summary(symbol):
    if symbol is None:
        return None
    out = {"type": symbol.type()}
    try:
        out["color"] = symbol.color().name()
    except Exception:  # noqa: BLE001
        pass
    for attr in ("size", "width"):
        fn = getattr(symbol, attr, None)
        if callable(fn):
            try:
                out[attr] = fn()
            except Exception:  # noqa: BLE001
                pass
    return out


def _graduation_mode(name):
    table = {
        "equal_interval": ("Mode.EqualInterval", "EqualInterval"),
        "quantile": ("Mode.Quantile", "Quantile"),
        "jenks": ("Mode.Jenks", "Jenks"),
        "std_dev": ("Mode.StdDev", "StdDev"),
        "pretty": ("Mode.Pretty", "Pretty"),
        "logarithmic": ("Mode.Logarithmic", "Logarithmic"),
    }
    names = table.get(str(name).lower())
    if names is None:
        return None
    return compat.resolve(QgsGraduatedSymbolRenderer, *names, default=None)


def _apply_mode(renderer, classification):
    mode = _graduation_mode(classification)
    if mode is None:
        return
    try:
        renderer.setMode(mode)
    except Exception:  # noqa: BLE001
        pass


_ACCEPTED_CLASSIFICATIONS = ("equal_interval", "quantile", "jenks", "std_dev",
                             "pretty", "logarithmic")


def _numeric_values(handler, layer, field):
    values = []
    request = handler.feature_request(layer, no_geometry=True, fields=[field])
    for feature in layer.getFeatures(request):
        value = feature[field]
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
        if len(values) >= 200000:
            break
    return values


def _class_breaks(values, count, name):
    mode = str(name or "equal_interval").lower().replace("-", "_")
    if mode not in _ACCEPTED_CLASSIFICATIONS:
        raise bad_request("unknown classification %r" % name,
                          detail={"accepted": list(_ACCEPTED_CLASSIFICATIONS)})
    if mode == "jenks":
        breaks = _jenks_breaks_via_qgis(values, count)
        if breaks is not None:
            return breaks, "jenks"
        return _quantile_breaks(values, count), "quantile (jenks fallback)"
    if mode == "quantile":
        return _quantile_breaks(values, count), "quantile"
    if mode == "std_dev":
        return _std_dev_breaks(values, count), "std_dev"
    if mode == "pretty":
        return _pretty_breaks(values, count), "pretty"
    if mode == "logarithmic":
        return _log_breaks(values, count), "logarithmic"
    return _equal_interval_breaks(values, count), "equal_interval"


def _equal_interval_breaks(values, count):
    low, high = min(values), max(values)
    if high <= low:
        return [(low, high)]
    step = (high - low) / count
    breaks = []
    for index in range(count):
        lower = low + step * index
        upper = high if index == count - 1 else low + step * (index + 1)
        breaks.append((lower, upper))
    return breaks


def _quantile_breaks(values, count):
    data = sorted(values)
    n = len(data)
    if n == 0:
        return []
    if n == 1:
        return [(data[0], data[0])]
    breaks = []
    low = data[0]
    for index in range(1, count + 1):
        if index == count:
            upper = data[-1]
        else:
            position = int(round(index / float(count) * (n - 1)))
            upper = data[max(0, min(n - 1, position))]
        breaks.append((low, upper))
        low = upper
    return breaks


def _std_dev_breaks(values, count):
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / max(1, len(values))
    std = variance ** 0.5 or 1.0
    low = mean - (count // 2) * std
    return [(low + index * std, low + (index + 1) * std)
            for index in range(count)]


def _pretty_breaks(values, count):
    low, high = min(values), max(values)
    if high <= low:
        return [(low, high)]
    raw = (high - low) / max(1, count)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = magnitude
    for factor in (1, 2, 5, 10):
        if raw <= factor * magnitude:
            step = factor * magnitude
            break
    start = math.floor(low / step) * step
    breaks = []
    current = start
    while current < high and len(breaks) < 200:
        breaks.append((current, current + step))
        current += step
    return breaks or [(low, high)]


def _log_breaks(values, count):
    positive = [value for value in values if value > 0]
    if len(positive) < 2:
        return _equal_interval_breaks(values, count)
    log_low = math.log10(min(positive))
    log_high = math.log10(max(positive))
    step = (log_high - log_low) / count
    return [(10 ** (log_low + step * index), 10 ** (log_low + step * (index + 1)))
            for index in range(count)]


def _jenks_breaks_via_qgis(values, count):
    try:
        from qgis.core import QgsClassificationJenks
    except Exception:  # noqa: BLE001
        return None
    sample = values
    if len(sample) > 4000:
        stride = max(1, len(sample) // 4000)
        sample = sample[::stride]
    method = QgsClassificationJenks()
    for prepare in (lambda: method.setNumberOfClasses(int(count)), lambda: None):
        try:
            prepare()
            result = method.calculateBreaks(min(sample), max(sample), sample)
            return [(float(entry.lowerBound()), float(entry.upperBound()))
                    for entry in result]
        except Exception:  # noqa: BLE001
            continue
    return None


def _color(value):
    from qgis.PyQt.QtGui import QColor
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return QColor(int(value[0]), int(value[1]), int(value[2]),
                      int(value[3]) if len(value) > 3 else 255)
    return QColor(str(value))


def _write_temp(qml_text):
    import tempfile
    path = os.path.join(tempfile.gettempdir(),
                        "piterm_style_%d.qml" % abs(hash(qml_text)) % 10 ** 8)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(qml_text))
    return path
