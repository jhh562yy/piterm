"""Repository checks that need no QGIS and no bridge (used by CI).

Verifies the generated command catalog and that the documented files exist.
Run: ``python scripts/ci_checks.py``
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RISKS = {"read", "write", "destructive", "execute"}
REQUIRED_DOCS = ["README.md", "LICENSE", "pyproject.toml", "docs/USAGE.md",
                 "docs/TOOLS.md", "docs/INSTALL.md", "docs/SECURITY.md",
                 "docs/PROTOCOL.md", "docs/CHANGELOG.md",
                 "skill/piterm-qgis-ai/SKILL.md"]
MANIFEST_PATHS = ["mcp/piterm_qgis_mcp/manifest.json", "verification/manifest.json"]
MIN_COMMANDS = 120


def fail(message):
    print("FAIL: %s" % message)
    return 1


def check_catalog():
    problems = 0
    for rel in MANIFEST_PATHS:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            problems += fail("manifest missing: %s" % rel)
            continue
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        commands = manifest.get("commands") or []
        names = [c.get("name") for c in commands]
        if manifest.get("count") != len(commands):
            problems += fail("%s: count=%s but %d entries"
                             % (rel, manifest.get("count"), len(commands)))
        if len(commands) < MIN_COMMANDS:
            problems += fail("%s: only %d commands (expected >= %d)"
                             % (rel, len(commands), MIN_COMMANDS))
        if len(set(names)) != len(names):
            problems += fail("%s: duplicate command names" % rel)
        bad_name = [n for n in names if not n or "." not in n or n != n.lower()]
        if bad_name:
            problems += fail("%s: bad command names %s" % (rel, bad_name[:5]))
        bad_risk = [c.get("name") for c in commands if c.get("risk") not in RISKS]
        if bad_risk:
            problems += fail("%s: unknown risk level on %s" % (rel, bad_risk[:5]))
        print("catalog ok: %-42s %4d commands" % (rel, len(commands)))
    return problems


def check_docs():
    missing = [rel for rel in REQUIRED_DOCS
               if not os.path.isfile(os.path.join(ROOT, rel))]
    if missing:
        return fail("missing files: %s" % ", ".join(missing))
    print("docs ok: %d files present" % len(REQUIRED_DOCS))
    return 0


def main():
    problems = check_catalog() + check_docs()
    if problems:
        print("\n%d problem(s)" % problems)
        return 1
    print("\nCI CHECKS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
