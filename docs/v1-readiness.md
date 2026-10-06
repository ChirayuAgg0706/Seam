# First-release readiness

Reviewed 2026-10-05–06, starting at `24d5b78`, with the local fixes below.

**Verdict: feature-complete for the documented Linux x86-64 scope; final release
sign-off is still outstanding.** No additional features are required for v1. The old
roadmap's "since v1" means the original implementation milestone, not a public release.
The package version is still `0.1.0`; choosing the first public version belongs to shipping.

## Work left before calling v1 complete

| Gate | Concrete completion condition | Status |
|---|---|---|
| Resolve the intermittent attach-exit failure | Explain and fix, or establish a reproducible test-environment cause for, the full-run timeout after continuing an attached process's uncaught exception. A passing retry alone is insufficient. | Resolved: Ubuntu's apport exception hook delayed exit; the fixture now restores CPython's hook. See evidence below. |
| Validate the final code | The final commit, including these fixes, passes the extended CI set: supported Python versions, LLDB 18/19/20, optimised builds, packaged VS Code, Neovim and clean-machine installation. Do not substitute an earlier green commit. | Passed on `610f460`; all extended CI jobs succeeded, including actual native-frame focus, disassembly and instruction stepping in VS Code. |
| Use the actual Windows + WSL editor path | Install the final Linux `.vsix` in a VS Code WSL window and complete the short acceptance session below. Record the versions and result. Automated Linux editor tests already pass, but this exact interactive path has not been signed off. | Owner reports checks through attach/detach working. Startup and disassembly fixes are now installed; reload the window and repeat those two checks. |
| Resolve the manual-session startup failure | Diagnose the failed helper injection reporting an already-deleted startup breakpoint; validate any fix against repeated launches. | Cause reproduced and corrected: synchronous network symbol requests could outlast injection's deadline. A delayed local server reproduced the exact error; disabling downloads passed the same probe, the HTTP regression, 20 repeated launches, and 17 relevant scenarios. Final extended CI and release package pass on `610f460`; owner retest remains. |
| Validate the artifacts that will ship | Build wheel, sdist and `.vsix` from that same final commit; run the release workflow and its install/startup checks. After choosing a release version, the tag must pass the version check too. | Passed on `610f460`; wheel, sdist and `.vsix` built and checked. No release was published. Version/tag selection remains shipping work. |

These are final verification tasks, not a new feature backlog.
Any further failure in these checks becomes a v1 bug to fix. Once they pass, the remaining
work is publishing.

The startup investigation also reproduced a distinct launch timeout in the installed
release during a repeated WSL launch/disassembly probe (sixth attempt after five passes).
Its log reached a running event around 45 seconds after the request and exceeded the
client's 60-second deadline. This is additional startup reliability evidence, not proof
that the deleted-breakpoint error has the same cause. Probe logs are preserved locally
as `build/launch-repeat-*.log` and `build/launch-repeat-results.txt`.

The attach scenario now supports `--repeat`. To investigate, run it with
`SEAM_LLDB=lldb-20 scripts/test.sh -q tests/test_attach_features.py::test_exception_breakpoints_on_an_attached_process --repeat=20`.
The original timeout was at `dap.wait_exit()` after all three exception stops and stack
inspections had succeeded. A diagnostic pause during exit found Ubuntu's apport hook
importing native libraries; exit eventually arrived after 25.82 seconds. Restoring
CPython's standard exception hook reduced the same measured exit to 0.01 seconds.
This matches the already-established launch-fixture diagnosis in decisions §32.

### One acceptance session (about 15–20 minutes)

1. In a Windows VS Code window connected to WSL, install the Linux `.vsix` into WSL,
   open `examples/pyo3-demo`, and build it as its README describes. Use LLDB 19 or 20.
2. Run **Seam: Check This Machine** and require the real debug-session check to pass.
3. Put a Python breakpoint on the native call in `demo.py`. Press F5, step into Rust,
   inspect native and Python frames/locals, and step back out. Verify source navigation
   goes to the correct files and the terminal displays the result.
4. Stop and restart the session. Confirm it starts cleanly; no orphan debuggee remains.
5. Attach to a running test Python process through the picker, stop at a breakpoint,
   then detach and confirm the process continues.
6. With an existing no-source scenario from `tests/test_nosource.py`, open VS Code's
   **Disassembly View** and step an instruction. The automated VS Code check and its
   screenshot pass; repeat on Windows/WSL. Disassembly breakpoints are out of scope.

Record the commit, artifact, VS Code/WSL/Python/LLDB versions, and any failures here.

## Fixed in this review

- **Slow startup and helper-injection failure.** Disable automatic network symbol
  downloads in the adapter's LLDB instance before creating a target. Local separate
  symbols stay enabled (`debugInfoLookup: true`); no user override is required. Phase
  timings and failed-call stop details make any further startup failure diagnosable.
- **Dimmed no-source native stops and disabled disassembly.** Keep the actual top native
  frame selectable and correct VS Code's initial selection when it skips that frame for
  the Python source below. Later manual frame selections remain under the user's control.
- **Newer-header builds failing on Python 3.12.** A wheel built using 3.14 headers loaded
  its helper but failed at `setBreakpoints` with agent error -2. The C dispatcher now
  constructs request bytes explicitly instead of using header-dependent `y#` format
  handling. A regression compiles the helper with each target's headers and exercises
  its real request/reply buffers on 3.12. See decisions §33.
- **Duplicate plain `await` breakpoints on cancellation/timeouts.** Both new scenario
  tests failed on Python 3.14 before the change: continuing stopped on the await again
  rather than at the exception handler. Candidate breakpoint hits now use the existing
  Python line filter. Non-hit lines still disable themselves in C; no monitoring is
  enabled when no breakpoints or steps exist. See decisions §24 and
  `test_continue_from_an_await_does_not_repeat_its_breakpoint` in `tests/test_async.py`.
- Removed this fixed bug from the current known-limit lists. Added the already-known
  failed-expression recovery limitation to the user-facing README.
- **Test reliability:** pause scenarios wait for target readiness instead of assuming
  imports finish within 0.5/1.5 seconds; Rust build caches are separated by checkout so
  copied/worktree checkouts cannot silently use the old checkout's DWARF source paths.
- The attach exception fixture now restores CPython's exception hook, avoiding Ubuntu's
  slow crash reporter. Replacement exception hooks retain separate scenario coverage.

## Verification evidence

- **Final code:** [extended CI run 37477454757](https://github.com/ChirayuAgg0706/Seam/actions/runs/37477454757)
  passed on `610f460`, now on main: all 11 required jobs succeeded. Full suite:
  **280 passed, 16 skipped** (13 opt-in, one -O2-only, one missing system Python debug
  information, and the documented LLDB 18 child-process limitation). LLDB 19/20 smoke:
  **235 passed, one skipped** each. The version/optimisation matrix, real VS Code and
  Neovim checks, lint/package checks and clean-machine installation all passed.
  The real VS Code test selected `nosource_work`, opened disassembly and advanced an
  instruction. Its screenshot was inspected; evidence is in `build/v1-final-editor-evidence/`.
- **Final artifacts:** [release run 37477455386](https://github.com/ChirayuAgg0706/Seam/actions/runs/37477455386)
  passed on `610f460`, including installed-wheel doctor, bundled helper comparison and
  all 19 bundled extension checks. Artifacts were downloaded to `build/v1-final-release/` locally;
  that `.vsix` is installed in this laptop's WSL VS Code server. The Rust demo is built.
  The prepared local `build/v1-release/LAPTOP-CHECK.md` and `acceptance.code-workspace`
  provide the remaining manual checks. Windows VS Code is 1.140.0, WSL Ubuntu 24.04,
  system Python 3.12 and LLDB 20.1.2. The owner passed through attach/detach on the prior
  build; only startup/disassembly need repeating after reloading this build.
- The final installed WSL extension's bundled adapter also passed `seam doctor` locally:
  launch, breakpoint, expression evaluation and clean exit all succeeded. The manual
  machine-check command still verifies that the editor itself selects that environment.
- The final installed adapter reached line 6 with stack/locals in 1.68 and 1.20 seconds
  with default local debug-symbol lookup enabled, measured through a DAP client and PTY
  (`build/startup-installed-final-results.txt`). These measurements exclude VS Code's UI
  startup and are not a universal bound for large projects.
- Before this review, [extended CI run 37338252923](https://github.com/ChirayuAgg0706/Seam/actions/runs/37338252923)
  passed on `736cc95`, including all version/optimisation cells, LLDB 18/19/20, real
  VS Code and Neovim sessions, and a clean-machine installation. `24d5b78` only records
  those results in documentation. This is evidence for the baseline, not today's patch.
- [Release run 37340340453](https://github.com/ChirayuAgg0706/Seam/actions/runs/37340340453)
  passed on `24d5b78` during this review: manylinux wheel, sdist, `.vsix` built around
  the wheel, installed-wheel `seam doctor`, byte-for-byte helper comparison, and all
  18 bundled extension checks. The helper reports a glibc 2.14 requirement. Nothing
  was published. These artifacts predate today's patch.
- Today's two regression scenarios pass on Python 3.13, 3.14 and 3.15.0rc3 with LLDB 20.
- Ruff, package-version consistency, and all 16 extension logic checks pass locally.
- The full local run on system Python 3.12 / LLDB 20 finished with **273 passed,
  14 skipped and seven failures**. Six were the test setup problems above and all six
  pass after those corrections. The remaining attach-exit timeout was traced to apport
  and its fixture corrected. This is not a claim of a green final full suite.
- On Python 3.14 / LLDB 20, all **48 async and breakpoint scenarios passed** after the
  await fix. The two new await regressions also passed individually on 3.13 and 3.15rc3.
- Helper ABI regression passes with 3.12, 3.13, 3.14 and 3.15rc3 headers, each running
  the resulting binary on system Python 3.12.
- The patched source builds an sdist and a wheel from that sdist using 3.14 headers.
  The installed wheel's doctor passes a real debug session on 3.12; a `.vsix` built
  around that wheel passes all 18 bundled extension checks. This is a local Linux
  build, not the manylinux release artifact.
- The corrected pause selection passes four cases (three Python pause/step repetitions
  and one NumPy pause). The four Rust scenarios also pass in the original checkout,
  independently confirming the copied-checkout cache diagnosis.
- The attached-process exception scenario passes eight further consecutive repetitions
  with the final helper changes (220.81 seconds, system Python 3.12 / LLDB 20). This
  preceded the diagnostic probe above; the probe established the crash-reporter cause.
- Local test artifacts for the editor acceptance session are in `build/v1-audit-dist/`,
  including `seam-debugger-linux-x64-0.1.0.vsix`. They are not published release artifacts.
- After correcting the attach exception hook, **20 consecutive repetitions passed**
  (227.36 seconds, system Python 3.12 / LLDB 20). Exit diagnostics and repeat logs are
  retained locally under `build/attach-exit-*.txt` and `build/v1-attach-final.log`.

## Known issues that can remain in a bounded v1

These are existing defects or limitations, not claims that they have been fixed. The
recommendation is to ship with the stated workarounds and scope, rather than promise
that every combination works.

| Issue | User impact / v1 treatment |
|---|---|
| Large-project symbol loading | Local symbol parsing still depends on the imported libraries and their debug information. The earlier 10+ second WSL delay was traced to automatic network downloads and corrected (§34); disabling all separate debug-info lookup is no longer the workaround. This does not establish a universal latency bound for large projects. |
| LLDB 18 with threads starting child processes | Can lose the debug session. Use LLDB 19/20 for these workloads, as documented. Merely installing a newer LLDB is insufficient when `lldb` still resolves to 18: explicitly set `SEAM_LLDB`. Automatically choosing a newer version is optional hardening. |
| An evaluated expression crashes or times out | The target interpreter can remain damaged after unwinding. Restart the session; do not promise recovery. This is now explicit in README limitations. |
| Native function breakpoint by bare name hits binding glue | Seen with pybind11/contourpy. Use a source-line breakpoint or a sufficiently specific native name. Better filtering is a follow-up bug fix. |
| Both uncaught and user-unhandled filters enabled for a thread exception | Two stops describe two exception events. Disable one filter when duplicate stops are distracting. |
| `_asyncio` C frames in stacks on Ubuntu's Python | Extra internal frames; cosmetic cleanup can follow v1. |
| LLDB 20 cannot unwind through optimised nanobind library code | Missing native frames in that combination. Use a debug build; retain the existing warning and limitation. |
| Thread-heavy, logpoint-heavy and very large-module workloads | Known performance limits: roughly 2x for thread-heavy work; trial logpoints about 8 ms/hit; first step into pydantic-core about 4 s. Subsequent large-module steps are fast. Do not claim universal low overhead. |

Two historic one-off stepping failures remain unreproduced in STATUS. They are not
proof of a current fix; if either recurs in final verification, retain the logs and fix
it before sign-off. A green test suite is evidence, not a guarantee of no other bugs.

## Keep outside v1

Do not add helper-free attach to blocked processes, richer object expansion at native
stops, child-process debugging, another front end, more platforms, remote debugging,
disassembly breakpoints or new UI markers to complete this release. Existing documented
limits on optimised code, blocked attach and native-stop evaluation define the scope.

Only claim Python versions actually validated. The recorded 3.15 evidence is for rc3;
if claiming support for a later final build, run that build first.

## Shipping work after sign-off

- Choose the public version and keep Python, extension, documentation examples and tag
  consistent (`tools/check_versions.py`).
- Set the real PyPI / Marketplace / Open VSX identities and repository visibility;
  replace source-access assumptions in installation instructions as appropriate.
- Build and publish the approved artifacts, add release notes, and verify installation
  from the actual public download/registry paths.

Publisher accounts, public visibility and a release tag have not been changed by this review.
