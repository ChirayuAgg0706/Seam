# Roadmap: from v1 to production-ready

v1 proved the design on the scenarios it was specified for. This list is what stands
between that and a tool people can rely on, in priority order. An item is **done** only
when a scenario test for it has been seen passing; [STATUS.md](STATUS.md) has the evidence.

## Priority 1: fails or is unverified on first real use

| # | Item | Status |
|---|---|---|
| 1 | **Crashes and signals.** Segfault/abort in an extension stops with the merged stack; signals the program handles do not stop the debugger; death by signal is reported as such. | **done** (`tests/test_crash.py`; `docs/decisions.md` §13) |
| 2 | **Adapter failure paths.** Editor vanishing, `seam dap` killed, LLDB dying, malformed requests, launch errors: a clear message and nothing left behind. | **done** (`tests/test_robust.py`) |
| 3 | **Exception breakpoints.** Uncaught and raised Python exceptions, with exception details; C++ `throw` and Rust panic. | **done** (`tests/test_exceptions.py`; §14) |
| 4 | **Program input.** The program runs in the editor's terminal, with Ctrl-C passed on; in the debug console, input is empty instead of hanging. | **done** (`tests/test_terminal.py`; §15) |
| 5 | **Editor verification.** The packaged extension inside a real VS Code, and the documented configuration inside a headless Neovim, in CI. | **done** (CI job `editors`) |

## Priority 2: coverage

| # | Item | Status |
|---|---|---|
| 6 | LLDB 19 and 20 in CI. | **done** (CI job `lldb`) |
| 7 | Real third-party wheels with no debug info, in a virtual environment. | **done** (`tests/test_wheels.py`: numpy, orjson) |
| 8 | Clear refusal on unsupported setups; `seam doctor`. | **done** (`tests/test_unsupported.py`, `tests/test_doctor.py`). The non-x86-64 refusal is untested. |
| 9 | Weekly looped soak run in CI. | **set up**; its first scheduled run has not happened yet |
| 10 | Python 3.15. | **done** for 3.15.0rc3 (full suite); re-check when 3.15.0 is released |

## Priority 3: features people expect from a debugger

| # | Item | Status |
|---|---|---|
| 11 | Hit-count conditions and logpoints. | **done** (`tests/test_breakpoints.py`; §16) |
| 12 | Set variable; long collections in pages; `__slots__` objects; expressions for nested values. | **done** (`tests/test_variables.py`; §19) |
| 13 | Function breakpoints (Python and native) with conditions; data breakpoints on native variables. | **done** (`tests/test_breakpoints.py`; §19) |

## Priority 4: release engineering

| # | Item | Status |
|---|---|---|
| 14 | Wheel, sdist and `.vsix` built and checked by a workflow; changelog; versions kept in step. | **done** as a workflow (`release.yml`); nothing is published |
| 15 | Lint in CI; contributor and security notes. | **done** |
| 16 | Split the 2,500-line adapter module into parts. | **done**: `adapter/protocol.py`, `session.py`, `stops.py`, `stepping.py`, `breakpoints.py`, `stack.py`, `common.py`. Moved mechanically, method texts unchanged; the full suite passes before and after. |

## Things found on the way that are still open

- The race behind the stale, extra and lost stops (§18) was measured away under LLDB 20
  only. LLDB 18 and 19 run the same code on CI but have not been looped under load.
- Thread-heavy programs run 2 to 3 times slower under Seam.
- nanobind at `-O2` under LLDB 20: LLDB cannot unwind through its library code.

## Needs the project owner (not yet)

- Publishing: the PyPI name, the VS Code Marketplace / Open VSX publisher, and making the
  repository public. Until then the README's install line (`git clone`) only works for
  people with access.

## Not planned

macOS, Windows, non-x86-64, free-threaded builds, PyPy, sub-interpreters, remote
debugging. Each is a project of its own; see the README's Limitations.
