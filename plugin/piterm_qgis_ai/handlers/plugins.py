"""QGIS plugin management: list, inspect, enable/disable/reload.

Self-harm guards: disabling or reloading *this* plugin over its own wire would
cut the connection mid-command, so those two are refused with a hint to use the
QGIS plugin manager instead.
"""

from __future__ import annotations

import os

from .. import compat
from ..errors import not_found, state
from ..registry import command
from .base import HandlerBase

SELF = "piterm_qgis_ai"


class PluginHandlers(HandlerBase):

    @command(name="plugins.list", summary="Installed QGIS plugins and their "
             "enabled state", risk="read", tags=("plugins",))
    def plugins_list(self):
        import qgis.utils as qu
        available = list(getattr(qu, "available_plugins", []) or [])
        active = set(getattr(qu, "active_plugins", []) or [])
        loaded = set((getattr(qu, "plugins", {}) or {}).keys())
        names = sorted(set(available) | set(active) | set(loaded))
        out = []
        for name in names:
            out.append({"name": name, "active": name in active,
                        "loaded": name in loaded,
                        "is_bridge": name == SELF})
        return {"count": len(out), "plugins": out}

    @command(name="plugins.info", summary="Plugin details (version, author, "
             "path)", risk="read", tags=("plugins",))
    def plugins_info(self, name):
        import qgis.utils as qu
        module = (getattr(qu, "plugins", {}) or {}).get(name)
        base = None
        if module is not None:
            base = os.path.dirname(getattr(module, "__file__", "") or "")
        if base is None:
            base = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "..", name)
            base = os.path.abspath(base)
            if not os.path.isdir(base):
                raise not_found("plugin %r is not installed" % name)
        metadata_path = os.path.join(base, "metadata.txt")
        info = {"name": name, "path": base,
                "loaded": module is not None,
                "active": name in (getattr(qu, "active_plugins", []) or [])}
        if os.path.exists(metadata_path):
            try:
                import configparser
                parser = configparser.ConfigParser()
                parser.read(metadata_path, encoding="utf-8")
                if parser.has_section("general"):
                    info.update({k: v for k, v in parser.items("general")})
            except Exception as exc:  # noqa: BLE001
                info["metadata_error"] = str(exc)[:200]
        return info

    @command(name="plugins.enable", summary="Enable (start) a plugin by folder "
             "name", risk="write", tags=("plugins",))
    def plugins_enable(self, name):
        if str(name) == SELF:
            raise state("the bridge plugin cannot re-enable itself over the wire; "
                        "use the QGIS plugin manager")
        import qgis.utils as qu
        start = getattr(qu, "startPlugin", None)
        if start is None:
            raise state("this QGIS build has no startPlugin helper")
        result = start(str(name))
        if result is False:
            raise not_found("could not start plugin %r" % name,
                            detail={"hint": "check Plugins > Manage for load errors"})
        return {"enabled": str(name)}

    @command(name="plugins.disable", summary="Disable (stop) a plugin by folder "
             "name", risk="write", tags=("plugins",))
    def plugins_disable(self, name):
        if str(name) == SELF:
            raise state("refusing to stop the bridge plugin over its own wire; "
                        "use the QGIS plugin manager")
        import qgis.utils as qu
        stop = getattr(qu, "stopPlugin", None)
        if stop is None:
            raise state("this QGIS build has no stopPlugin helper")
        stop(str(name))
        return {"disabled": str(name)}

    @command(name="plugins.reload", summary="Reload a plugin's code",
             risk="destructive", tags=("plugins",))
    def plugins_reload(self, name):
        if str(name) == SELF:
            raise state("refusing to reload the bridge plugin over its own wire; "
                        "use Plugins > Piterm QGIS AI > Reload")
        import qgis.utils as qu
        for step, fn_name in (("unload", "unloadPlugin"), ("load", "loadPlugin"),
                              ("start", "startPlugin")):
            fn = getattr(qu, fn_name, None)
            if fn is None:
                continue
            try:
                fn(str(name))
            except Exception as exc:  # noqa: BLE001
                raise state("reload step %s failed: %s" % (step, exc))
        return {"reloaded": str(name)}
