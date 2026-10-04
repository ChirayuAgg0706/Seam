# Roadmap: from v1 to production-ready

v1 (see [STATUS.md](STATUS.md)) proves the design on the scenarios it was specified for.
This list is what stands between that and a tool people can rely on, in priority order.
An item is **done** only when a scenario test for it has been seen passing.

## Priority 1: fails or is unverified on first real use

| # | Item | Status |
|---|---|---|
| 1 | **Crashes and signals.** Segfault/abort in an extension stops with the merged stack; signals the program handles do not stop the debugger; death by signal is reported as such. | **done** (`tests/test_crash.py`, 12 scenarios; `docs/decisions.md` §13) |
| 2 | **Adapter failure paths.** Editor vanishing, `seam dap` killed, LLDB dying, malformed requests, launch errors: a clear message and nothing left behind. | **done** (`tests/test_robust.py`, 7 scenarios) |
| 3 | **Exception breakpoints.** Stop on uncaught and on raised Python exceptions, with exception details; C++ `throw` and Rust panic. | not started |
| 4 | **Program input.** `input()` does not work today. Run the program in the editor's terminal (`console: integratedTerminal`). | not started |
| 5 | **Editor verification.** Automated checks of the VS Code extension (extension host under a virtual display) and of the Neovim configuration (headless), in CI. | not started |

## Priority 2: coverage

| # | Item | Status |
|---|---|---|
| 6 | LLDB 19 and 20 in CI (today CI runs 18 only; 20 is run locally). | not started |
| 7 | Real third-party wheels with no debug info (numpy, a PyO3 package): merged stack and stepping. | not started |
| 8 | Clear refusal on unsupported setups (free-threaded build, Python 3.11, no LLDB, ptrace blocked) and a `seam doctor` command that checks the environment. | not started |
| 9 | Weekly looped soak run in CI to catch nondeterminism without anyone asking for it. | not started |

## Priority 3: features people expect from a debugger

| # | Item | Status |
|---|---|---|
| 10 | Hit-count conditions and logpoints. | not started |
| 11 | Set variable; richer variable display (dict keys, object attributes, long collections in pages). | not started |

## Priority 4: release engineering

| # | Item | Status |
|---|---|---|
| 12 | Wheel and `.vsix` built as CI artifacts on a version tag; changelog; one version number. Nothing is published without the owner's say-so. | not started |
| 13 | Lint in CI; split the 1,800-line adapter module; contributor and security notes. | not started |

## Needs the project owner (not yet)

- Publishing: the PyPI name, the VS Code Marketplace / Open VSX publisher, and making the
  repository public.

## Not planned

macOS, Windows, non-x86-64, PyPy, sub-interpreters, remote debugging. Each is a project of
its own; see the README's Limitations.
