"""Project commands: lifecycle, CRS, variables, bookmarks, map themes, metadata.

``project.open`` uses the guarded reader so a broken layer reference can never
freeze the socket behind the modal 'Handle Unavailable Layers' dialog.
"""

from __future__ import annotations

import os

from qgis.core import (QgsBookmark, QgsCoordinateReferenceSystem, QgsProject,
                       QgsReferencedRectangle, QgsRectangle)

from .. import compat
from ..errors import bad_request, invalid, io_error, not_found
from ..registry import command
from .base import HandlerBase


class ProjectHandlers(HandlerBase):

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @command(name="project.info", summary="Current project metadata, CRS, extent, "
             "dirty flag", risk="read", tags=("project",))
    def project_info(self, with_extent=True, with_variables=False):
        project = QgsProject.instance()
        crs = project.crs()
        info = {
            "file": project.fileName() or None,
            "file_name": os.path.basename(project.fileName() or ""),
            "title": _safe(lambda: project.metadata().title()) or project.baseName(),
            "dirty": bool(project.isDirty()),
            "layer_count": len(project.mapLayers()),
            "crs": {"authid": crs.authid(), "description": crs.description(),
                    "is_geographic": crs.isGeographic(),
                    "units": _crs_units(crs)},
            "home_path": project.homePath(),
            "layer_tree_root_count": len(project.layerTreeRoot().children()),
            "ellipsoid": _safe(lambda: project.ellipsoid()) or "",
        }
        if with_extent:
            info["extent"] = _project_extent(project)
        if with_variables:
            info["variables"] = dict(project.customVariables())
        return info

    @command(name="project.new", summary="Discard the current project (optionally "
             "save it first)", risk="destructive", tags=("project",))
    def project_new(self, save_path=None, title=None):
        project = QgsProject.instance()
        warnings = []
        if project.isDirty():
            warnings.append("previous project had unsaved changes and was discarded "
                            "(use checkpoints or save_project first next time)")
        project.clear()
        if title is not None:
            try:
                metadata = project.metadata()
                metadata.setTitle(str(title))
                project.setMetadata(metadata)
            except Exception as exc:  # noqa: BLE001
                warnings.append("could not set title: %s" % exc)
        saved = None
        if save_path:
            path = self.check_path(save_path, for_write=True)
            project.setFileName(path)
            if not project.write():
                raise io_error("project.write() failed for %s" % path)
            saved = path
        return {"created": True, "saved": saved, "warnings": warnings}

    @command(name="project.open", summary="Load a .qgs/.qgz project from disk",
             risk="destructive", tags=("project",))
    def project_open(self, path):
        resolved = self.check_path(path, for_write=False)
        if not os.path.exists(resolved):
            raise not_found("project file does not exist: %s" % resolved)
        ok = compat.read_project_guarded(resolved)
        project = QgsProject.instance()
        broken = []
        for layer in project.mapLayers().values():
            try:
                if not layer.isValid():
                    broken.append(layer.name())
            except Exception:  # noqa: BLE001
                broken.append(layer.name())
        return {"opened": resolved, "ok": bool(ok),
                "layer_count": len(project.mapLayers()),
                "invalid_layers": broken,
                "hint": broken and "fix data sources or remove invalid layers" or None}

    @command(name="project.save", summary="Save to the current file or a new path",
             risk="write", tags=("project",))
    def project_save(self, path=None):
        project = QgsProject.instance()
        target = project.fileName()
        if path:
            target = self.check_path(path, for_write=True)
            project.setFileName(target)
        if not target:
            raise invalid("project has no file name yet; pass path=... to save")
        ok = project.write()
        if not ok:
            raise io_error("project.write() failed for %s" % target)
        return {"saved": target, "ok": True, "dirty": bool(project.isDirty())}

    @command(name="project.set_crs", summary="Set the project CRS (canvas "
             "projection)", risk="write", tags=("project",))
    def project_set_crs(self, crs):
        value = self.resolve_crs(crs)
        project = QgsProject.instance()
        project.setCrs(value)
        try:
            canvas = self.canvas
            canvas.refresh()
        except Exception:  # noqa: BLE001
            pass
        return {"crs": value.authid(), "description": value.description()}

    # ------------------------------------------------------------------
    # Variables
    # ------------------------------------------------------------------
    @command(name="project.variables_list", summary="Project expression variables "
             "(@key)", risk="read", tags=("project", "variables"))
    def project_variables_list(self):
        project = QgsProject.instance()
        return {"variables": {str(k): str(v) for k, v in project.customVariables().items()}}

    @command(name="project.variables_set", summary="Set one project variable",
             risk="write", tags=("project", "variables"))
    def project_variables_set(self, key, value):
        project = QgsProject.instance()
        variables = dict(project.customVariables())
        variables[str(key)] = str(value)
        project.setCustomVariables(variables)
        return {"set": str(key), "value": str(value)}

    @command(name="project.variables_remove", summary="Remove one project variable",
             risk="write", tags=("project", "variables"))
    def project_variables_remove(self, key):
        project = QgsProject.instance()
        variables = dict(project.customVariables())
        existed = str(key) in variables
        variables.pop(str(key), None)
        project.setCustomVariables(variables)
        return {"removed": str(key), "existed": existed}

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------
    @command(name="project.bookmarks_list", summary="Spatial bookmarks",
             risk="read", tags=("project", "bookmarks"))
    def project_bookmarks_list(self):
        manager = QgsProject.instance().bookmarkManager()
        out = []
        for bookmark in manager.bookmarks():
            out.append(_bookmark_dict(bookmark))
        return {"bookmarks": out}

    @command(name="project.bookmarks_add", summary="Add a spatial bookmark "
             "(defaults to the current canvas extent)", risk="write",
             tags=("project", "bookmarks"))
    def project_bookmarks_add(self, name, bbox=None, crs=None, group=""):
        project = QgsProject.instance()
        rect = self.bbox_from_params({"bbox": bbox, "bbox_crs": crs},
                                     "bbox", crs=project.crs())
        if rect is None:
            try:
                rect = QgsRectangle(self.canvas.extent())
            except Exception:  # noqa: BLE001
                raise bad_request("no bbox and no canvas extent available")
        target_crs = self.resolve_crs(crs) if crs else project.crs()
        bookmark = _make_bookmark(str(name), str(group or ""), rect, target_crs)
        bookmark_id = project.bookmarkManager().addBookmark(bookmark)
        return {"id": str(bookmark_id), "name": str(name),
                "extent": compat.rect_to_dict(rect), "crs": target_crs.authid()}

    @command(name="project.bookmarks_remove", summary="Remove a bookmark by id",
             risk="destructive", tags=("project", "bookmarks"))
    def project_bookmarks_remove(self, id):
        manager = QgsProject.instance().bookmarkManager()
        manager.removeBookmark(str(id))
        return {"removed": str(id)}

    @command(name="project.bookmarks_zoom", summary="Zoom the canvas to a bookmark",
             risk="read", tags=("project", "bookmarks"))
    def project_bookmarks_zoom(self, id):
        manager = QgsProject.instance().bookmarkManager()
        for bookmark in manager.bookmarks():
            if str(bookmark.id()) == str(id):
                canvas = self.canvas
                rect = bookmark.extent()
                src = bookmark.crs()
                dst = canvas.mapSettings().destinationCrs()
                if src is not None and src.isValid() and src.authid() != dst.authid():
                    rect = compat.transform_rect(QgsRectangle(rect), src, dst,
                                                 QgsProject.instance().transformContext())
                canvas.setExtent(rect)
                canvas.refresh()
                return {"zoomed": str(id), "extent": compat.rect_to_dict(rect)}
        raise not_found("no bookmark with id %r" % id)

    # ------------------------------------------------------------------
    # Map themes (visibility presets)
    # ------------------------------------------------------------------
    @command(name="project.themes_list", summary="Map themes (layer visibility "
             "presets)", risk="read", tags=("project", "themes"))
    def project_themes_list(self):
        collection = QgsProject.instance().mapThemeCollection()
        themes = []
        try:
            names = collection.mapThemes()
        except Exception:  # noqa: BLE001
            names = []
        if isinstance(names, dict):
            names = list(names.keys())
        for name in names:
            layers = []
            try:
                state = collection.mapThemeState(name)
                layers = list(state.visibleLayerIds())
            except Exception:  # noqa: BLE001
                pass
            themes.append({"name": str(name), "visible_layers": len(layers)})
        return {"themes": themes}

    @command(name="project.themes_add", summary="Save current layer visibility as "
             "a theme (updates same name)", risk="write", tags=("project", "themes"))
    def project_themes_add(self, name):
        project = QgsProject.instance()
        collection = project.mapThemeCollection()
        root = project.layerTreeRoot()
        try:
            collection.insert(str(name), root)
        except TypeError:
            collection.update(str(name), root)
        return {"saved": str(name)}

    @command(name="project.themes_apply", summary="Restore layer visibility saved "
             "in a theme", risk="write", tags=("project", "themes"))
    def project_themes_apply(self, name):
        iface = self.iface
        applied = False
        if iface is not None and hasattr(iface, "setActiveTheme"):
            try:
                iface.setActiveTheme(str(name))
                applied = True
            except Exception:  # noqa: BLE001
                applied = False
        if not applied:
            collection = QgsProject.instance().mapThemeCollection()
            try:
                collection.applyTheme(str(name), QgsProject.instance().layerTreeRoot())
                applied = True
            except Exception as exc:  # noqa: BLE001
                raise not_found("could not apply theme %r: %s" % (name, exc))
        return {"applied": str(name)}

    @command(name="project.themes_remove", summary="Remove a map theme",
             risk="destructive", tags=("project", "themes"))
    def project_themes_remove(self, name):
        collection = QgsProject.instance().mapThemeCollection()
        for method in ("removeMapTheme", "removeTheme"):
            fn = getattr(collection, method, None)
            if fn is not None:
                fn(str(name))
                return {"removed": str(name)}
        raise not_found("this QGIS build has no theme removal API")

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    @command(name="project.metadata_get", summary="Project metadata (title, "
             "abstract, keywords)", risk="read", tags=("project",))
    def project_metadata_get(self):
        metadata = QgsProject.instance().metadata()
        return {
            "title": _safe(lambda: metadata.title()) or "",
            "abstract": _safe(lambda: metadata.abstract()) or "",
            "keywords": list(_safe(lambda: metadata.keywords()) or []),
            "categories": list(_safe(lambda: metadata.categories()) or []),
            "identifier": _safe(lambda: metadata.identifier()) or "",
        }

    @command(name="project.metadata_set", summary="Update project metadata",
             risk="write", tags=("project",))
    def project_metadata_set(self, title=None, abstract=None, keywords=None):
        project = QgsProject.instance()
        metadata = project.metadata()
        changed = {}
        if title is not None:
            metadata.setTitle(str(title))
            changed["title"] = str(title)
        if abstract is not None:
            metadata.setAbstract(str(abstract))
            changed["abstract"] = str(abstract)
        if keywords is not None:
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]
            metadata.setKeywords(list(keywords))
            changed["keywords"] = list(keywords)
        project.setMetadata(metadata)
        project.setDirty(True)
        return {"changed": changed}


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------
def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _crs_units(crs):
    try:
        units = crs.mapUnits()
        return getattr(units, "name", None) or str(units)
    except Exception:  # noqa: BLE001
        return None


def _project_extent(project):
    rect = None
    try:
        for layer in project.mapLayers().values():
            try:
                if not layer.isValid():
                    continue
                layer_rect = layer.extent()
                if layer_rect.isNull() or layer_rect.isEmpty():
                    continue
                if rect is None:
                    rect = QgsRectangle(layer_rect)
                else:
                    rect.combineExtentWith(layer_rect)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return compat.rect_to_dict(rect) if rect is not None else None


def _bookmark_dict(bookmark):
    crs = None
    try:
        crs = bookmark.crs()
    except Exception:  # noqa: BLE001
        pass
    return {
        "id": str(bookmark.id()),
        "name": bookmark.name(),
        "group": bookmark.group(),
        "extent": compat.rect_to_dict(QgsRectangle(bookmark.extent())),
        "crs": crs.authid() if crs is not None and crs.isValid() else "",
    }


def _make_bookmark(name, group, rect, crs):
    bookmark = QgsBookmark()
    bookmark.setName(name)
    bookmark.setGroup(group)
    try:
        bookmark.setExtent(QgsReferencedRectangle(rect, crs))
    except Exception:  # noqa: BLE001 - older signature fallback
        bookmark.setExtent(rect)
        try:
            bookmark.setCrs(crs)
        except Exception:  # noqa: BLE001
            pass
    return bookmark
