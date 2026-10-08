# Roadmap

Seam 0.1.0 is released on GitHub, PyPI and the VS Code Marketplace. The tables below
record completed work. The open list is a backlog, not a requirement to add features
before using v1. [STATUS.md](STATUS.md) records test evidence, and
[docs/decisions.md](docs/decisions.md) explains the design choices.

For the first public release, use [the v1 readiness checklist](docs/v1-readiness.md).
The open items below are a backlog, not a requirement to add more features before v1.

## Debugger foundations. Complete

| # | Item | Where |
|---|---|---|
| 1 | Crashes and signals: a segfault or abort stops with the merged stack; handled signals do not stop the debugger; death by signal is reported as such | `tests/test_crash.py`; decisions §13 |
| 2 | Adapter failure paths: editor vanishing, `seam dap` killed, LLDB dying, malformed requests, launch errors | `tests/test_robust.py` |
| 3 | Exception breakpoints: uncaught, raised, C++ `throw`, Rust panic | `tests/test_exceptions.py`; §14 |
| 4 | Program input: the editor's terminal with Ctrl-C; empty input in the debug console | `tests/test_terminal.py`; §15 |
| 5 | The packaged extension inside a real VS Code and the documented configuration inside Neovim, in CI | CI job `editors` |
| 6 | LLDB 19 and 20 in CI | CI job `lldb` |
| 7 | Real third-party wheels with no debug info | `tests/test_wheels.py` |
| 8 | Clear refusal on unsupported setups; `seam doctor` | `tests/test_unsupported.py`, `tests/test_doctor.py` |
| 9 | Weekly looped soak run in CI | the `soak` job |
| 10 | Python 3.15 (release candidate) | CI smoke cell |
| 11 | Hit-count conditions and logpoints | `tests/test_breakpoints.py`; §16 |
| 12 | Set variable, paged collections, `__slots__`, expressions for nested values | `tests/test_variables.py`; §19 |
| 13 | Function breakpoints and data breakpoints | `tests/test_breakpoints.py`; §19 |
| 14 | Wheel, sdist and `.vsix` built and checked by a workflow; changelog; versions in step | `release.yml` |
| 15 | Lint in CI; contributor and security notes | |
| 16 | The adapter split into a module per concern | `src/seam/adapter/` |

## Project and editor support. Complete

| # | Item | Where |
|---|---|---|
| 17 | **First run in VS Code.** The extension carries the adapter; it debugs with the project's interpreter; an attach picker; "Seam: Check This Machine" | CI job `editors`, `tests/editors/`; §20 |
| 18 | **Stepping in `async` code and generators** (step over an `await` used to end inside asyncio) | `tests/test_async.py`; §24 |
| 19 | **Steps keep to the user's code** (`justMyCode`) | `tests/test_justmycode.py`; §25 |
| 20 | **Debugging under pytest**, and stopping where a test fails ("user-unhandled" exceptions) | `tests/test_pytest.py`, `tests/test_user_unhandled.py`; §22, §23 |
| 21 | **Programs that start child processes** | `tests/test_children.py`; §21. Under LLDB 18 with one limit, see below |
| 22 | **Source path mapping** (`sourceMap`), and projects opened through symbolic links | `tests/test_sourcemap.py`; §26 |
| 23 | **Code without source**: library names, disassembly, stepping by instruction | `tests/test_nosource.py`; §27 |
| 24 | **Scale.** Step-in no longer slows down with the size of a module; sessions against regex, msgpack, contourpy and pydantic-core built from source, and the six defects they turned up | `tests/test_entrytraps.py`, `test_inlined_glue.py`, `test_cython_exceptions.py`, opt-in `test_scale.py` and `test_projects.py`; §28 to §31 |
| 25 | **What the editor looks like.** CI keeps pictures of the VS Code window at each stop; they were looked at | artifact `editor-check-screenshots` |
| 26 | **The race fix confirmed on LLDB 18 and 19 under load** | §18 |
| 27 | Completion in the debug console | `tests/test_completions.py` |
| 28 | The newer features on an attached process; a refused attach takes its request back | `tests/test_attach_features.py`, `tests/test_attach_blocked.py`; §8 |
| 29 | Developer-trial fixes: clean program globals, actionable condition and debug-info errors, local watchpoint lifetimes, C++ exception messages, thread names and clearer stack/variable inspection | `tests/test_inspection.py`, attach, no-source, exception and source-map scenarios; §32 |

## Open

Known limitations and follow-up work. These entries have documented workarounds or are
outside the frozen first-release scope. The review's attached-process exit timeout was
traced to Ubuntu's crash reporter and its test fixture corrected; see
[the readiness checklist](docs/v1-readiness.md) for final validation.

- **LLDB 18, threads and child processes.** LLDB 18 breaks when a thread is at a
  breakpoint while another starts a child process. LLDB 19 and 20 avoid the bug.
  Seam now prefers installed `lldb-20` and `lldb-19` over plain `lldb`.
  An explicit `SEAM_LLDB` overrides that choice.
- **Attaching to a program whose main thread is blocked** is refused after the timeout,
  because the helper cannot load. "Why is my program hung?" is a common reason to attach,
  and the merged call stack and native debugging need no helper. Staying attached without
  it, and loading it when the interpreter next runs Python, would cover that.
- Python objects read as `<Order object at 0x…>` at native stops; reading their
  attributes from memory would make native and Python stops agree.
- Extension-type objects (pybind11 instances, numpy arrays) have no expandable fields
  unless Python can safely expose them; a terminal front end is also still open.
- Logpoint and conditional-breakpoint overhead: the trial measured about 8 ms per Python
  logpoint hit and 1.8 ms per never-true native condition. Those costs have not been reduced.
- Python and native frames in the call stack differ only by their file. A marker where
  the stack crosses the boundary would make it plainer.
- `_asyncio`'s C frames show in coroutine stacks on Ubuntu's own Python.
- A function breakpoint by bare name can stop in pybind11 glue first.
- The first Step Into of a session in a very large module takes seconds (about 4 for
  pydantic-core).
- A crash or timeout inside an expression typed by the user damages the interpreter.
- No breakpoints in the disassembly view; `stepOut` ignores instruction granularity.
- Thread-heavy programs run about twice as slowly under Seam.
- nanobind at `-O2` under LLDB 20: LLDB cannot unwind through its library code.
- Validate Python 3.15 final. This release was tested with 3.15.0rc3.

## Release presentation

The owner completed the Windows/WSL acceptance checks. The repository is public,
and the original GitHub, PyPI and Marketplace installation routes are verified.
Debugger 0.1.1 and extension 0.1.3 add Apple Silicon support. The logo, demo GIF
and revised copy are included. Open VSX is deferred.

## Not planned

Native Windows, Intel Macs, Rosetta targets, Linux ARM, free-threaded Python, PyPy, sub-interpreters,
remote debugging and child-process debugging are outside v1's scope. Windows users
can run Seam in WSL. See the README's [limitations](README.md#limitations).
