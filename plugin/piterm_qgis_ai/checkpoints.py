"""Project checkpoints: cheap full-project snapshots the AI can roll back to.

A checkpoint writes the live project (including memory layers) to a .qgz in the
plugin's private storage dir. Restoring uses :func:`compat.read_project_guarded`
so a broken layer reference can never freeze the socket with a modal dialog.
"""

from __future__ import annotations

import json
import os
import shutil
import time

from .constants import CHECKPOINT_MAX
from .errors import io_error, not_found


class Checkpoints:
    def __init__(self, directory):
        self.directory = directory
        self.index_path = os.path.join(directory, "index.json")
        self._items = []
        os.makedirs(directory, exist_ok=True)
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._items = data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            self._items = []

    def _save(self):
        try:
            with open(self.index_path, "w", encoding="utf-8") as fh:
                json.dump(self._items, fh, ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass

    # -- api ----------------------------------------------------------------
    def create(self, name="", note="", author="ai"):
        from qgis.core import QgsProject
        project = QgsProject.instance()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        cid = "ckpt-%s-%d" % (stamp, len(self._items) + 1)
        filename = "%s.qgz" % cid
        path = os.path.join(self.directory, filename)
        try:
            ok = project.write(path)
        except Exception as exc:  # noqa: BLE001
            raise io_error("checkpoint write failed: %s" % exc)
        if not ok:
            raise io_error("QgsProject.write() reported failure for %s" % path)
        title = name or os.path.basename(project.fileName() or "") or "untitled"
        entry = {
            "id": cid,
            "name": str(title)[:120],
            "note": str(note)[:300],
            "created": time.time(),
            "created_text": time.strftime("%Y-%m-%d %H:%M:%S"),
            "path": path,
            "layer_count": len(project.mapLayers()),
            "author": author,
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
        }
        self._items.append(entry)
        self._prune()
        self._save()
        return entry

    def list(self):
        return [dict(item) for item in self._items]

    def get(self, cid):
        for item in self._items:
            if item["id"] == cid:
                return item
        raise not_found("no checkpoint with id %r" % cid,
                        detail={"available": [i["id"] for i in self._items][-10:]})

    def restore(self, cid):
        from . import compat
        entry = self.get(cid)
        if not os.path.exists(entry["path"]):
            raise not_found("checkpoint file is missing: %s" % entry["path"])
        ok = compat.read_project_guarded(entry["path"])
        return {"restored": cid, "name": entry["name"], "ok": bool(ok),
                "layers": entry.get("layer_count")}

    def delete(self, cid):
        entry = self.get(cid)
        try:
            if os.path.exists(entry["path"]):
                os.remove(entry["path"])
        except Exception:  # noqa: BLE001
            pass
        self._items = [i for i in self._items if i["id"] != cid]
        self._save()
        return {"deleted": cid}

    def clear(self):
        count = len(self._items)
        for item in self._items:
            try:
                if os.path.exists(item["path"]):
                    os.remove(item["path"])
            except Exception:  # noqa: BLE001
                pass
        self._items = []
        self._save()
        return {"cleared": count}

    def purge_directory(self):
        try:
            shutil.rmtree(self.directory, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    # -- internals ----------------------------------------------------------
    def _prune(self):
        while len(self._items) > CHECKPOINT_MAX:
            oldest = self._items.pop(0)
            try:
                if os.path.exists(oldest["path"]):
                    os.remove(oldest["path"])
            except Exception:  # noqa: BLE001
                pass
