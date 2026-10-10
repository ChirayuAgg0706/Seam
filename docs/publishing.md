# Publishing Seam

The prepared package versions are debugger 0.1.2 and extension 0.1.5.
Marketplace currently serves extension 0.1.4; uploading 0.1.5 remains a manual step.
Both extension versions bundle debugger 0.1.2. Use this description on each listing:

> Debug Python and C, C++ or Rust in one session.

Keep platform details in requirements and installation instructions. Supported
platforms are Apple Silicon macOS 14+ and Linux x86-64 with glibc. Windows uses WSL.
Use CPython 3.12, 3.13 or 3.14. Linux also has compatibility evidence for 3.15.0rc3.

## Build and verify

1. Run the version check and both platform test workflows at the release commit.
2. Dispatch `.github/workflows/release.yml` at that commit. It builds a manylinux
   x86-64 wheel, a macOS ARM64 wheel, a source archive, and both platform VSIX files.
   It checks installed packages and metadata before combining checksums.
3. Download `seam-combined-release`. Keep the exact files from that workflow.
   Each VSIX filename includes publisher `chirayuagg0706` and its platform.
4. Create tag `v0.1.2` at the verified commit. Upload the verified files and
   `SHA256SUMS` to the GitHub release with [these notes](releases/0.1.2.md).
5. Dispatch `publish-pypi.yml` with tag `v0.1.2`. It verifies the GitHub assets and
   publishes those exact Python distributions through Trusted Publishing.
6. Install the public wheel in a fresh environment on each platform and run
   `seam doctor --python /path/to/project/python`. Compare PyPI hashes with the
   GitHub assets. Verify the public README images and download links.

The PyPI project is `seam-debugger`. Its GitHub Trusted Publisher uses owner
`ChirayuAgg0706`, repository `Seam`, workflow `publish-pypi.yml`, and environment
`pypi`. That account setup is complete. No PyPI token belongs in the repository.

## Marketplace

The extension ID is `chirayuagg0706.seam-debugger`. Publication currently uses
manual upload, so there is no `VSCE_PAT` repository secret.

1. Download both publisher-qualified VSIX files from the GitHub release.
2. Open [Manage Publishers & Extensions](https://marketplace.visualstudio.com/manage)
   and select publisher `chirayuagg0706` and the existing Seam extension.
3. Use its update/upload action to upload the Linux x64 and macOS ARM64 packages
   for version 0.1.5. Both belong to the same extension listing.
4. Wait for validation, then test Marketplace installation on each platform.
   A successful GitHub build does not establish Marketplace availability.

The package includes the logo, real screenshots, demo GIF and README. Screenshots
are in `vscode/images/`. Open VSX is deferred at the owner's request.
The `publish-extension.yml` workflow remains available for future token-based
publication. It verifies the exact asset before uploading and does not rebuild it.

## Machine-check fix, 2026-10-10

Extension 0.1.5 expands `${workspaceFolder}` in `python.defaultInterpreterPath`
before **Seam: Check This Machine** invokes doctor. The Python debugger remains
0.1.2. No new Python distribution is published for this editor fix.

Both platform VSIX files and their checksums are in `build/extension-0.1.5/`.
They reuse the released platform bundles from extension 0.1.4. Each of the 27 bundled
debugger/helper files is byte-for-byte unchanged. The extension changes are the
interpreter resolver, version metadata and changelog. Package identity, helper
architecture, screenshots and checksums pass the publication guard.

The unit regression failed before the fix and passes after it. The installed Linux
package's machine check passes with an explicit workspace-variable interpreter,
including a directory containing spaces and literal dollar signs. New Mac execution
also passed the packaged VS Code check on the Apple Silicon runner, including the
workspace-variable machine-check regression. See
[the release notes](releases/extension-0.1.5.md).

The full installed VS Code editor check also passed in a project directory with
spaces: F5, Python/Rust stepping, exceptions, terminal output, attach/detach,
disassembly, both machine-check paths and expected startup refusals. Unit checks
passed 18 cases. The focused machine-check run also passed in a directory containing
literal `$&`.

The broader check found that VS Code 1.141.0 can corrupt `${file}` substitution when
the project directory contains literal `$&`. The protocol records the corrupted
program path before Seam receives it; passing the same path directly to the adapter
succeeds. The machine check passes in that directory. This separate editor behavior
is not fixed by 0.1.5; use a directory without `$&` for VS Code variable-based launches.
Local evidence is in `build/extension-0.1.5/`.

The first full Mac run passed its packaged-editor, real-project and other matrix
jobs, but the macOS 14 Python 3.12 job reached its time limit. Its traceback showed
an unbounded wait in the test client's terminal cleanup, after the program had
exited successfully. Cleanup now closes the test PTY and bounds the wait after
SIGKILL to 15 seconds. Seven focused Linux terminal checks pass with this harness
change. It changes no released debugger or VSIX file.

## Apple Silicon publication record, 2026-10-08

- [GitHub release 0.1.2](https://github.com/ChirayuAgg0706/Seam/releases/tag/v0.1.2)
  contains both platform wheels, the source archive, both extension 0.1.4 VSIX files,
  companion checksums and the combined checksum list.
- [Release build 37784619768](https://github.com/ChirayuAgg0706/Seam/actions/runs/37784619768)
  passed on Linux and macOS. It verified installed wheels, doctor, the bundled adapter,
  thin ARM64/ELF x86-64 helpers, versions, metadata and package checksums.
- [PyPI 0.1.2](https://pypi.org/project/seam-debugger/0.1.2/) is public. All three Python
  distribution downloads match the verified GitHub hashes. A fresh Linux installation
  passed doctor, and pip selected the Mac ARM64 wheel with the Mac platform tags.
- [Trusted publication 37785234772](https://github.com/ChirayuAgg0706/Seam/actions/runs/37785234772)
  passed. Version 0.1.1 introduced Mac support; 0.1.2 clarifies the attach instructions
  and changes no debugger behavior.
- The owner subsequently uploaded both Marketplace 0.1.4 packages. On October 9,
  installation by extension ID into an isolated Linux/WSL VS Code profile passed
  doctor and Python-to-Rust stepping. Both public platform downloads were verified.
  Open VSX remains deferred.

The final runtime and real-project evidence is in [the Mac validation record](macos-stage1.md).

## Historical releases

The records below describe the original Linux releases. Their assets, hashes and
platform requirements remain unchanged. Use the current README for new installations.

## First-release publication record (2026-10-07)

The owner approved version 0.1.0, public visibility including repository history, and
GitHub/PyPI publication. The owner completed the PyPI account and pending publisher setup.

- [GitHub release v0.1.0](https://github.com/ChirayuAgg0706/Seam/releases/tag/v0.1.0)
  is public, with all four assets; the tag points to `b8c82a2`.
- [PyPI 0.1.0](https://pypi.org/project/seam-debugger/0.1.0/) is public. Its wheel
  and sdist hashes match the GitHub assets exactly.
- [Release/tag build 37630396762](https://github.com/ChirayuAgg0706/Seam/actions/runs/37630396762)
  passed real wheel/bundle installation checks, metadata checks and checksum generation.
- [Release commit CI 37630375050](https://github.com/ChirayuAgg0706/Seam/actions/runs/37630375050)
  passed: 296 tests, 16 documented skips, lint, version and VSIX checks. Debugger code
  is unchanged from the full compatibility/editor validation on `c3d84d1`.
- [PyPI publishing 37631245619](https://github.com/ChirayuAgg0706/Seam/actions/runs/37631245619)
  verified the published GitHub assets and uploaded them through Trusted Publishing.
- Unauthenticated GitHub downloads, all asset checksums, packaged sources/screenshots
  and public screenshot URLs passed. The downloaded wheel's doctor passed a real session.
- An uncached install from `https://pypi.org/simple` into a separate Python 3.12
  environment passed its version check and real-session doctor under LLDB 20.1.2.

Published SHA-256:

| Asset | SHA-256 |
|---|---|
| Wheel | `788303c0207fc6f53eafb65194a82e508ce29d86646de56e0a75e1907ead190e` |
| Sdist | `ddbec1fec49591d7ffafbb79536a71752ff518f47c58fa038ee594bfb270f49a` |
| VSIX | `25a9468f05e557793541618171f8e082ad97c19b5bfe50783b9f7f819684025c` |

Local verification logs are `build/public-release-verification.txt`,
`build/pypi-release-verification.txt` and `build/public-vsix-verification.txt`.
Marketplace was subsequently published by the owner, as recorded below. Open VSX
publication remains the next release step.

## Extension registry preparation (2026-10-07)

The owner confirmed creation of Marketplace publisher `chirayuagg0706`. Commit
`588ede3` updates the extension identity, makes the editor checks read that identity
from the manifest, fixes packaged README screenshot URLs for this repository's
`vscode/` subdirectory, and adds the verified-file registry publishing workflow.

- The publisher-qualified VSIX and its `.sha256` are attached to the public 0.1.0
  release; unauthenticated downloads passed identity, version, platform and checksum checks.
- Every bundled debugger/helper file matches the published Python wheel byte for byte.
- The extracted extension passed doctor, including a real breakpoint/evaluation/exit session.
- All 17 extension unit checks and the publishing script's lint passed. The verification
  guard rejected wrong publishers, wrong versions and an altered package.
- [Editor CI 37635105386](https://github.com/ChirayuAgg0706/Seam/actions/runs/37635105386)
  passed packaged VS Code acceptance with and without Microsoft's Python extension,
  plus Neovim acceptance, under the new extension ID.
- [Main CI 37635094451](https://github.com/ChirayuAgg0706/Seam/actions/runs/37635094451)
  passed: 296 tests, 16 documented skips, lint, versions and extension packaging.

Registry VSIX SHA-256:
`6939283aaa3acfbac92711b5eb10d58f8d059359566eb10998a3c5bd5ca25398`.
Local verification log: `build/registry-package-verification.txt`.
The owner subsequently uploaded this package to Marketplace. Open VSX still needs
Eclipse/GitHub account setup, the Publisher Agreement and `OVSX_PAT`.

## Marketplace publication (2026-10-07)

The owner uploaded the verified publisher-qualified VSIX through the Marketplace
portal, which displayed successful validation, version 0.1.0 and Public availability.
The [public listing](https://marketplace.visualstudio.com/items?itemName=chirayuagg0706.seam-debugger)
was fetched without authentication. The VS Code CLI installed
`chirayuagg0706.seam-debugger` 0.1.0 directly from Marketplace into an empty, isolated
extensions directory and profile, leaving the existing laptop installation untouched.

All 37 installed files matched the uploaded VSIX (with VS Code's added manifest
`__metadata` excluded from the comparison); the registry selected target `linux-x64`.
The installed bundle passed doctor under LLDB 20.1.2, including a real launch,
breakpoint, expression evaluation and successful exit. Local log:
`build/marketplace-install-verification.txt`.

Marketplace publication is complete. Open VSX remains unpublished pending account setup.

## Extension logo update 0.1.1 (2026-10-07)

The owner selected Joined S (concept 01) and authorised uploading it. The standalone
PNG is `vscode/images/icon.png`, included through the manifest's `icon` field. Its
appearance was inspected at 32, 64, 128 and 256 pixels on light and dark backgrounds.
See [brand.md](brand.md) for the design and image-generation provenance.

This is extension 0.1.1, declaring `seamAdapterVersion: 0.1.0`; PyPI and the debugger
remain 0.1.0. Extension-only releases use `extension-v0.1.1` and their own changelog,
without modifying the existing v0.1.0 tag or distributions.

Marketplace update instructions: open the existing Seam extension in
<https://marketplace.visualstudio.com/manage>, choose **Update**, and select
`seam-debugger-chirayuagg0706-linux-x64-0.1.1.vsix`. Wait for validation. Publishing
still requires the owner's browser upload because no Marketplace credential is
configured for this task. Open VSX publication is deferred at the owner's request.

Local validation passed all 19 extension/bundled-adapter checks and lint. All 36
pre-existing extension files apart from the changed manifest match the public 0.1.0
VSIX byte for byte, including every runtime file. A fresh isolated VS Code CLI
installation includes the icon and passes doctor's real breakpoint/evaluation/exit
session. The publication guard also rejects a false bundled-adapter version declaration.
Local log: `build/logo-release-verification.txt`.

Extension 0.1.1 VSIX SHA-256:
`521948764a193d2b0ba08074a1daa5a0f2bd1cb1a49c1970fb6c119d48d86618`.

## Combined extension update 0.1.2. 2026-10-08

Extension 0.1.2 includes the Joined S icon, recorded Python-to-Rust GIF and revised
public copy. It bundles the released debugger 0.1.0. The READMEs, Neovim and demo
guides, release notes, contributor and security text, settings descriptions and
installation errors follow the installed unslop skill. The status and roadmap
now reflect completed publication and automatic LLDB selection.

Prepared package: `build/v1-presentation-release/seam-debugger-chirayuagg0706-linux-x64-0.1.2.vsix`.
Its companion `.sha256` records this hash:
`bfe304bad7050dd28728a93191b00c0fc7f138b7549a8f5ac8e6ea0dbe0512de`.
Release copy is in [releases/extension-0.1.2.md](releases/extension-0.1.2.md).

Validation passed 19 extension and bundled-adapter checks, lint and version checks,
the publication guard, and a real installed VS Code session. Step Into opened Rust;
locals showed `i = 2` and `total = 1`; Step Out returned to Python and the completed
assignment showed `result = 30`. The program exited successfully. An installation
in an empty CLI profile also passed the machine check's breakpoint, evaluation and
exit session.

All 30 debugger and extension-library files match 0.1.1 byte for byte. Configuration
defaults and IDs are unchanged. Packaged images match their source files. Public
document links, shell syntax, six JSON examples and the unchanged tested Neovim
configuration passed checks. The extension's two installation-error strings changed;
its debugging logic did not. Local evidence is in `build/v1-presentation-release/`.

To update Marketplace, open the existing Seam extension in the publisher portal,
choose **Update**, and upload this VSIX. Package preparation does not publish it.
No publishing credential is configured for this task. Open VSX remains deferred.

The rewritten root README is also the source for future PyPI metadata. PyPI's
published 0.1.0 page keeps its existing description until a new Python package
release. Do not replace the published 0.1.0 wheel, source archive or checksums.
