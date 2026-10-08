"""Small UI helpers: message bar banners and attribute-table windows."""

from __future__ import annotations

from ..errors import not_found, state
from ..registry import command
from .base import HandlerBase


class UiHandlers(HandlerBase):

    @command(name="ui.message", summary="Show a message bar banner in QGIS",
             risk="read", tags=("ui",))
    def ui_message(self, text, level="info", duration=5):
        from .. import compat
        compat.push_message(str(text), str(level), int(duration or 5))
        return {"shown": True, "level": str(level)}

    @command(name="ui.open_attribute_table", summary="Open the attribute table "
             "window for a layer", risk="write", tags=("ui",))
    def ui_open_attribute_table(self, layer):
        target = self.resolve_layer({"layer": layer})
        iface = self.iface
        if iface is None:
            raise state("no GUI session available")
        opener = getattr(iface, "showAttributeTable", None)
        if opener is None:
            raise state("this QGIS build has no showAttributeTable helper")
        opener(target)
        return {"opened": target.id(), "name": target.name()}
