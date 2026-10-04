# Contributing to Seam

## Getting set up

Seam is developed on Linux x86-64 (WSL works). You need LLDB 18 or newer with its Python
bindings, gcc/g++, the CPython headers, [uv](https://docs.astral.sh/uv/) and, for the
PyO3 scenarios, a Rust toolchain.

```bash
scripts/test.sh -q                 # the whole suite against /usr/bin/python3.12
```

The script creates a virtual environment under `~/.cache/seam/venv` with pytest and the
binding libraries the test extensions are built with. Useful variations:

```bash
SEAM_TEST_PYTHON=/path/to/python3.14 scripts/test.sh -q   # another interpreter
SEAM_TEST_OPT=O2 scripts/test.sh -q -m smoke              # optimised native test code
SEAM_TEST_REPEAT=20 scripts/test.sh -q -k stepping        # loop the stepping scenarios
SEAM_LLDB=lldb-19 scripts/test.sh -q -m smoke             # another LLDB
uvx ruff check src tests tools                            # lint
```

## How the code is laid out

| Path | What it is |
|---|---|
| `src/seam/cli.py` | `seam dap` (starts LLDB, relays the protocol) and `seam doctor`. |
| `src/seam/adapter/` | The debug adapter. Runs inside LLDB's embedded Python; standard library only. One class, `Adapter` (`server.py`, which lists all its state), assembled from a mixin per concern: |
| &nbsp;&nbsp;`protocol.py` | the DAP connection, request dispatch, access to the target's memory and to the agent |
| &nbsp;&nbsp;`session.py` | launch, attach, the terminal, exit, detach |
| &nbsp;&nbsp;`stops.py` | process events: deciding what a stop is, reporting it or carrying on |
| &nbsp;&nbsp;`stepping.py` | stepping across the boundary; what counts as user code |
| &nbsp;&nbsp;`breakpoints.py` | source-line, function, data and exception breakpoints |
| &nbsp;&nbsp;`stack.py` | the merged call stack, variables, expressions |
| &nbsp;&nbsp;`common.py` | constants and small helpers |
| `src/seam/adapter/pyread.py`, `layouts.py`, `linetable.py` | Reading the interpreter's state from raw memory. |
| `src/seam/_target/seam_agent.py`, `_seam_trap.c` | The helper that runs inside the debugged program. |
| `src/seam/terminal.py` | The holder that runs in the editor's terminal (`console` option). |
| `tests/` | Scenario tests: real programs under Seam, driven by a scripted DAP client (`dapclient.py`). |
| `docs/decisions.md` | Why things are the way they are. Read the relevant section before changing something that looks odd. |

## Ground rules

- **A feature is done when a scenario test has been seen passing**, not when the code
  looks right. Tests launch real processes; there are no mocks of LLDB or of CPython.
- **An intermittent failure is a bug.** Loop the scenario (`SEAM_TEST_REPEAT`) until it
  reproduces, read the adapter log the failure report carries, and fix the cause. Several
  entries in `docs/decisions.md` started as a failure seen once.
- **Fix the cause, not the test.** If a test's expectation was wrong, say so in the commit.
- **Python is run in the debugged program only at safe points.** At a native stop the
  adapter reads memory. If a change needs to run something in the target anywhere else,
  it needs a section in `docs/decisions.md` saying why that is safe.
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
rather than skipping. Pushes to branches other than `main` start nothing.

A run started by hand can be narrowed down, which is the cheap way to check one thing:

```bash
gh workflow run ci.yml --ref my-branch -f only=editors      # one job: full, smoke, lldb,
                                                            # editors, clean-machine, vsix
gh workflow run ci.yml -f soak=test_breakpoints -f soak_rounds=12 \
   -f soak_lldb=19 -f soak_load=2                           # loop a selection under load
```

The soak job prints how many leftover stops and stale frame lists the adapter worked
around (`docs/decisions.md` §17, §18). Both should be zero.
