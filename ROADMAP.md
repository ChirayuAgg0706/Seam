# Roadmap

What has been done since v1, what is still open, and what is waiting for the project
owner. An item is **done** only when a scenario test for it has been seen passing;
[STATUS.md](STATUS.md) has the evidence, [docs/decisions.md](docs/decisions.md) the
reasons.

## From v1 to something people can rely on (done)

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

## From "passes its scenarios" to "works on a stranger's project" (done)

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

Found on the way and not done. None of them blocks ordinary use; the first is the one a
user is most likely to meet.

- **LLDB 18, threads and child processes.** LLDB 18 breaks when a thread is at a
  breakpoint at the moment another thread starts a child process. Seam could not cure it
  from outside (§21). LLDB 18 is Ubuntu 24.04's default; LLDB 19 is one `apt install`
  away and is not affected. Worth considering: make `seam` prefer a newer LLDB when one
  is installed.
- **Attaching to a program whose main thread is blocked** is refused after the timeout,
  because the helper cannot load. "Why is my program hung?" is a common reason to attach,
  and the merged call stack and native debugging need no helper. Staying attached without
  it, and loading it when the interpreter next runs Python, would cover that.
- A breakpoint on an `await` line stops twice on 3.13+ when an exception is thrown into
  the await (§24).
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
- Re-run the suite on Python 3.15.0 when it is released (only rc3 exists so far).
- Run the release workflow again now that the extension carries the adapter.

## Needs the project owner

- **One session by hand** in VS Code on Windows connected to WSL. It is the one path that
  could not be driven from here. About ten minutes: build or download the `.vsix`,
  install it in a WSL window, open `examples/pyo3-demo`, press F5, step in and out.
- **Publishing**: the PyPI name, the VS Code Marketplace / Open VSX publisher, making the
  repository public, a version number and a tagged release. Until then the README's pip
  install line (`git clone`) only works for people with access. Nothing has been
  published.

## Not planned

macOS, Windows, non-x86-64, free-threaded builds, PyPy, sub-interpreters, remote
debugging, debugging child processes. Each is a project of its own; see the README's
Limitations.
