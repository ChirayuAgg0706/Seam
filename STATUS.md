# Seam status

Legend: **done** = run and seen passing; **partial** = part of it runs and passes, the rest
is listed; **blocked** = needs something from the project owner.

Every "done" below refers to scenario tests that launch real programs under Seam through a
scripted DAP client (`tests/`), or through a real editor where it says so. Nothing is
marked done on the strength of reading code. Work that remains is in
[ROADMAP.md](ROADMAP.md).

## The v1 checklist

| # | Item | Status | Evidence / what is missing |
|---|---|---|---|
| 1 | Python 3.12, 3.13, 3.14, including builds with no debug info | **done** | With debug info: Ubuntu's 3.12 and the deadsnakes 3.13/3.14 with their `-dbg` packages. Without: uv's standalone builds (symbol table, no DWARF), and system 3.12 with symbol lookup disabled. Python 3.15.0rc3 passes the full suite too. |
| 2 | Breakpoints in Python files (incl. modules imported later) and native files, in one session, add/remove while running | **done** | `test_python.py`, `test_mixed.py`. Conditions, hit counts and log messages: `test_breakpoints.py`. |
| 3 | Merged call stack, Seam's frames hidden, nested Python → native → Python → native, multi-threaded | **done** | `test_mixed.py`. Python frames are compared with what Python itself reports inside the target. Also against real wheels without debug info (`test_wheels.py`: numpy, orjson). |
| 4 | Variables: Python locals in Python frames, native locals in native frames | **done** | Same tests. At native stops Python locals are decoded from memory (simple built-in types show values, others type and address). |
| 5 | Evaluate Python at Python stops; clear refusal at native stops, process stays healthy | **done** | `test_python.py`, `test_mixed.py`. |
| 6 | Stepping: over/in/out in Python; Python → native; native → calling Python line; native → Python callback; callback → native caller | **done** | `test_python.py::test_stepping`, `test_stepping.py`. |
| 7 | Binding layers: step in from Python lands in user code for C API, PyO3, pybind11, nanobind, Cython | **done at -O0; partial at -O2** | `test_bindings.py`. See the matrix below for the two -O2 cells. |
| 8 | Exceptions raised in native code don't break stepping | **done** | `test_stepping.py`. |
| 9 | Launch and attach; PEP 768 on 3.14+; caveated injection on 3.12/3.13 | **done** | `test_attach.py`; the test asserts which mechanism was used. |
| 10 | DAP server tested with a scripted DAP client | **done** | Every scenario goes through `tests/dapclient.py` talking to `seam dap` over stdio. |
| 11 | VS Code extension as a `.vsix`; nvim-dap configuration | **done** | CI's `editors` job installs the packaged `.vsix` into a real VS Code (extension host under a virtual display) and debugs the PyO3 demo through VS Code's own debug API and stepping commands, checking which file and line VS Code shows at each stop, and that the program runs in the integrated terminal. It then loads the configuration from `docs/neovim.md` verbatim into a headless Neovim with nvim-dap and does the same. What this does not cover is what the UI looks like to a person; see "Not verified". |
| 12 | Overhead within 10% with no breakpoints | **done** for CPU-bound and native-call workloads | `test_overhead.py`. Measured ratios vary from run to run between 0.99 and 1.05 on this machine. A thread-creation-heavy workload is 2 to 3 times slower under Seam (LLDB handles every thread start and exit); it is measured and printed, not held to 10%. |
| 13 | Docs: README (install, quick start, architecture, limitations) | **done** | `README.md`, `docs/decisions.md`, `docs/neovim.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`. |
| 14 | Clean-machine check with a PyO3 project | **done**, with one caveat | The `clean-machine` CI job starts from a bare `ubuntu:24.04` container, runs the README's install commands, `seam doctor`, and then debugs `examples/pyo3-demo` with the installed `seam`. Caveat: it installs from the checked-out repository instead of running the README's `git clone` line, because the repository is private. |

## Since v1

| Area | Status | Evidence |
|---|---|---|
| Crashes in native code (segfault, abort, on any thread) stop with the merged stack; nothing is run in a crashed process | **done** | `test_crash.py` |
| Signals the program handles do not stop the debugger; `stopOnSignals` | **done** | `test_crash.py` |
| Death by signal reported as such (exit code 128 + signal) | **done** | `test_crash.py` |
| Adapter failure paths: client vanishing, `seam dap` killed, LLDB dying, bad requests, launch errors | **done** | `test_robust.py`. Nothing is left behind in each case. |
| Exception breakpoints: uncaught (main thread, threads, replaced `sys.excepthook`), raised, C++ throw, Rust panic | **done** | `test_exceptions.py` |
| Program input: the editor's terminal, Ctrl-C, terminal closed; empty input in the debug console | **done** | `test_terminal.py`, and for real in VS Code and Neovim by the `editors` job |
| Hit counts and logpoints, Python and native | **done** | `test_breakpoints.py` |
| `seam doctor`; clear messages when LLDB is missing or cannot load the adapter | **done** | `test_doctor.py`; also run on the clean machine |
| Unsupported interpreters refused by name (3.11, free-threaded) | **done** | `test_unsupported.py`. The refusal of non-x86-64 programs is written but **not tested** (no such machine here). |
| Real third-party wheels in a virtual environment | **done** | `test_wheels.py`: numpy and orjson from PyPI |
| LLDB 19 and 20 | **done** | CI `lldb` job (smoke scenarios); LLDB 20 is also what local runs use |
| Release artifacts (manylinux wheel, sdist, `.vsix`) | **partial** | `.github/workflows/release.yml` builds them and checks the wheel with `seam doctor`. Nothing is published. See "Not verified". |

## CI

GitHub Actions, on every push and once a week (`.github/workflows/ci.yml`):

- `full suite (3.12, -O0)`: every test on the runner's system Python (no debug info there).
- `smoke` × 5: 3.12 -O2, 3.13 -O0, 3.14 -O0 and -O2, 3.15 -O0, on uv's standalone
  interpreters, with LLDB 18.
- `lldb` × 2: the smoke scenarios under LLDB 19 and LLDB 20.
- `editors`: VS Code and Neovim, see item 11.
- `clean machine`: see item 14.
- `VS Code extension package`: builds the `.vsix` and uploads it as an artifact.
- Weekly only: the stepping, binding-layer and attach scenarios looped 8 times.

A missing toolchain fails CI rather than skipping (`SEAM_TEST_STRICT=1`). One push costs
roughly 25 minutes of runner time across the jobs.

## Test matrix

The two skips at -O2 are the same on every interpreter and are not Seam failures; the test
asserts what Seam reports before skipping:

- **nanobind -O2, PyO3 -O2: breakpoint on the line that calls the Python callback.** The
  optimiser leaves that statement with no code of its own. LLDB moves the breakpoint to
  the next line that has code (for PyO3, into the next function) or leaves it unresolved;
  Seam reports where it ended up. So "step in from native to a Python callback" is
  **untested for nanobind and PyO3 at -O2**. It passes for the C API, pybind11 and Cython.
- **nanobind -O2 under LLDB 20**: stepping into the callback works, but LLDB 20 cannot
  unwind through nanobind's optimised library code, so the native frames below the
  callback are missing and the callback cannot be stepped back out to its native caller.
  Seam says so in the debug console and still shows every Python frame.

"Several presses": in Cython-generated code, and in optimised code generally, a single
"step in" on the line that calls a Python callback can stop elsewhere on the same line
first; pressing it again gets there (the test allows up to 12, typically 2 to 5).

## Known problems

- **Leftover stops (handled; one related failure not proven fixed).** On a busy machine
  LLDB sometimes reports the internal step it takes to leave a breakpoint as a new stop
  at that breakpoint. It showed up as a hit count being off by one in 4 of 9 CI jobs.
  Seam now recognises such stops and ignores them: 52 occurred in loaded local loops
  after the check went in and none caused a failure. While fixing it, one loaded run lost
  a genuine breakpoint hit instead; the likely cause was removed and it has not recurred
  in 22 further loaded runs, which is not enough to call it proven
  (`docs/decisions.md` §17).
- **Two older one-off failures remain unexplained.** A Cython callback step-in that once
  reported an "exception" stop, and a nanobind callback scenario that failed once under
  LLDB 18 with its output lost. Neither has recurred. The leftover stops above are a
  plausible cause for both, but that is a guess.
- **Thread-heavy programs run 2 to 3 times slower** under Seam even with no breakpoints.
- Limits by design are listed in the README's Limitations section.

## Not verified

- **How the editors look to a person.** The `editors` job checks what VS Code and Neovim
  do (which file and line they show, that stepping commands work, that the terminal is
  used). It cannot check that the Variables pane, the call-stack view or the exception
  pop-up read well. To look yourself: install `vscode/seam-debugger-0.1.0.vsix` (built by
  `scripts/build-vsix.sh`, or downloaded from a CI run's artifacts) in VS Code connected
  to WSL, open `examples/pyo3-demo`, put a breakpoint on the `result = ...` line of
  `demo.py` and press F5.
- **Non-x86-64 machines**: the refusal message is untested.
- **The release workflow** has been run by hand, not from a version tag.

## Blocked on the project owner

Nothing. Publishing (PyPI, the VS Code Marketplace, Open VSX, making the repository
public) is the owner's decision and has not been done.
