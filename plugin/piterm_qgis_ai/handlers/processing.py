"""Processing: algorithm discovery, execution (sync + background jobs), models,
batch runs and the generic job API.

The sync runner pumps the Qt event loop while an algorithm runs (so QGIS stays
alive) and enforces a plugin-side deadline slightly below the client timeout -
the plugin gives up first and says why, instead of the client abandoning a
response while QGIS keeps crunching.
"""

from __future__ import annotations

import collections
import os
import time

from qgis.core import (QgsApplication, QgsMapLayer, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject)

from .. import compat
from ..constants import DEFAULT_SYNC_TIMEOUT
from ..errors import CommandError, ErrorCode, bad_request, invalid, not_found
from ..registry import command
from .base import HandlerBase, value_to_json

_SOURCE_TYPES = {"source", "vector", "raster", "layer", "maplayer", "mesh",
                 "pointcloud", "vectorlayer", "rasterlayer", "featurestore"}
_SINK_TYPES = {"sink", "vectorDestination", "rasterDestination", "fileDestination",
               "folderDestination", "filedestination", "folderdestination",
               "vectordestination", "rasterdestination"}


# ---------------------------------------------------------------------------
# shared machinery (also used by raster/zonal helpers)
# ---------------------------------------------------------------------------
class ResponsiveFeedback(QgsProcessingFeedback):
    """Feedback that pumps the event loop and enforces a deadline."""

    def __init__(self, deadline, on_progress=None, pump=True):
        super().__init__()
        self.deadline = deadline
        self.on_progress = on_progress
        self.pump = pump
        self.lines = collections.deque(maxlen=300)
        self._last_pump = 0.0

    def setProgress(self, progress):  # noqa: N802 (Qt override)
        now = time.monotonic()
        if self.pump and (now - self._last_pump) > 0.05:
            try:
                from qgis.PyQt.QtCore import QCoreApplication
                QCoreApplication.processEvents()
            except Exception:  # noqa: BLE001
                pass
            self._last_pump = now
        if now > self.deadline:
            self.cancel()
        if self.on_progress:
            try:
                self.on_progress(progress)
            except Exception:  # noqa: BLE001
                pass

    def pushInfo(self, info):  # noqa: N802
        self.lines.append(str(info)[:500])

    def pushCommandInfo(self, info):  # noqa: N802
        self.lines.append(str(info)[:500])

    def pushDebugInfo(self, info):  # noqa: N802
        self.lines.append(str(info)[:500])

    def reportError(self, error, fatalError=False):  # noqa: N802
        self.lines.append("ERROR: %s" % str(error)[:500])
        if fatalError:
            self.cancel()


def _ensure_processing_initialized():
    """Make sure Processing providers exist.

    Inside the QGIS GUI the Processing plugin initializes them at startup; in
    headless embeds (scripts, CI, tests) nothing does, so every enumeration
    entry point calls this cheap guard first. Attempts, in order: full
    ``Processing.initialize()`` (processing plugin on sys.path), the C++
    ``QgsNativeAlgorithms`` provider, then the registry's own hook. All are
    best-effort: when none works, the caller sees zero providers and says so.
    """
    try:
        registry = QgsApplication.processingRegistry()
    except Exception:  # noqa: BLE001
        return
    try:
        if registry.providers():
            return
    except Exception:  # noqa: BLE001
        pass

    # 1) full Processing plugin init (adds qgis + gdal + native + ...)
    try:
        import os
        import sys
        plugins_dir = os.path.join(QgsApplication.prefixPath(), "python",
                                   "plugins")
        if os.path.isdir(plugins_dir) and plugins_dir not in sys.path:
            sys.path.append(plugins_dir)
        from processing.core.Processing import Processing
        Processing.initialize()
        if registry.providers():
            return
    except Exception:  # noqa: BLE001
        pass

    # 2) C++ native algorithms provider (headless-safe, no plugin needed)
    for module_name in ("qgis.analysis", "qgis.core"):
        try:
            module = __import__(module_name, fromlist=["QgsNativeAlgorithms"])
            provider_class = getattr(module, "QgsNativeAlgorithms", None)
            if provider_class is None:
                continue
            registry.addProvider(provider_class())
            if registry.providers():
                return
        except Exception:  # noqa: BLE001
            continue

    # 3) last resort: whatever the registry itself can load
    try:
        registry.initializeProviders()
    except Exception:  # noqa: BLE001
        pass


def find_algorithm(spec):
    """Resolve an algorithm by id, then by exact/partial display name."""
    _ensure_processing_initialized()
    registry = QgsApplication.processingRegistry()
    text = str(spec)
    alg = registry.algorithmById(text)
    if alg is not None:
        return alg
    lowered = text.casefold()
    partial = []
    for candidate in registry.algorithms():
        if candidate.displayName().casefold() == lowered:
            return candidate
        if lowered in candidate.id().casefold() or lowered in candidate.displayName().casefold():
            partial.append(candidate)
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise CommandError(ErrorCode.AMBIGUOUS,
                           "'%s' matches %d algorithms" % (spec, len(partial)),
                           detail={"candidates": [c.id() for c in partial[:15]]})
    import difflib
    suggestions = difflib.get_close_matches(
        text, [a.id() for a in registry.algorithms()], n=6, cutoff=0.5)
    raise not_found("no processing algorithm %r" % spec,
                    detail={"suggestions": suggestions})


def prepare_parameters(handler, algorithm, parameters):
    """Convert friendly values (layer names/ids, paths, 'memory:x') to what the
    algorithm expects; returns (params, warnings)."""
    definitions = {d.name(): d for d in algorithm.parameterDefinitions()}
    prepared = {}
    warnings = []
    for name, raw in (parameters or {}).items():
        definition = definitions.get(name)
        if definition is None:
            warnings.append("unknown parameter %r passed through" % name)
            prepared[name] = raw
            continue
        prepared[name] = _convert_value(handler, definition, raw, warnings)
    return prepared, warnings


def _convert_value(handler, definition, raw, warnings):
    ptype = str(definition.type() or "").lower()
    if isinstance(raw, str):
        text = raw.strip()
        if ptype in _SOURCE_TYPES or ptype.endswith("source"):
            if text in ("", "None"):
                return None
            layer = _match_layer(handler, text)
            if layer is not None:
                return layer
            if _looks_like_path(text):
                return handler.check_path(text, for_write=False)
            return raw
        if ptype in _SINK_TYPES or ptype.endswith("destination"):
            if text.startswith(("memory:", "TEMPORARY_OUTPUT", "temp:",
                                "in_memory:")) or text == "":
                return raw
            if _looks_like_path(text):
                return handler.check_path(text, for_write=True)
            return raw
        if ptype == "boolean":
            return text.lower() in ("1", "true", "yes", "on")
        if ptype in ("filedestination", "file", "folder", "folderdestination") \
                and _looks_like_path(text) and not text.startswith("memory:"):
            return handler.check_path(text, for_write=ptype.startswith("file") and
                                      "destination" in ptype)
    if isinstance(raw, (list, tuple)) and ptype in ("source", "vector", "raster"):
        layers = []
        for item in raw:
            layers.append(_match_layer(handler, item) or item)
        return layers
    return raw


def _match_layer(handler, text):
    try:
        return handler.resolve_layer({"layer": text}, kind=None, required=False)
    except CommandError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _looks_like_path(text):
    if len(text) < 3:
        return False
    if "://" in text:
        return False
    if text.startswith(("http", "wms", "wfs", "postgres", "mysql", "mssql",
                        "oracle", "spatialite", "pointcloud")):
        return False
    return ("\\" in text or "/" in text or ":" in text[:3]
            or text.lower().endswith((".shp", ".gpkg", ".tif", ".tiff", ".csv",
                                      ".geojson", ".json", ".qml", ".kml",
                                      ".gml", ".fgb", ".parquet", ".vrt",
                                      ".img", ".asc", ".xyz", ".nc")))


def run_processing_sync(handler, algorithm, parameters, timeout=DEFAULT_SYNC_TIMEOUT,
                        load_results=True, feedback_progress=None):
    """Run an algorithm inline; convert result layers; optionally load them."""
    alg = find_algorithm(algorithm)
    prepared, warnings = prepare_parameters(handler, alg, parameters)
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = ResponsiveFeedback(
        time.monotonic() + float(timeout or DEFAULT_SYNC_TIMEOUT),
        on_progress=feedback_progress)
    try:
        results = _run(alg, prepared, context, feedback)
    except Exception as exc:  # noqa: BLE001
        detail = {"log_tail": list(feedback.lines)[-20:]}
        if feedback.isCanceled():
            raise CommandError(ErrorCode.TIMEOUT,
                               "algorithm exceeded %ss (plugin-side timeout)"
                               % timeout, detail=detail)
        raise invalid("algorithm failed: %s" % exc, detail=detail)
    if feedback.isCanceled():
        raise CommandError(ErrorCode.TIMEOUT,
                           "algorithm exceeded %ss (plugin-side timeout)" % timeout,
                           detail={"log_tail": list(feedback.lines)[-20:]})
    payload = {"algorithm": alg.id(), "warnings": warnings,
               "log_tail": list(feedback.lines)[-15:]}
    payload.update(serialize_results(results, load_results))
    return payload


def _run(alg, prepared, context, feedback):
    from qgis import processing
    attempts = (
        lambda: processing.run(alg.id(), prepared, context=context,
                               feedback=feedback),
        lambda: processing.run(alg.id(), prepared, feedback=feedback),
        lambda: processing.run(alg.id(), prepared),
    )
    last_error = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:  # signature differences across QGIS versions
            last_error = exc
            continue
    raise last_error


def serialize_results(results, load_results):
    out = {}
    loaded = []
    project = QgsProject.instance()
    for key, value in (results or {}).items():
        if isinstance(value, QgsMapLayer):
            entry = None
            try:
                entry = {"id": value.id(), "name": value.name(),
                         "kind": compat.layer_kind(value),
                         "source": value.publicSource()[:200]}
            except Exception:  # noqa: BLE001
                entry = {"name": getattr(value, "name", lambda: key)()}
            out[key] = entry
            if load_results:
                try:
                    if project.mapLayer(value.id()) is None:
                        project.addMapLayer(value)
                        loaded.append({"key": key, **entry})
                except Exception:  # noqa: BLE001
                    pass
        else:
            out[key] = value_to_json(value)
    return {"result": out, "loaded_layers": loaded}


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------
class ProcessingHandlers(HandlerBase):

    @command(name="processing.providers", summary="Processing providers with "
             "algorithm counts", risk="read", tags=("processing",))
    def processing_providers(self):
        _ensure_processing_initialized()
        registry = QgsApplication.processingRegistry()
        out = []
        for provider in registry.providers():
            entry = {"id": provider.id(), "name": provider.name()}
            try:
                entry["algorithm_count"] = len(provider.algorithms())
            except Exception:  # noqa: BLE001
                try:
                    entry["algorithm_count"] = provider.algorithmCount()
                except Exception:  # noqa: BLE001
                    entry["algorithm_count"] = None
            try:
                entry["active"] = bool(provider.isActive())
            except Exception:  # noqa: BLE001
                entry["active"] = None
            out.append(entry)
        out.sort(key=lambda e: -(e.get("algorithm_count") or 0))
        return {"providers": out,
                "total_algorithms": sum(e.get("algorithm_count") or 0 for e in out)}

    @command(name="processing.algorithms_search", summary="Search algorithms by "
             "keyword (id, name, group)", risk="read", tags=("processing",))
    def processing_algorithms_search(self, query=None, provider=None, limit=50,
                                     include_deprecated=False):
        _ensure_processing_initialized()
        registry = QgsApplication.processingRegistry()
        text = str(query).casefold() if query else None
        out = []
        for alg in registry.algorithms():
            try:
                if not include_deprecated and alg.flags() & alg.Flag.Deprecated:
                    continue
            except Exception:  # noqa: BLE001
                pass
            if provider and not alg.id().startswith(str(provider) + ":"):
                if not (alg.provider() and alg.provider().id() == provider):
                    continue
            haystack = (alg.id() + " " + alg.displayName() + " " + str(alg.group() or "")).casefold()
            if text and text not in haystack:
                continue
            out.append({"id": alg.id(), "name": alg.displayName(),
                        "group": str(alg.group() or ""),
                        "provider": alg.provider().id() if alg.provider() else ""})
            if len(out) >= max(1, min(int(limit), 2000)):
                break
        return {"count": len(out), "algorithms": out}

    @command(name="processing.algorithms_get", summary="Algorithm parameters, "
             "outputs and help", risk="read", tags=("processing",))
    def processing_algorithms_get(self, algorithm):
        alg = find_algorithm(algorithm)
        parameters = []
        for definition in alg.parameterDefinitions():
            entry = {
                "name": definition.name(),
                "description": definition.description(),
                "type": str(definition.type()),
                "default": value_to_json(definition.defaultValue()),
            }
            try:
                entry["optional"] = bool(definition.flags() & definition.Flag.Optional)
            except Exception:  # noqa: BLE001
                pass
            try:
                options = definition.options()
                if options:
                    entry["options"] = [str(o) for o in options][:60]
            except Exception:  # noqa: BLE001
                pass
            parameters.append(entry)
        outputs = []
        for output in alg.outputDefinitions():
            outputs.append({"name": output.name(), "description": output.description(),
                            "type": str(output.type())})
        help_text = ""
        for attr in ("shortHelpString", "shortDescription"):
            fn = getattr(alg, attr, None)
            if callable(fn):
                try:
                    help_text = fn() or ""
                    if help_text:
                        break
                except Exception:  # noqa: BLE001
                    continue
        return {"id": alg.id(), "name": alg.displayName(),
                "group": str(alg.group() or ""), "parameters": parameters,
                "outputs": outputs, "help": help_text[:4000]}

    @command(name="processing.execute", summary="Run a processing algorithm "
             "synchronously (plugin-side timeout, results optionally loaded)",
             risk="execute", tags=("processing",))
    def processing_execute(self, algorithm, parameters, timeout=DEFAULT_SYNC_TIMEOUT,
                           load_results=True):
        return run_processing_sync(self, algorithm, parameters,
                                   timeout=timeout, load_results=load_results)

    @command(name="processing.start", summary="Run a processing algorithm as a "
             "background job (no time limit, progress via job.*)",
             risk="execute", tags=("processing",))
    def processing_start(self, algorithm, parameters, timeout=None,
                         load_results=True):
        from qgis.core import QgsProcessingAlgRunnerTask
        alg = find_algorithm(algorithm)
        prepared, warnings = prepare_parameters(self, alg, parameters)
        job = self.srv.jobs.create("processing", alg.displayName(),
                                   {"algorithm": alg.id()})
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        deadline = time.monotonic() + (float(timeout) if timeout else 24 * 3600)
        feedback = ResponsiveFeedback(deadline, pump=False,
                                      on_progress=lambda p: self.srv.jobs.update(
                                          job, progress=p))
        task = QgsProcessingAlgRunnerTask(alg, prepared, context, feedback)
        load_flag = bool(load_results)
        # Keep task/context/feedback alive until the task finishes; the C++
        # task only holds raw pointers, and GC during the run is a heap crash.
        self.srv.qt_tasks[job.id] = {"task": task, "context": context,
                                     "feedback": feedback}

        def _finished(*args):
            self._finish_processing_job(job, task, feedback, args, load_flag, warnings)

        try:
            task.executed.connect(_finished)
        except Exception:  # noqa: BLE001
            task.executed.connect(lambda *a: _finished(*a))
        try:
            task.begun.connect(lambda: self.srv.jobs.mark_running(job))
        except Exception:  # noqa: BLE001
            self.srv.jobs.mark_running(job)
        self.srv.jobs.register_cancel(job, task.cancel)
        QgsApplication.taskManager().addTask(task)
        return {"job": job.id, "algorithm": alg.id(), "state": job.state}

    def _finish_processing_job(self, job, task, feedback, args, load_results,
                               warnings):
        if job.terminal:
            return
        success = None
        results = None
        for arg in args:
            if isinstance(arg, bool) and success is None:
                success = arg
            if isinstance(arg, dict) and results is None:
                results = arg
        if results is None:
            for attr in ("results",):
                fn = getattr(task, attr, None)
                if callable(fn):
                    try:
                        results = fn()
                        break
                    except Exception:  # noqa: BLE001
                        pass
        if success is None:
            try:
                success = bool(results)
            except Exception:  # noqa: BLE001
                success = False
        if job.cancel_requested:
            self._drop_task(job)
            return  # manager already marked cancelled
        if success:
            payload = {"algorithm": job.params.get("algorithm"),
                       "warnings": warnings,
                       "log_tail": list(feedback.lines)[-15:]}
            payload.update(serialize_results(results or {}, load_results))
            self.srv.jobs.succeed(job, payload)
        else:
            tail = list(feedback.lines)
            self.srv.jobs.fail(job, tail[-1] if tail else "algorithm reported failure")
        self._drop_task(job)

    def _drop_task(self, job):
        try:
            self.srv.qt_tasks.pop(job.id, None)
        except Exception:  # noqa: BLE001
            pass

    @command(name="processing.batch_execute", summary="Run one algorithm over "
             "many parameter sets (per-run status)", risk="execute",
             tags=("processing",))
    def processing_batch_execute(self, algorithm, parameter_sets, timeout=55,
                                 stop_on_error=False, load_results=True):
        if not isinstance(parameter_sets, list) or not parameter_sets:
            raise bad_request("'parameter_sets' must be a non-empty list")
        results = []
        for index, params in enumerate(parameter_sets):
            try:
                outcome = run_processing_sync(self, algorithm, params or {},
                                              timeout=timeout,
                                              load_results=load_results)
                results.append({"index": index, "ok": True,
                                "algorithm": outcome.get("algorithm"),
                                "loaded_layers": outcome.get("loaded_layers")})
            except CommandError as exc:
                results.append({"index": index, "ok": False,
                                "error": {"code": exc.code, "message": exc.message}})
                if stop_on_error:
                    break
        return {"count": len(results),
                "ok_count": sum(1 for r in results if r["ok"]),
                "results": results}

    @command(name="processing.models_list", summary="Registered Processing "
             "models (.model3)", risk="read", tags=("processing", "models"))
    def processing_models_list(self):
        _ensure_processing_initialized()
        registry = QgsApplication.processingRegistry()
        out = []
        for alg in registry.algorithms():
            try:
                provider = alg.provider()
                if provider is not None and provider.id() == "model":
                    out.append({"id": alg.id(), "name": alg.displayName(),
                                "group": str(alg.group() or "")})
            except Exception:  # noqa: BLE001
                continue
        return {"count": len(out), "models": out}

    @command(name="processing.models_run", summary="Run a Processing model by id",
             risk="execute", tags=("processing", "models"))
    def processing_models_run(self, model, parameters=None,
                              timeout=DEFAULT_SYNC_TIMEOUT, load_results=True):
        return run_processing_sync(self, model, parameters or {},
                                   timeout=timeout, load_results=load_results)


class JobHandlers(HandlerBase):

    @command(name="job.list", summary="List background jobs (processing, renders)",
             risk="read", tags=("jobs",))
    def job_list(self, kind=None, state=None, limit=100):
        return {"jobs": self.srv.jobs.list(kind=kind, state=state, limit=limit)}

    @command(name="job.get", summary="Job state, progress, result or error",
             risk="read", tags=("jobs",))
    def job_get(self, id, with_log=True, log_limit=50):
        job = self.srv.jobs.get(str(id))
        if job is None:
            raise not_found("no job %r" % id,
                            detail={"open": [j["id"] for j in
                                             self.srv.jobs.list(limit=20)]})
        return job.to_dict(with_log=with_log, log_limit=log_limit)

    @command(name="job.cancel", summary="Ask a running job to stop",
             risk="write", tags=("jobs",))
    def job_cancel(self, id):
        result = self.srv.jobs.request_cancel(str(id))
        if result is None:
            raise not_found("no job %r" % id)
        return result

    @command(name="job.clear", summary="Drop finished jobs from the list",
             risk="write", tags=("jobs",))
    def job_clear(self):
        return self.srv.jobs.clear_finished()

    @command(name="job.log", summary="Job log lines", risk="read", tags=("jobs",))
    def job_log(self, id, limit=100):
        job = self.srv.jobs.get(str(id))
        if job is None:
            raise not_found("no job %r" % id)
        return {"id": job.id, "lines": list(job.log)[-max(1, int(limit)):]}
