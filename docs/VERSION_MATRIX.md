# Dependency version matrix

Tracks the external tools this pack depends on, and the state on this machine after
the 2026-09-19 version sweep. Verified by running the tools, not by reading versions.

| component | installed | latest checked | state |
|---|---|---|---|
| semble (pip) | 0.6.0 | 0.6.0 | upgraded from 0.5.2 |
| codegraphcontext (pip) | 0.6.13 | 0.6.13 | upgraded from 0.5.3 |
| graphifyy (pip) | 0.9.64 | 0.9.64 | upgraded from 0.9.26 |
| @tobilu/qmd (npm) | 2.8.3 | 2.8.3 | already current |
| omniroute (npm) | 3.8.50 | 3.8.50 | already current |

## A stale CLI copy exists (not fixed — see below)

`~/.local/bin/graphify` is generated for **system python3 (/usr/bin/python3)**, and
an older `graphifyy-0.9.15` sits in `~/.local/lib/python3.12/site-packages/`, while
the Hermes venv (python3.11) has the current 0.9.64. The `graphify` on PATH can
therefore report a version different from the one the plugin loads in-process.

The plugin imports the library directly (which is what matters, and it is current).
The stale user-site copy is left in place deliberately rather than removed: the
`graphify` CLI is invoked as a subprocess by the plugin's auto-build path, and
re-pointing it is a change with its own failure modes. Noted here so the mismatch is
not mistaken for a broken upgrade later.

## Known dependency conflict (declared, not functional)

`codegraphcontext` pins `protobuf<3.21,>=3.20`, while `onnxruntime` 1.28.0 declares
`protobuf>=4.25.8`. Installing the cgc upgrade downgraded protobuf to 3.20.3, so
pip reports conflicts for onnxruntime, google-api-core and proto-plus.

Verified at runtime: all four import successfully (onnxruntime 1.28.0,
google.protobuf 3.20.3, google.api_core 2.30.3, proto.plus 1.27.2), so this is a
metadata conflict rather than a functional break on this machine. It is recorded
because a future `pip install -U protobuf` (or a dependency that needs protobuf 4+
APIs, gRPC in particular) would resolve it in the other direction and could break
codegraphcontext. Do not "fix" one side without checking the other.

## Post-upgrade verification

- LSP suite: 10/10 (includes the known-bad-file check)
- semble 0.6.0: `from_path` only ADDED `show_progress_bar`, so the plugin's call site
  is unaffected; indexing through the plugin's pre-flight guard still works (4.5s)
- cgc 0.6.13: analysis handler imports intact; the false-success this pack patched
  upstream no longer occurs, and the patch remains as a guard for error payloads
