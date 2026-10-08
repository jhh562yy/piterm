"""Bounded cursors for chunked reads (large feature queries etc.).

``features.query`` returns everything in one frame; when a client wants tens of
thousands of features it opens a cursor instead and pulls pages, which keeps
single frames small and memory predictable.
"""

from __future__ import annotations

import itertools
import time

from .constants import CURSOR_MAX_OPEN, CURSOR_TTL_SECONDS
from .errors import not_found


class Cursor:
    __slots__ = ("id", "kind", "meta", "created", "touched", "done", "fetched",
                 "_factory", "_iterator")

    def __init__(self, cursor_id, kind, factory, meta):
        self.id = cursor_id
        self.kind = kind
        self.meta = meta or {}
        self.created = time.time()
        self.touched = self.created
        self.done = False
        self.fetched = 0
        self._factory = factory
        self._iterator = None

    def next_page(self, n):
        if self.done:
            return [], True
        if self._iterator is None:
            self._iterator = iter(self._factory())
        out = []
        try:
            for _ in range(max(1, int(n))):
                out.append(next(self._iterator))
        except StopIteration:
            self.done = True
        self.touched = time.time()
        self.fetched += len(out)
        return out, self.done


class CursorManager:
    def __init__(self, ttl=CURSOR_TTL_SECONDS, max_open=CURSOR_MAX_OPEN):
        self.ttl = ttl
        self.max_open = max_open
        self._cursors = {}
        self._counter = itertools.count(1)

    def expire(self):
        now = time.time()
        stale = [cid for cid, cur in self._cursors.items()
                 if now - cur.touched > self.ttl]
        for cid in stale:
            del self._cursors[cid]
        return len(stale)

    def open(self, kind, factory, meta=None):
        self.expire()
        if len(self._cursors) >= self.max_open:
            oldest = min(self._cursors.values(), key=lambda c: c.touched)
            del self._cursors[oldest.id]
        cursor_id = "cur-%d" % next(self._counter)
        cursor = Cursor(cursor_id, kind, factory, meta)
        self._cursors[cursor_id] = cursor
        return {"cursor": cursor_id, "kind": kind, "meta": cursor.meta}

    def get(self, cursor_id):
        cursor = self._cursors.get(cursor_id)
        if cursor is None:
            raise not_found("no such cursor: %s" % cursor_id)
        return cursor

    def fetch(self, cursor_id, n):
        cursor = self.get(cursor_id)
        items, done = cursor.next_page(n)
        return {"cursor": cursor_id, "items": items, "done": done,
                "fetched": cursor.fetched}

    def close(self, cursor_id):
        self._cursors.pop(cursor_id, None)
        return {"closed": cursor_id}

    def list(self):
        self.expire()
        return [{"cursor": c.id, "kind": c.kind, "fetched": c.fetched,
                 "done": c.done, "age_s": round(time.time() - c.created, 1)}
                for c in self._cursors.values()]
