"""Session journal: every mutating wire command is recorded so a session can be
replayed, audited or turned into a Python script.

Writes a self-contained replay script that talks to the bridge with nothing but
the standard library - no QGIS, no MCP, no pip install needed on the runner.
"""

from __future__ import annotations

import copy
import json
import os
import time

from .constants import JOURNAL_MAX
from .security import _trim_params

_UNRECORDED = {
    # read-only noise never recorded (the journal is about changes)
    "ping", "hello", "help", "capabilities", "diagnose", "info", "metrics",
    "events.poll", "config.get", "audit.tail", "clients.list",
    "journal.list", "journal.export", "checkpoint.list", "job.list", "job.get",
    "cursor.open", "cursor.fetch", "cursor.close", "cursor.list",
}

# Commands that change something but must not be auto-replayed blindly.
_NEVER_REPLAY = {"shutdown", "python.exec", "python.exec_full", "workflow.run",
                 "journal.export", "macro.play"}


class Journal:
    def __init__(self, maxlen=JOURNAL_MAX):
        self.entries = []
        self.seq = 0
        self.maxlen = maxlen
        self.paused = False

    # -- recording ----------------------------------------------------------
    def record(self, cmd, params, status, duration_ms=None, client=None,
               result_note=None):
        if self.paused or cmd in _UNRECORDED:
            return None
        self.seq += 1
        entry = {
            "seq": self.seq,
            "ts": time.time(),
            "cmd": cmd,
            "params": _trim_params(copy.deepcopy(params or {})),
            "status": status,
        }
        if duration_ms is not None:
            entry["t_ms"] = round(duration_ms, 2)
        if client:
            entry["client"] = client
        if result_note:
            entry["note"] = str(result_note)[:300]
        self.entries.append(entry)
        if len(self.entries) > self.maxlen:
            del self.entries[: len(self.entries) - self.maxlen]
        return entry

    # -- querying -----------------------------------------------------------
    def list(self, since_seq=0, limit=500):
        since_seq = int(since_seq or 0)
        limit = max(1, min(int(limit or 500), self.maxlen))
        out = [e for e in self.entries if e["seq"] > since_seq]
        return out[-limit:]

    def clear(self):
        count = len(self.entries)
        self.entries.clear()
        return {"cleared": count}

    # -- export -------------------------------------------------------------
    def export(self, path, fmt="jsonl", only_ok=False):
        entries = [e for e in self.entries if not only_ok or e.get("status") == "ok"]
        if fmt == "python":
            body = self._python_script(entries)
        else:
            body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return {"path": os.path.abspath(path), "entries": len(entries), "format": fmt}

    def _python_script(self, entries):
        lines = [
            '#!/usr/bin/env python3',
            '"""Piterm QGIS AI - recorded session replay.',
            '',
            'Run with any Python 3.9+ (stdlib only). The bridge must be listening',
            '(QGIS open, Piterm plugin server started).',
            '"""',
            'import json, socket, sys',
            '',
            'HOST = "127.0.0.1"',
            'PORT = 9877',
            'TOKEN = ""  # set when authentication is enabled',
            '',
            '',
            'def call(cmd, params):',
            '    payload = json.dumps({"type": cmd, "params": params, "token": TOKEN}).encode()',
            '    with socket.create_connection((HOST, PORT), timeout=60) as sock:',
            '        sock.sendall(len(payload).to_bytes(4, "big") + payload)',
            '        header = sock.recv(4)',
            '        if len(header) < 4:',
            '            raise SystemExit("no response")',
            '        size = int.from_bytes(header, "big")',
            '        data = b""',
            '        while len(data) < size:',
            '            data += sock.recv(min(65536, size - len(data)))',
            '        return json.loads(data)',
            '',
            '',
            'STEPS = [',
        ]
        for entry in entries:
            if entry["cmd"] in _NEVER_REPLAY or entry.get("status") != "ok":
                continue
            step = json.dumps([entry["cmd"], entry["params"]], ensure_ascii=False)
            lines.append('    # seq %s  %s' % (entry["seq"], time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(entry["ts"]))))
            lines.append('    %s,' % step)
        lines += [
            ']',
            '',
            '',
            'def main():',
            '    failed = 0',
            '    for index, (cmd, params) in enumerate(STEPS, 1):',
            '        if cmd in ("python.exec", "python.exec_full"):',
            '            print("[%d] skip %s (not auto-replayed)" % (index, cmd))',
            '            continue',
            '        response = call(cmd, params)',
            '        ok = response.get("ok", False)',
            '        print("[%d] %s -> %s" % (index, cmd, "ok" if ok else response.get("error")))',
            '        if not ok:',
            '            failed += 1',
            '    sys.exit(1 if failed else 0)',
            '',
            '',
            'if __name__ == "__main__":',
            '    main()',
        ]
        return "\n".join(lines) + "\n"
