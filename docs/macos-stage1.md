# Apple Silicon validation

Seam's Mac port uses Apple's LLDB and native ARM64 CPython. The release targets
macOS 14 and newer. Intel Macs and Python running through Rosetta are unsupported.
The initial experiment required `SEAM_EXPERIMENTAL_MACOS=1`; current builds work
without that flag.

## What the checks cover

The Apple Silicon workflow runs on actual macOS 14 and 15 ARM64 runners with
CPython 3.12, 3.13 and 3.14. It exercises the existing debugger behavior, including
Python and native stepping, C API, PyO3, pybind11, nanobind, Cython, mixed stacks,
variables, breakpoints, exceptions, attach, async code, threads, child processes,
terminal input, crash handling and disassembly. Optimized native builds run as well
as debug builds.

Separate jobs install freshly built wheels, repeat complete debug sessions, and
run the packaged extension in real VS Code. The Python extension interpreter
selection and Neovim's documented nvim-dap configuration are checked too.

[The port validation on 7c32b9c](https://github.com/ChirayuAgg0706/Seam/actions/runs/37762461119)
passed the full CPython 3.12 suite, the Mac 14 smoke checks, Mac 15 CPython 3.13,
optimized regressions, installed-wheel sessions, and packaged editor checks.
Mac 14 attach tests passed but their artifact upload failed. A Mac 15 optimized
run stalled during a debugger-exit test; subsequent runs include Python stack
capture and a focused repeat check. Final validation is recorded in the readiness
and publication records.

## Port changes

- Read ARM64 arguments and program counters through their native registers.
- Follow framework Python's launcher exec to the interpreter entry point.
- Use libproc and sysctl for process discovery instead of `/proc`.
- Restore inherited software and hardware breakpoints in forked children.
- Keep optimized frameless and inline native functions in the mixed stack.
- Pass faults through macOS signal handlers before resuming the program.
- Package a thin ARM64 helper with a macOS 14 deployment target.

## Limits

The Linux entry-trap shortcut writes `/proc/<pid>/mem` and cannot run on macOS.
Mac Step Into excludes modules with more than 20,000 functions; source and function
breakpoints remain available. With 15,000 functions loaded, the measured first
Step Into took 5.3 seconds and repeated entries took 2.7-3.2 seconds. The Linux
subsecond performance bound does not apply to this Mac path. LLVM can omit an interrupted native frame under a
signal handler. Seam keeps Python callers and reports native unwind problems.

The CI checks needed no signing changes, `sudo`, or system security changes.
That proves permissions for the tested runners and interpreters. It does not give
Seam permission to attach to protected system processes or every signed executable.

## Run the checks

Dispatch `.github/workflows/macos-stage1.yml` from the Actions page. It tests the
selected commit without publishing packages.

After installing a wheel on an Apple Silicon machine:

```sh
SEAM_LLDB=/usr/bin/lldb python scripts/macos-stage1-check.py --output mac-results --repeat 5
```

The script drives the installed adapter rather than importing it from the checkout.
