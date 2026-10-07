# Seam status

Legend: **done** = run and seen passing; **partial** = part of it runs and passes, the rest
is listed; **blocked** = needs something from the project owner.

Every "done" below refers to scenario tests that launch real programs under Seam through a
scripted DAP client (`tests/`), or through a real editor where it says so. Nothing is
marked done on the strength of reading code. The first-release sign-off is in
[docs/v1-readiness.md](docs/v1-readiness.md); the longer backlog is in [ROADMAP.md](ROADMAP.md).

The suite has 306 scenarios; 13 are opt-in (timings with a 15,000-function module,
and sessions against four real projects built from source), and one specifically tests
optimised-away locals at -O2. The CI section records validation under each LLDB version.

The developer-trial fixes were checked locally on 2026-10-05 under LLDB 20: 278 pass
on system Python 3.12 and uv's 3.14, with the 13 opt-in skips and the -O2-only local
inspection check skipped. The -O2 smoke selection passes on both: 229 pass and three
expected skips (two callback lines with no code and one local with no address to change).
CPU/native throughput ratios are 0.942/0.988 on 3.12 and 1.006/1.000 on 3.14; thread-heavy
ratios are 1.98 and 2.23. The throughput timer excludes initial library loading (§32).

The 2026-10-05–06 v1 review fixed plain-await duplicate stops and helpers compiled with
newer Python headers failing to dispatch on 3.12 (§33). The full local review run was
**not green**: 273 passed, 14 skipped, seven failed. Four failures were stale Rust DWARF
paths in the copied checkout; two pause scenarios assumed imports finished within a
fixed delay. Those six pass after test-harness fixes. The seventh was Ubuntu's apport
exception hook loading native libraries before process exit: a diagnostic pause caught
that stack, and restoring CPython's hook reduced measured exit from 25.82 to 0.01 seconds.
The attach fixture now uses the standard hook, as the launch fixture already did (§32).
Final extended CI passed on `d5e3185`: 279 passed / 16 documented skips in the full
suite, all version/optimisation and LLDB cells green, both editors and clean-machine
installation green. Release artifacts and the main-branch push check also passed.
The owner's Windows/WSL checks passed through attach/detach. The subsequent helper
injection failure was reproduced with a delayed symbol server and corrected by disabling
network downloads (§34). The dimmed native stop and VS Code's initial frame selection
are corrected too. Final extended CI passed on `610f460` (run 37477454757): 280 passed,
16 documented skips; every compatibility, editor and clean-machine job passed. Release
run 37477455386 passed on the same commit. That extension is installed in WSL and its
doctor passes. On 2026-10-06 the owner confirmed the startup/disassembly retest works,
with disassembly open in the supplied screenshot. V1 development and acceptance are
complete for the documented Linux x86-64 scope; shipping work remains.

On 2026-10-07, publishing was held for a focused release follow-up: automatic selection
now prefers installed LLDB 20/19 over plain `lldb`, preserving explicit overrides.
The original pre-fix timeout's exact cause remains unproven. The installed `610f460`
extension passed 30 further terminal/disassembly launches with no timeout; validation
of the updated package is pending in `docs/v1-readiness.md`.

## The v1 checklist

| # | Item | Status | Evidence / what is missing |
|---|---|---|---|
| 1 | Python 3.12, 3.13, 3.14, including builds with no debug info | **done** | With debug info: Ubuntu's 3.12 and the deadsnakes 3.13/3.14 with their `-dbg` packages. Without: uv's standalone builds (symbol table, no DWARF), and system 3.12 with symbol lookup disabled. Python 3.15.0rc3 passes too (smoke cell on CI). |
| 2 | Breakpoints in Python files (incl. modules imported later) and native files, in one session, add/remove while running | **done** | `test_python.py`, `test_mixed.py`. Conditions, hit counts and log messages: `test_breakpoints.py`. |
| 3 | Merged call stack, Seam's frames hidden, nested Python → native → Python → native, multi-threaded | **done** | `test_mixed.py`. Python frames are compared with what Python itself reports inside the target. Also against real wheels without debug info (`test_wheels.py`: numpy, orjson). |
| 4 | Variables: Python locals in Python frames, native locals in native frames | **done** | Same tests. At native stops Python locals are decoded from memory (simple built-in types show values, others type and address). |
| 5 | Evaluate Python at Python stops; clear refusal at native stops, process stays healthy | **done** | `test_python.py`, `test_mixed.py`. |
| 6 | Stepping: over/in/out in Python; Python → native; native → calling Python line; native → Python callback; callback → native caller | **done** | `test_python.py::test_stepping`, `test_stepping.py`. |
| 7 | Binding layers: step in from Python lands in user code for C API, PyO3, pybind11, nanobind, Cython | **done at -O0; partial at -O2** | `test_bindings.py`. See the matrix below for the two -O2 cells. |
| 8 | Exceptions raised in native code don't break stepping | **done** | `test_stepping.py`. |
| 9 | Launch and attach; PEP 768 on 3.14+; caveated injection on 3.12/3.13 | **done** | `test_attach.py`; the test asserts which mechanism was used. |
| 10 | DAP server tested with a scripted DAP client | **done** | Every scenario goes through `tests/dapclient.py` talking to `seam dap` over stdio. |
| 11 | VS Code extension as a `.vsix`; nvim-dap configuration | **done** | CI's `editors` job packages the extension with the adapter and its helper inside, checks that packaged adapter on its own (`vscode/test/unit.js --adapter`, `tests/editors/test_bundle.py` against the unpacked `.vsix`), then installs the `.vsix` into a real VS Code (extension host under a virtual display) with no Seam installed anywhere else. It opens a generated project with a virtual environment, presses F5 with no `launch.json`, and checks that the program being debugged is the environment's interpreter; steps into Rust and back; attaches through the process picker and detaches; runs "Seam: Check This Machine"; and reads what the user is told when LLDB is missing. A second run installs the Python extension and checks that the interpreter selected there is the one debugged. Pictures of the window at each stop are kept as the artifact `editor-check-screenshots`. It then loads the configuration from `docs/neovim.md` verbatim into a headless Neovim with nvim-dap and debugs the PyO3 demo with a pip-installed `seam`. |
| 12 | Overhead within 10% with no breakpoints | **done** for CPU-bound and native-call workloads | `test_overhead.py`. Measured ratios vary from run to run between 0.94 and 1.05 on this machine when it is otherwise idle (one run made while other test runs were going measured 1.19 and passed when repeated alone), and are the same with a 15,000-function module loaded (1.001). A thread-creation-heavy workload is about twice as slow under Seam (measured 1.9 to 2.1; LLDB handles every thread start and exit); it is measured and printed, not held to 10%. |
| 13 | Docs: README (install, quick start, architecture, limitations) | **done** | `README.md`, `docs/decisions.md`, `docs/neovim.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `vscode/README.md`. |
| 14 | Clean-machine check with a PyO3 project | **done**, with one caveat | The `clean-machine` CI job starts from a bare `ubuntu:24.04` container, runs the README's pip install commands, `seam doctor`, and then debugs `examples/pyo3-demo` with the installed `seam`. Caveat: it installs from the checked-out repository instead of running the README's `git clone` line, because the repository is private. |

## Since v1

| Area | Status | Evidence |
|---|---|---|
| Crashes in native code (segfault, abort, on any thread) stop with the merged stack; nothing is run in a crashed process | **done** | `test_crash.py` |
| Signals the program handles do not stop the debugger; `stopOnSignals` | **done** | `test_crash.py` |
| Death by signal reported as such (exit code 128 + signal) | **done** | `test_crash.py` |
| Adapter failure paths: client vanishing, `seam dap` killed, LLDB dying, bad requests, launch errors | **done** | `test_robust.py`. Nothing is left behind in each case. |
| Exception breakpoints: uncaught (main thread, threads, replaced `sys.excepthook`), raised, C++ throw, Rust panic | **done** | `test_exceptions.py` |
| Exception breakpoint "user-unhandled": stop where an exception leaves your code (a failing test) | **done** | `test_user_unhandled.py`, `test_pytest.py` |
| Program input: the editor's terminal, Ctrl-C, terminal closed; empty input in the debug console | **done** | `test_terminal.py`, and for real in VS Code and Neovim by the `editors` job |
| Hit counts and logpoints, Python and native | **done** | `test_breakpoints.py` |
| Function breakpoints (Python by bare, qualified or module-qualified name; native), with conditions and hit counts | **done** | `test_breakpoints.py` |
| Data breakpoints on native variables | **done** | `test_breakpoints.py` (hardware watchpoints; pass under WSL2 and on GitHub's runners) |
| Set variable (Python locals, globals, members; native), paged lists, expressions for nested values | **done** | `test_variables.py` |
| Completion in the debug console | **done** | `test_completions.py` |
| Developer-trial diagnostics and inspection fixes | **done** | `test_inspection.py`: clean launch globals, broken source/function conditions reported once (including hits filtered by a count), native string conditions, local watchpoint expiry through both DAP request forms, Python thread names cached for native stops, consistent type names, file-level constants without header statics, library frame hints, Python-expression guidance, unavailable locals at -O2, help text, derived and non-standard C++ throws. `test_attach.py` checks clean globals through pending-call and PEP 768 attach; `test_nosource.py` checks missing debug-info messages on load and attach; exception/source-map tests check `what()`, and `test_pytest.py` checks installed library frame hints. |
| Stepping in coroutines, tasks, async generators and plain generators | **done** | `test_async.py` (23 scenarios) on 3.12.3, 3.12.15, 3.13, 3.14 and once on 3.15.0rc3; looped 15 times on 3.12 and 3.14 |
| Stepping keeps to the user's code (`justMyCode`) | **done** | `test_justmycode.py` (17 scenarios): heapq, contextlib, unittest, pytest, numpy callbacks |
| Debugging a pytest run, including pytest-xdist | **done** | `test_pytest.py` |
| Child processes: fork, subprocess, multiprocessing (all start methods), children outliving the program, session end, expressions that start processes | **done** under LLDB 19 and 20; **partial** under LLDB 18 | `test_children.py` (19 scenarios). LLDB 18: see Known problems. |
| Source path mapping (`sourceMap`) for native code built elsewhere; projects opened through symbolic links | **done** | `test_sourcemap.py` (33): sources moved after the build, `-fdebug-prefix-map` / `-ffile-prefix-map` (invented and relative prefixes), Cython line directives, a PyO3 crate built with `--remap-path-prefix`; -O0 and -O2; launch and attach |
| Frames without source: library names, disassembly, stepping by instruction, crashes inside the interpreter | **done** | `test_nosource.py`: a stripped extension, orjson from PyPI, libc, the interpreter. The real VS Code test also checks initial native-frame focus, opens Disassembly View and steps an instruction. |
| Large modules: step-in does not slow down with the number of functions | **done** | `test_entrytraps.py` in the ordinary suite (pybind11, nanobind and PyO3 test modules use the mechanism by default, under LLDB 18, 19 and 20 on CI); opt-in `test_scale.py`: 0.025 s per step at 15,000 functions, was 1.3 s (LLDB 20) |
| Stepping where binding-layer code is inlined into user code | **done** | `test_inlined_glue.py` |
| Requests run in the program while other Python threads are busy | **done** | `test_entrytraps.py::test_other_threads_running_into_the_traps`, looped 60 times on 3.14 |
| Exceptions out of Cython libraries | **done** | `test_cython_exceptions.py` |
| Real projects built from source: regex (C API), msgpack (Cython), contourpy (pybind11), pydantic-core (PyO3) | **done**, opt-in, LLDB 20 only | `test_projects.py` (9 sessions), `tools/build_projects.sh`. Not in CI. numpy from source was not tried. |
| The newer features on an attached process | **done** | `test_attach_features.py`: exception filters (also changed while running), function breakpoint, logpoint, native hit count, set variable, a segfault; detach restores the hooks |
| A refused attach leaves nothing behind in the process | **done** | `test_attach_blocked.py` |
| `seam doctor`; clear messages when LLDB is missing or cannot load the adapter | **done** | `test_doctor.py`; also run on the clean machine, and out of the VS Code extension |
| Unsupported interpreters refused by name (3.11, free-threaded) | **done** | `test_unsupported.py`. The refusal of non-x86-64 programs is written but **not tested** (no such machine here). |
| Real third-party wheels in a virtual environment | **done** | `test_wheels.py`: numpy and orjson from PyPI |
| LLDB 19 and 20 | **done** | CI `lldb` job (smoke scenarios); LLDB 20 is also what local runs use |
| Release artifacts (manylinux wheel, sdist, `.vsix`) | **done on `610f460`** | Release run 37477455386 passes, including the bundled adapter and installed-wheel doctor. The final extension is installed in WSL and its doctor passes. Release version/tag selection remains shipping work; nothing is published. |

## CI

GitHub Actions (`.github/workflows/ci.yml`). The repository is on the free tier (2,000
runner minutes a month), so the jobs are split.

On every push to `main` (about 6 minutes of runner time):

- `full suite (3.12, -O0)`: every test on the runner's system Python (no debug info
  there), LLDB 18.
- `lint, versions, VS Code extension package`.

The extended set (about 30 more minutes), run once a week, when started by hand, and on
a push whose commit message contains `[ci full]`:

- `smoke` × 5: 3.12 -O2, 3.13 -O0, 3.14 -O0 and -O2, 3.15 -O0, on uv's standalone
  interpreters, with LLDB 18. The smoke selection is 236 of the scenarios.
- `lldb` × 2: the smoke scenarios under LLDB 19 and LLDB 20.
- `editors`: VS Code and Neovim, see item 11.
- `clean machine`: see item 14.
- Weekly only: the stepping, binding-layer and attach scenarios looped 8 times.

A run started by hand can be narrowed to one job (`-f only=editors`), and the soak job
can pick the LLDB version, repeat a selection and keep cores busy; see `CONTRIBUTING.md`.
A missing toolchain fails CI rather than skipping (`SEAM_TEST_STRICT=1`).

Recent CI runs:

- The developer-trial fixes passed the whole extended set on `736cc95` on 2026-10-05
  ([run 37338252923](https://github.com/ChirayuAgg0706/Seam/actions/runs/37338252923)):
  276 passed and 16 expected skips in the full LLDB 18 suite; all five LLDB 18 smoke
  cells on 3.12–3.15 and at -O2; the LLDB 19 and 20 smoke cells; VS Code and Neovim;
  the clean-machine install; lint, versions and extension packaging. Earlier runs
  exposed LLDB 18's RTTI symbol lookup and the terminal fixture's readiness race;
  both are corrected (decisions §32 and §15). The terminal scenario also passes
  20 local repeats each under LLDB 18 and 20.
- The whole extended set passed on the pre-trial code (commit `370d31f`, started by hand on
  2026-10-05): the full suite and the five smoke cells under LLDB 18, the smoke scenarios
  under LLDB 19 and 20, the editors, the clean machine, lint and the extension package.
- The run before it, the first on the merged code, had failed one new scenario on the
  LLDB 18 and 19 jobs at -O0. The scenario was wrong, not Seam: it set a breakpoint on a
  line whose code is all an inlined call, which LLDB before 20 moves to the next line.
- The race fix (decisions §18) was looped under load: the breakpoint scenarios 12 times
  over on a 2-core runner with both cores busy, under LLDB 18.1.3 and 19.1.1: 288
  scenario runs and 2,448 continues each, no failures, no leftover stops, no stale frame
  lists.

Earlier on 2026-10-05, before the developer-trial round, this repository had used about
470 of the month's 2,000 minutes.

## Test matrix

The skips at -O2 are the same on every interpreter and are not Seam failures; the test
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
- **`test_variables.py`, -O2**: optimised code keeps no addressable copy of the local the
  test changes.

"Several presses": in Cython-generated code, and in optimised code generally, a single
"step in" on the line that calls a Python callback can stop elsewhere on the same line
first; pressing it again gets there (the test allows up to 12, typically 2 to 5). Pieces
of binding-layer glue inlined into the line no longer count (decisions §30); the Cython
case was not re-measured.

## Known problems

- **LLDB 18 and child processes in programs with several threads.** If a thread reaches a
  breakpoint (also a conditional one whose condition is false) at the moment another
  thread starts a child with `subprocess` or `os.system`, or if several threads start
  children at once, LLDB 18 can no longer evaluate anything or loses the program. Three
  ways around it were tried from Seam's side and none worked (decisions §21). Seam
  reports a lost program as such, `seam doctor` notes the weakness, and the README says
  how to use LLDB 19, which is not affected. Under LLDB 18 one scenario of
  `test_children.py` skips after asserting that message and one runs without the
  breakpoint that provokes the problem. LLDB 18 is what Ubuntu 24.04 installs by default.
- **Coroutine stacks on Ubuntu's own Python show `_asyncio`'s C frames** (`task_step`)
  between the coroutine and the event loop: modules under `lib-dynload` are not classed
  as the interpreter's. uv's builds link `_asyncio` in and hide them.
- **With both "uncaught" and "user-unhandled" on, an exception leaving a thread's target
  stops twice**: leaving into `threading`, and at `threading.excepthook`.
- **A function breakpoint by bare name can stop in binding glue first.** Seen with
  contourpy: `lines` stops twice inside a pybind11 template during import before the
  real function. LLDB resolves the name there; why was not found.
- **The first Step Into of a session in pydantic-core takes about 4 seconds** (123,456
  locations looked up once; the debug console says so). Later steps take 0.02 s.
- **A crash or timeout inside an expression typed by the user leaves the interpreter
  damaged**: a second evaluation crashes too. Found while changing how such calls are
  made; the old and the new call path behave the same, so it predates this round.
- **A Python object reads differently at native and Python stops** (`<Order object at
  0x…>` against its repr), so VS Code marks it as changed each time the session moves
  between the two. Reading an object's attributes from memory at native stops would fix
  it.
- **Names in generated code.** Cython functions and variables appear under their
  generated C names unless the module was built with line directives; Rust generic names
  are correct but very long.
- **Thread-heavy programs run about twice as slowly** under Seam even with no breakpoints.
- **Two older one-off failures were never reproduced.** A Cython callback step-in that
  once reported an "exception" stop, and a nanobind callback scenario that failed once
  under LLDB 18 with its output lost. The race removed in decisions §18 is a plausible
  cause for both, but that is a guess.
- Limits by design are listed in the README's Limitations section.

## Not verified

- **VSCodium.** VSCodium was not run; the first-run check runs
  without the Python extension, which is VSCodium's situation as far as Seam is
  concerned.
- **LLDB 18 and 19 on the real projects and the scale timings.** Both ran under LLDB 20
  only. What the ordinary suite covers of the same mechanisms runs under 18 and 19 on CI.
- **Attach, then Step Into a large module**: no scenario.
- **Non-x86-64 machines**: the refusal message is untested.
- **The release tag/version.** Artifacts from final code `610f460` pass the release
  workflow (37477455386). Choosing a public version and validating its tag remain
  shipping work; nothing is published.
- **Python 3.15.0 final**: only the release candidate (rc3) exists so far.
- **The weekly schedule** did not start a run on its first Monday (2026-10-05, due at
  03:17 UTC, nothing by 06:20 UTC). GitHub delays and sometimes drops scheduled runs;
  whether this one was dropped or the schedule is not taking effect is not known. The
  jobs it would have run were run by hand instead: the extended set above, and the looped
  soak with the weekly settings earlier (165 scenario runs, all passed, LLDB 18).

## Needs the project owner

- Publishing (PyPI, the VS Code Marketplace, Open VSX, making the repository public), and
  with it a version number and a tagged release. Nothing has been published.
