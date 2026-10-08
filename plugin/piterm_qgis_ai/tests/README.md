# Plugin-side tests

The authoritative tests live in the repo-level `tests/` folder and
`scripts/qgis_headless_smoke.py`. This folder is kept so a future
`pip install -e`-style packaging of the plugin can ship its own smoke entry.

Run the end-to-end suite (no GUI needed) against a real QGIS install:

```powershell
"C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat" ..\..\scripts\qgis_headless_smoke.py
"C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat"       ..\..\scripts\qgis_headless_smoke.py
```

Reports land in `verification/logs/smoke_*.json` at the repo root.
