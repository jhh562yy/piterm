# Security policy

Piterm QGIS AI is a **local automation surface**: an agent that can reach the
bridge port can drive QGIS. The full threat model, control matrix and recommended
configurations live in [`docs/SECURITY.md`](docs/SECURITY.md); this page is the
short version.

## Reporting a vulnerability

Open a private security advisory on GitHub
(<https://github.com/jhh562yy/piterm/security/advisories/new>) or, if that is not
available to you, open an issue **without** exploit details and ask for a private
channel. Please include: affected version/commit, the command or transport
involved, and a minimal reproduction.

## What the project assumes

* The bridge binds **loopback only** by default. Binding a non-loopback address is
  refused unless a token is configured.
* Defaults favour a single-user workstation: no token, writes and `python.exec`
  allowed, path allow-list `~` · `:TEMP:` · `:PROJECT:`, audit log on,
  `python.exec_full` off.
* Hardening switches (all runtime-changeable): token auth, read-only mode,
  execute off, `__confirm` gate, path allow/deny lists, rate limits, client caps.

## Known limits (documented, not hidden)

* A **same-user** process can read your config/token and talk to the bridge. No
  local scheme prevents that; scopes, confirm, audit and checkpoints bound the
  damage.
* `python.exec_full` is arbitrary code by design — enabling it is equivalent to
  handing out a shell in the QGIS process (and therefore your user account).
* The HTTP endpoint sends permissive CORS headers for local tooling; do not
  combine that with a non-loopback bind unless you trust every page the browser
  loads.
* A malicious QGIS plugin is outside this model entirely.

## Supported versions

The current `main` (1.0.x) is the supported line; fixes land there.
