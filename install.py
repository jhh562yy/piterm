#!/usr/bin/env python3
"""Cross-platform installer for Piterm QGIS AI (plugin deploy + enable + verify).

Windows users should prefer ``scripts/install.ps1`` (richer: backups & every
profile). This script exists for Linux/macOS and CI:

    python install.py                 # deploy into every detected profile
    python install.py --profile default --all-profiles
    python install.py --uninstall
    python install.py --list
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN_SOURCE = os.path.join(ROOT, "plugin", "piterm_qgis_ai")
MANIFEST_SOURCE = os.path.join(ROOT, "mcp", "piterm_qgis_mcp", "manifest.json")
PLUGIN_ID = "piterm_qgis_ai"


def profile_roots():
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", "")
    candidates = [
        os.path.join(appdata, "QGIS", "QGIS3", "profiles"),
        os.path.join(appdata, "QGIS", "QGIS4", "profiles"),
        os.path.join(home, ".local", "share", "QGIS", "QGIS3", "profiles"),
        os.path.join(home, ".local", "share", "QGIS", "QGIS4", "profiles"),
        os.path.join(home, "Library", "Application Support", "QGIS", "QGIS3",
                     "profiles"),
        os.path.join(home, "Library", "Application Support", "QGIS", "QGIS4",
                     "profiles"),
    ]
    return [path for path in candidates if os.path.isdir(path)]


def find_profiles(names, all_profiles):
    found = []
    for root in profile_roots():
        app = os.path.basename(os.path.dirname(root))
        for name in sorted(os.listdir(root)):
            profile_dir = os.path.join(root, name)
            if not os.path.isdir(profile_dir):
                continue
            if names and name not in names:
                continue
            if not names and not all_profiles and name != "default":
                continue
            found.append({
                "app": app,
                "name": name,
                "path": profile_dir,
                "plugins": os.path.join(profile_dir, "python", "plugins"),
                "ini": os.path.join(profile_dir, "QGIS", "%s.ini" % app),
            })
    return found


def backup(path):
    if os.path.exists(path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = "%s.bak-piterm-%s" % (path, stamp)
        if os.path.isdir(path):
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)
        return target
    return None


def install_profile(profile, no_enable=False):
    print("== %s / %s ==" % (profile["app"], profile["name"]))
    target = os.path.join(profile["plugins"], PLUGIN_ID)
    os.makedirs(profile["plugins"], exist_ok=True)
    if os.path.exists(target):
        moved = backup(target)
        shutil.rmtree(target)
        print("   backup: %s" % moved)
    shutil.copytree(PLUGIN_SOURCE, target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if os.path.exists(MANIFEST_SOURCE):
        shutil.copy2(MANIFEST_SOURCE, os.path.join(target, "manifest.json"))
    if not no_enable:
        _enable_in_ini(profile["ini"])
    files = sum(len(f) for _r, _d, f in os.walk(target))
    print("   deployed: %s (%d files)" % (target, files))


def uninstall_profile(profile, no_enable=False):
    print("== uninstall %s / %s ==" % (profile["app"], profile["name"]))
    target = os.path.join(profile["plugins"], PLUGIN_ID)
    if os.path.exists(target):
        shutil.rmtree(target)
        print("   removed: %s" % target)
    if not no_enable and os.path.exists(profile["ini"]):
        _enable_in_ini(profile["ini"], disable=True)


def _enable_in_ini(ini_path, disable=False):
    if not os.path.exists(ini_path):
        print("   (ini missing: %s - enable manually in the plugin manager)"
              % ini_path)
        return
    backup(ini_path)
    with open(ini_path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    value = "false" if disable else "true"
    line = "%s=%s" % (PLUGIN_ID, value)
    if re.search(r"(?m)^%s\s*=" % re.escape(PLUGIN_ID), content):
        content = re.sub(r"(?m)^%s\s*=.*$" % re.escape(PLUGIN_ID), line, content)
    elif "[PythonPlugins]" in content:
        content = re.sub(r"(?m)^(\[PythonPlugins\]\s*)$",
                         r"\1\n%s" % line, content, count=1)
    else:
        content = content.rstrip() + "\n\n[PythonPlugins]\n%s\n" % line
    with open(ini_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    print("   ini updated: %s" % ini_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Piterm QGIS AI installer")
    parser.add_argument("--profile", action="append", default=[],
                        help="profile name (repeatable); default: 'default'")
    parser.add_argument("--all-profiles", action="store_true",
                        help="deploy into every detected profile")
    parser.add_argument("--no-enable", action="store_true",
                        help="do not touch the profile ini")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    profiles = find_profiles(args.profile, args.all_profiles)
    if args.list:
        for profile in profiles:
            print("%s/%s -> %s" % (profile["app"], profile["name"],
                                   profile["path"]))
        return 0
    if not profiles:
        print("No QGIS profiles found. Start QGIS once (profiles are created on "
              "first run), then retry.", file=sys.stderr)
        return 2
    for profile in profiles:
        if args.uninstall:
            uninstall_profile(profile, args.no_enable)
        else:
            install_profile(profile, args.no_enable)
    if not args.uninstall:
        print("\nDone. Start QGIS; the Piterm QGIS AI toolbar button appears.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
