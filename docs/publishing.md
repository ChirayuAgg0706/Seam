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
