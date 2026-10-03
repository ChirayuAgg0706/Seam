# Seam v1 status

Legend: **done** = run and seen passing in the test suite; **partial** = some of it runs
and passes, the rest is listed; **not started**.

Last updated: 2026-10-03, after milestone 1 (Python-only core on CPython 3.12).

| Item | Status | Notes |
|---|---|---|
| Python 3.12, 3.13, 3.14, incl. no debug info | partial | 3.12 without debug info passes. 3.13/3.14 layouts not written yet. |
| Breakpoints in Python files (incl. late imports) and native files, add/remove while running | partial | Python breakpoints (late import, conditional, line snapping, add/remove while stopped) pass. Native breakpoints and changes while running are implemented but untested. |
| Merged call stack, Seam frames hidden, nested Py→native→Py→native, threads | partial | Python-only stacks match `traceback.extract_stack()`. Nested and threaded cases untested. |
| Variables: Python locals, native locals | partial | Python locals/globals and children pass. Native locals implemented, untested. Python locals at native stops (raw memory) not started. |
| Evaluate at Python stops; refusal at native stops | partial | Evaluate at Python stops passes. Refusal implemented, untested. |
| Stepping within Python (over/in/out) | done | `tests/test_python.py`. |
| Stepping across the boundary (4 directions) | not started | |
| Binding layers (C API, PyO3, pybind11, nanobind, Cython) | not started | |
| Exceptions from native code don't break stepping | not started | |
| Launch and attach | partial | Launch passes. Attach not started. |
| DAP server tested with a scripted client | partial | All tests go through `tests/dapclient.py`. |
| VS Code extension (.vsix) and nvim-dap config | not started | |
| Overhead within 10% with no breakpoints | not started | |
| Docs: README, quick start, architecture, limitations | not started | |
| Clean-machine check | not started | |

## Blocked on the project owner

- Python 3.13/3.14 **with** debug symbols need the deadsnakes packages (sudo). Until then
  those versions are tested only as stripped uv builds.
