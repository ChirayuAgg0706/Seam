# Publishing Seam 0.1.0

The release targets **Linux x86-64 with glibc, including WSL**. Native Windows,
macOS and ARM are outside this release. The debugger version is 0.1.0 in both Python
and VS Code. Canonical description:

> Debug Python and C, C++ or Rust together on Linux x86-64, including WSL.

## Release files and pages

- GitHub repository: `ChirayuAgg0706/Seam`; tag `v0.1.0`.
- PyPI distribution: `seam-debugger`; executable `seam`.
- VS Code extension: `seam-debugger`, currently using the placeholder publisher ID
  `seam`. Confirm ownership of the actual publisher before publishing to Marketplace
  or Open VSX; do not assume that ID is available.
- Release notes: [releases/0.1.0.md](releases/0.1.0.md).
- Screenshots: `vscode/images/`. These are unedited captures from the real VS Code
  packaged-extension acceptance job, not illustrative mockups.

`.github/workflows/release.yml` checks the version, builds the sdist and manylinux
stable-ABI wheel, packages the Linux x64 VSIX from that wheel, runs real installation
checks, checks metadata with `twine check --strict`, and writes `SHA256SUMS`.
Upload these exact files to the GitHub release; the PyPI workflow reuses its wheel
and sdist, verifies checksums and versions, and does not rebuild them.

## One-time PyPI account setup

1. Create an account at <https://pypi.org/account/register/>, verify the email address
   and configure two-factor authentication.
2. At <https://pypi.org/manage/account/publishing/>, add a pending GitHub publisher:

   | Field | Value |
   |---|---|
   | PyPI project name | `seam-debugger` |
   | Owner | `ChirayuAgg0706` |
   | Repository | `Seam` |
   | Workflow filename | `publish-pypi.yml` |
   | Environment | `pypi` |

3. The repository's GitHub environment must also be named `pypi`.

Pending publishers do not reserve package names. The account must finish setup before
the first successful upload. No account password or long-lived PyPI token is needed
in the repository. See the [official Trusted Publishing instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Publish and verify

After version/package checks pass and the owner has authorised publication:

1. Create and push `v0.1.0` at the release commit. Require that tag's release build to pass.
2. Download `seam-release-artifacts` from that run. Attach the wheel, source archive,
   VSIX and `SHA256SUMS` to the GitHub release, using `releases/0.1.0.md` as its body.
3. Verify the public GitHub downloads and their checksums. Install the downloaded wheel
   in a fresh Python 3.12 environment and run `seam doctor --python python3`.
4. Once PyPI account setup is complete, dispatch `publish-pypi.yml` with tag `v0.1.0`.
   The workflow rejects drafts and checks the published distributions before upload.
5. Install from PyPI with `pip install --only-binary=:all: seam-debugger==0.1.0` in
   another fresh environment, run doctor, and compare the downloaded wheel hash to
   the GitHub release asset.
6. Publish the same VSIX to Marketplace and Open VSX after their publisher accounts
   are configured. Check actual registry installation before announcing those routes.

Keep all descriptions, requirements and limitations aligned with the README. Marketing
starts after the public installation routes work.

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
Marketplace and Open VSX account/publisher setup and publication remain the next
release step. Neither registry has been published by this task.
