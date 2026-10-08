"""Session commands: checkpoints, journal (audit replay), edit sessions with
undo/redo, and the workflow runner (multi-step scripts with ``$N.field``
references, dry-run mode and per-step policy checks)."""

from __future__ import annotations

import fnmatch
import time

from ..errors import bad_request, invalid, not_found, state
from ..registry import command
from .base import HandlerBase


class SessionHandlers(HandlerBase):

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------
    @command(name="session.checkpoint_create", summary="Snapshot the whole "
             "project (including memory layers) for rollback", risk="write",
             tags=("session", "checkpoint"))
    def session_checkpoint_create(self, name=None, note=None):
        entry = self.srv.checkpoints.create(name or "", note or "")
        return {"checkpoint": entry["id"], "name": entry["name"],
                "created": entry["created_text"], "layers": entry["layer_count"],
                "size": entry["size"]}

    @command(name="session.checkpoint_list", summary="List checkpoints",
             risk="read", tags=("session", "checkpoint"))
    def session_checkpoint_list(self):
        items = self.srv.checkpoints.list()
        for item in items:
            item.pop("path", None)
        return {"count": len(items), "checkpoints": items}

    @command(name="session.checkpoint_restore", summary="Restore the project to "
             "a checkpoint", risk="destructive", tags=("session", "checkpoint"))
    def session_checkpoint_restore(self, checkpoint):
        return self.srv.checkpoints.restore(str(checkpoint))

    @command(name="session.checkpoint_delete", summary="Delete a checkpoint",
             risk="destructive", tags=("session", "checkpoint"))
    def session_checkpoint_delete(self, checkpoint):
        return self.srv.checkpoints.delete(str(checkpoint))

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------
    @command(name="session.journal_list", summary="Recorded session commands "
             "(mutations)", risk="read", tags=("session", "journal"))
    def session_journal_list(self, since_seq=0, limit=500):
        entries = self.srv.journal.list(since_seq, limit)
        return {"count": len(entries), "entries": entries}

    @command(name="session.journal_export", summary="Export the session as a "
             "replay script (.py) or JSONL", risk="write",
             tags=("session", "journal"))
    def session_journal_export(self, path, format="python", only_ok=False):
        out = self.check_path(path, for_write=True)
        return self.srv.journal.export(out, fmt=str(format), only_ok=bool(only_ok))

    @command(name="session.journal_clear", summary="Clear the journal",
             risk="write", tags=("session", "journal"))
    def session_journal_clear(self):
        return self.srv.journal.clear()

    # ------------------------------------------------------------------
    # Edit sessions / undo-redo
    # ------------------------------------------------------------------
    @command(name="session.edit_begin", summary="Open an undoable edit session "
             "on a layer (subsequent writes are buffered)", risk="write",
             tags=("session", "editing"))
    def session_edit_begin(self, layer):
        target = self.resolve_vector({"layer": layer})
        if target.isEditable():
            return {"layer": target.id(), "editable": True,
                    "note": "already editing"}
        if not target.startEditing():
            raise state("could not start editing on %s" % target.name())
        return {"layer": target.id(), "editable": True}

    @command(name="session.edit_commit", summary="Commit the edit buffer",
             risk="write", tags=("session", "editing"))
    def session_edit_commit(self, layer):
        target = self.resolve_vector({"layer": layer})
        if not target.isEditable():
            return {"layer": target.id(), "committed": False,
                    "note": "layer is not in an edit session"}
        result = target.commitChanges()
        if isinstance(result, tuple):
            ok, errors = result
        else:
            ok = bool(result)
            errors = list(getattr(target, "commitErrors", lambda: [])() or [])
        if not ok:
            raise invalid("commit failed: %s" % "; ".join(str(e) for e in errors[:5]),
                          detail={"errors": [str(e) for e in errors[:10]]})
        return {"layer": target.id(), "committed": True}

    @command(name="session.edit_rollback", summary="Discard every uncommitted "
             "change", risk="destructive", tags=("session", "editing"))
    def session_edit_rollback(self, layer):
        target = self.resolve_vector({"layer": layer})
        if not target.isEditable():
            return {"layer": target.id(), "rolled_back": False,
                    "note": "layer is not in an edit session"}
        if hasattr(target, "rollBack"):
            target.rollBack()
        else:  # very old bindings
            target.rollback()
        return {"layer": target.id(), "rolled_back": True}

    @command(name="session.edit_status", summary="Editing state and undo/redo "
             "availability", risk="read", tags=("session", "editing"))
    def session_edit_status(self, layer):
        target = self.resolve_vector({"layer": layer})
        out = {"layer": target.id(), "editable": bool(target.isEditable())}
        try:
            out["modified"] = bool(target.isModified())
        except Exception:  # noqa: BLE001
            pass
        try:
            stack = target.undoStack()
            out["undo_count"] = stack.count()
            out["undo_index"] = stack.index()
            out["can_undo"] = stack.canUndo()
            out["can_redo"] = stack.canRedo()
        except Exception:  # noqa: BLE001
            pass
        return out

    @command(name="session.undo", summary="Undo N steps on the layer's own "
             "undo stack", risk="write", tags=("session", "editing"))
    def session_undo(self, layer, steps=1):
        target = self.resolve_vector({"layer": layer})
        stack = target.undoStack()
        done = 0
        for _ in range(max(1, int(steps))):
            if not stack.canUndo():
                break
            stack.undo()
            done += 1
        return {"layer": target.id(), "undone": done, "can_undo": stack.canUndo()}

    @command(name="session.redo", summary="Redo N steps on the layer's undo "
             "stack", risk="write", tags=("session", "editing"))
    def session_redo(self, layer, steps=1):
        target = self.resolve_vector({"layer": layer})
        stack = target.undoStack()
        done = 0
        for _ in range(max(1, int(steps))):
            if not stack.canRedo():
                break
            stack.redo()
            done += 1
        return {"layer": target.id(), "redone": done, "can_redo": stack.canRedo()}

    # ------------------------------------------------------------------
    # Workflow runner
    # ------------------------------------------------------------------
    @command(name="session.workflow_run", summary="Run a multi-step command "
             "workflow with $N.field references, dry-run and stop-on-error",
             risk="read", tags=("session", "workflow"))
    def session_workflow_run(self, steps, stop_on_error=True, dry_run=False,
                             max_steps=200):
        if not isinstance(steps, list) or not steps:
            raise bad_request("'steps' must be a non-empty list of "
                              "{type, params}")
        if len(steps) > int(max_steps):
            raise bad_request("workflow exceeds %s steps" % max_steps)
        from ..registry import COMMANDS
        dispatcher = self.srv.dispatcher
        ctx = dispatcher.current_ctx
        results = []
        outputs = []
        started = time.perf_counter()
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                results.append({"index": index, "ok": False,
                                "error": {"code": "E_BAD_REQUEST",
                                          "message": "step must be an object"}})
                if stop_on_error:
                    break
                continue
            ctype = step.get("type") or step.get("command")
            if not ctype:
                results.append({"index": index, "ok": False,
                                "error": {"code": "E_BAD_REQUEST",
                                          "message": "step without 'type'"}})
                if stop_on_error:
                    break
                continue
            if ctype in ("session.workflow_run", "system.batch", "system.shutdown"):
                results.append({"index": index, "type": ctype, "ok": False,
                                "error": {"code": "E_FORBIDDEN",
                                          "message": "%s cannot run inside a "
                                                     "workflow" % ctype}})
                if stop_on_error:
                    break
                continue
            params = _resolve_refs(step.get("params") or {}, outputs)
            if dry_run:
                spec = COMMANDS.get(ctype)
                if spec is None:
                    results.append({"index": index, "type": ctype, "ok": False,
                                    "error": {"code": "E_UNKNOWN_COMMAND",
                                              "message": "unknown command"}})
                    if stop_on_error:
                        break
                    continue
                try:
                    self.srv.security.check_command(spec, params)
                    results.append({"index": index, "type": ctype, "ok": True,
                                    "dry_run": True, "risk": spec.risk})
                    outputs.append({"dry_run": True})
                except Exception as exc:  # noqa: BLE001
                    results.append({"index": index, "type": ctype, "ok": False,
                                    "error": {"code": getattr(exc, "code",
                                                              "E_FORBIDDEN"),
                                              "message": str(exc)}})
                    if stop_on_error:
                        break
                continue
            response = dispatcher.execute_single(ctx, ctype, params,
                                                 step.get("id"))
            entry = {"index": index, "type": ctype, "ok": bool(response.get("ok"))}
            if response.get("ok"):
                entry["result"] = response.get("result")
                outputs.append(response.get("result"))
            else:
                entry["error"] = response.get("error")
                outputs.append(None)
            results.append(entry)
            if not response.get("ok") and stop_on_error:
                break
        return {
            "count": len(results),
            "ok_count": sum(1 for r in results if r.get("ok")),
            "stopped_early": len(results) < len(steps),
            "dry_run": bool(dry_run),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "results": results,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resolve_refs(value, outputs, depth=0):
    """Replace ``$0.field.sub`` / ``$1.result.id`` references in params."""
    if depth > 6:
        return value
    if isinstance(value, str):
        return _resolve_string(value, outputs)
    if isinstance(value, dict):
        return {k: _resolve_refs(v, outputs, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, outputs, depth + 1) for v in value]
    return value


def _resolve_string(text, outputs):
    if "$" not in text:
        return text
    stripped = text.strip()
    if stripped.startswith("$") and " " not in stripped:
        resolved = _lookup(stripped, outputs)
        if resolved is not _MISSING:
            return resolved
        return text
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "$" and i + 1 < len(text) and text[i + 1].isdigit():
            j = i + 1
            while j < len(text) and (text[j].isdigit() or text[j] == "." or
                                     text[j].isalnum() or text[j] == "_"):
                j += 1
            token = text[i:j]
            resolved = _lookup(token, outputs)
            if resolved is _MISSING:
                out.append(token)
            else:
                out.append(str(resolved) if not isinstance(resolved, (int, float))
                           else str(resolved))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_MISSING = object()


def _lookup(token, outputs):
    parts = token[1:].split(".")
    if not parts or not parts[0].isdigit():
        return _MISSING
    index = int(parts[0])
    if index >= len(outputs):
        return _MISSING
    current = outputs[index]
    for part in parts[1:]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current
