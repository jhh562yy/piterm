"""Generic background job manager (processing runs, renders, exports, workflows).

QGIS-free on purpose: handlers start real work through ``QgsTask`` /
``QgsProcessingAlgRunnerTask`` and report progress into these plain objects,
which keeps the state machine unit-testable outside QGIS.
"""

from __future__ import annotations

import collections
import itertools
import threading
import time

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

_TERMINAL = (STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED)
MAX_JOBS = 200


class Job:
    __slots__ = ("id", "kind", "title", "params", "state", "progress", "message",
                 "created", "started", "finished", "result", "error", "log",
                 "cancel_requested", "_cancel_cb", "owner")

    def __init__(self, job_id, kind, title, params=None, owner=None):
        self.id = job_id
        self.kind = kind
        self.title = title
        self.params = params or {}
        self.state = STATE_QUEUED
        self.progress = 0
        self.message = ""
        self.created = time.time()
        self.started = None
        self.finished = None
        self.result = None
        self.error = None
        self.log = collections.deque(maxlen=200)
        self.cancel_requested = False
        self._cancel_cb = None
        self.owner = owner

    @property
    def terminal(self):
        return self.state in _TERMINAL

    def to_dict(self, with_log=False, log_limit=50):
        d = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "progress": self.progress,
            "message": self.message,
            "created": self.created,
        }
        if self.started:
            d["started"] = self.started
        if self.finished:
            d["finished"] = self.finished
            d["elapsed"] = round(self.finished - (self.started or self.created), 3)
        if self.state == STATE_SUCCEEDED:
            d["result"] = self.result
        elif self.state == STATE_FAILED:
            d["error"] = self.error
        if with_log:
            d["log"] = list(self.log)[-max(1, int(log_limit)):]
        return d

    # -- transitions (called by managers/handlers) --------------------------
    def mark_running(self):
        self.state = STATE_RUNNING
        self.started = time.time()
        self.progress = max(self.progress, 1)

    def update(self, progress=None, message=None):
        if progress is not None:
            try:
                self.progress = max(0, min(100, int(progress)))
            except Exception:  # noqa: BLE001
                pass
        if message is not None:
            self.message = str(message)[:500]

    def append_log(self, line):
        self.log.append("%s %s" % (time.strftime("%H:%M:%S"), str(line)[:400]))

    def succeed(self, result=None):
        self.state = STATE_SUCCEEDED
        self.finished = time.time()
        self.progress = 100
        self.result = result

    def fail(self, error):
        self.state = STATE_FAILED
        self.finished = time.time()
        self.error = str(error)[:2000]

    def cancel(self):
        self.state = STATE_CANCELLED
        self.finished = time.time()
        self.message = "cancelled"


class JobManager:
    def __init__(self, event_cb=None, max_jobs=MAX_JOBS):
        self._jobs = collections.OrderedDict()
        self._counter = itertools.count(1)
        self._lock = threading.RLock()
        self.event_cb = event_cb  # callable(event_name, payload)
        self.max_jobs = max_jobs

    # -- lifecycle ----------------------------------------------------------
    def create(self, kind, title, params=None, owner=None):
        with self._lock:
            job_id = "job-%d" % next(self._counter)
            job = Job(job_id, kind, title, params, owner)
            self._jobs[job_id] = job
            self._prune_locked()
        self._emit("job.created", job)
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, kind=None, state=None, limit=100):
        with self._lock:
            items = list(self._jobs.values())
        if kind:
            items = [j for j in items if j.kind == kind]
        if state:
            items = [j for j in items if j.state == state]
        items.sort(key=lambda j: j.created, reverse=True)
        return [j.to_dict() for j in items[:max(1, int(limit))]]

    def clear_finished(self):
        with self._lock:
            done = [jid for jid, job in self._jobs.items() if job.terminal]
            for jid in done:
                del self._jobs[jid]
        return {"cleared": len(done)}

    def register_cancel(self, job, callback):
        job._cancel_cb = callback

    def request_cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.terminal:
            return {"id": job_id, "state": job.state, "note": "already finished"}
        job.cancel_requested = True
        if job._cancel_cb:
            try:
                job._cancel_cb()
            except Exception as exc:  # noqa: BLE001
                job.append_log("cancel callback failed: %s" % exc)
        job.cancel()
        self._emit("job.cancelled", job)
        return {"id": job_id, "state": job.state}

    # -- updates ------------------------------------------------------------
    def mark_running(self, job):
        job.mark_running()
        self._emit("job.started", job)

    def update(self, job, progress=None, message=None):
        job.update(progress, message)
        self._emit("job.progress", job)

    def log(self, job, line):
        job.append_log(line)
        self._emit("job.log", job)

    def succeed(self, job, result=None):
        job.succeed(result)
        self._emit("job.succeeded", job)

    def fail(self, job, error):
        job.fail(error)
        self._emit("job.failed", job)

    # -- internals ----------------------------------------------------------
    def _emit(self, event, job):
        if not self.event_cb:
            return
        try:
            self.event_cb(event, {
                "job_id": job.id, "kind": job.kind, "state": job.state,
                "progress": job.progress, "message": job.message,
                "title": job.title,
            })
        except Exception:  # noqa: BLE001
            pass

    def _prune_locked(self):
        while len(self._jobs) > self.max_jobs:
            finished = [jid for jid, j in self._jobs.items() if j.terminal]
            if not finished:
                break
            del self._jobs[finished[0]]
