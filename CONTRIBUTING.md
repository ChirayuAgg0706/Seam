# Contributing to Seam

## Set up

Develop and test Seam on Linux x86-64 or Apple Silicon macOS 14+. WSL works on
Windows. Use LLDB with Python bindings, a C/C++ compiler, CPython headers and [uv](https://docs.astral.sh/uv/). PyO3 tests also need a
Rust toolchain. On Linux, use LLDB 19 or 20 for new installations. On macOS, install
Apple's command-line tools and use an ARM64 interpreter.

```bash
scripts/test.sh -q                 # the whole suite against /usr/bin/python3.12
```

The script creates `~/.cache/seam/venv` with pytest and the binding libraries used
by the test extensions. To choose an interpreter, compiler optimisation or test group:

```bash
SEAM_TEST_PYTHON=/path/to/python3.14 scripts/test.sh -q   # another interpreter
SEAM_TEST_OPT=O2 scripts/test.sh -q -m smoke              # optimised native test code
SEAM_TEST_REPEAT=20 scripts/test.sh -q -k stepping        # loop the stepping scenarios
SEAM_LLDB=lldb-19 scripts/test.sh -q -m smoke             # another LLDB
uvx ruff check src tests tools                            # lint
SEAM_TEST_SCALE=1 scripts/test.sh -q tests/test_scale.py  # timings with a 15,000-function module
tools/build_projects.sh                                   # regex, msgpack, contourpy, pydantic-core
SEAM_TEST_PROJECTS=1 scripts/test.sh -q tests/test_projects.py   # ... and sessions against them
```

The last three are opt-in: they are not part of the ordinary suite or of CI. The projects
are built from source with debug info under `~/.cache/seam/scale` (about 5 minutes and
1 GB).

## Code layout

| Path | What it is |
|---|---|
| `src/seam/cli.py` | `seam dap` (starts LLDB, relays the protocol) and `seam doctor`. |
| `src/seam/adapter/` | The debug adapter. Runs inside LLDB's embedded Python; standard library only. One class, `Adapter` (`server.py`, which lists all its state), assembled from a mixin per concern: |
| &nbsp;&nbsp;`protocol.py` | the DAP connection, request dispatch, access to the target's memory and to the agent |
| &nbsp;&nbsp;`session.py` | launch, attach, the terminal, exit, detach |
| &nbsp;&nbsp;`stops.py` | process events: deciding what a stop is, reporting it or carrying on |
| &nbsp;&nbsp;`stepping.py` | stepping across the boundary; what counts as user code |
| &nbsp;&nbsp;`entrytraps.py` | step-in for large modules: trap instructions Seam places itself |
| &nbsp;&nbsp;`breakpoints.py` | source-line, function, data and exception breakpoints |
| &nbsp;&nbsp;`stack.py` | the merged call stack, variables, expressions |
| &nbsp;&nbsp;`sources.py` | source paths: the debug info's, this machine's (`sourceMap`), the editor's |
| &nbsp;&nbsp;`disassembly.py` | the listing for frames without source, stepping by instruction |
| &nbsp;&nbsp;`common.py` | constants and small helpers |
| `src/seam/adapter/pyread.py`, `layouts.py`, `linetable.py` | Reading the interpreter's state from raw memory. |
| `src/seam/_target/seam_agent.py`, `_seam_trap.c` | The helper that runs inside the debugged program. |
| `src/seam/terminal.py` | The holder that runs in the editor's terminal (`console` option). |
| `vscode/` | The VS Code extension: `extension.js` (wiring), `lib/` (which interpreter, which processes, starting the bundled adapter; no VS Code needed, checked by `test/unit.js`), `bundled/` (the adapter as packaged; filled by `scripts/build-vsix.sh`), `test/` (the editor check run by CI). |
| `tests/` | Scenario tests: real programs under Seam, driven by a scripted DAP client (`dapclient.py`). |
| `docs/decisions.md` | Why things are the way they are. Read the relevant section before changing something that looks odd. |

## Ground rules

- Complete a feature only after its scenario test passes. Tests launch real programs
  and do not mock LLDB or CPython.
- Treat an intermittent failure as a bug. Repeat the scenario with `SEAM_TEST_REPEAT`,
  read the adapter log in the failure report, and fix the cause.
- If a test expectation was wrong, explain the correction in the commit.
- Run Python in the program only at safe points. At native stops, read memory.
  Any exception needs an entry in `docs/decisions.md` explaining why the call is safe.
- Record design decisions, and things tried and rejected, in `docs/decisions.md`. Keep
  `STATUS.md` and `ROADMAP.md` truthful: what is tested, what is not, what is known broken.
- New Python versions: add the `_Py_DebugOffsets` field list from that version's
  `Include/internal/pycore_debug_offsets.h` to `layouts.py` and run the suite against it.

## Continuous integration

Every push runs the full suite on Python 3.12, and the linter. The extended set runs once
a week, when started by hand from the Actions page, and on any push whose commit message
contains `[ci full]`: smoke scenarios on the other supported versions and at `-O2`, the
same under LLDB 19 and 20, a clean-machine install, and the editor checks (the packaged
extension inside a real VS Code, and the documented configuration in a headless Neovim).
It is split this way because the repository lives on GitHub's free tier; use `[ci full]`
for changes to the adapter's core, to stepping, or to anything version-specific, and
`[skip ci]` for changes that touch only documentation. A missing toolchain fails CI
rather than skipping. The Apple Silicon workflow tests macOS 14 and 15 with CPython 3.12 through 3.14.
It can also be started from the Actions page.

To run one CI job or repeat a test group under load:

```bash
gh workflow run ci.yml --ref my-branch -f only=editors      # one job: full, smoke, lldb,
                                                            # editors, clean-machine, vsix
gh workflow run ci.yml -f soak=test_breakpoints -f soak_rounds=12 \
   -f soak_lldb=19 -f soak_load=2                           # loop a selection under load
```

The soak job prints how many leftover stops and stale frame lists the adapter worked
around (`docs/decisions.md` §17, §18). Both should be zero.

The `editors` job is the only place the extension runs in a real VS Code; it keeps
pictures of the window as the artifact `editor-check-screenshots`. Look at them when
changing what the adapter sends for frames, variables or exceptions.
