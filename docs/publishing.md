# Publishing Seam

The release targets **Linux x86-64 with glibc, including WSL**. Native Windows,
macOS and ARM are outside this release. The released debugger is 0.1.0.
Extension 0.1.2 combines the Joined S logo, demo GIF and revised copy while
bundling the unchanged debugger 0.1.0. Canonical description:

> Debug Python and C, C++ or Rust in one session on Linux x86-64, including WSL.

## Release files and pages

- GitHub repository: `ChirayuAgg0706/Seam`; tag `v0.1.0`.
- PyPI distribution: `seam-debugger`; executable `seam`.
- VS Code extension: `chirayuagg0706.seam-debugger`. The owner created the Marketplace
  publisher `chirayuagg0706` on 2026-10-07. Open VSX is deferred.
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
6. Publish the publisher-qualified VSIX to Marketplace and Open VSX using the steps
   below. Check actual registry installation before announcing those routes.

Keep all descriptions, requirements and limitations aligned with the README. Marketing
starts after the public installation routes work.

## Marketplace and Open VSX

The original tag's VSIX used the placeholder publisher `seam`. Keep that release asset
and its published checksum unchanged. The registry package is built from the original
released wheel, with the owned publisher ID and corrected README image URLs. It is
attached as `seam-debugger-chirayuagg0706-linux-x64-0.1.0.vsix`, with a companion
`.sha256` asset. No debugger source or Python distribution is changed.

For the first Marketplace publication, uploading in the browser is sufficient:

1. Download the publisher-qualified VSIX from the GitHub release.
2. Open <https://marketplace.visualstudio.com/manage>, select publisher
   `chirayuagg0706`, then **New extension → Visual Studio Code**.
3. Upload that VSIX and follow the portal's publication prompts. Wait for validation
   to finish before claiming the extension is installable.

For automatic Marketplace publishing instead, create an Azure DevOps PAT under the
Microsoft account that owns the publisher: **All accessible organizations**, custom
scopes, **Marketplace → Manage** only. Store it as GitHub repository secret `VSCE_PAT`;
never put it in chat, source or command arguments. A short expiration suffices for
this release. Microsoft retires global PATs on 2026-12-01; future automation will need
Microsoft Entra ID authentication. See the
[official publishing instructions](https://code.visualstudio.com/api/working-with-extensions/publishing-extension).

Open VSX first-time account setup:

1. Create an [Eclipse account](https://accounts.eclipse.org/user/register) and set its
   GitHub username to `ChirayuAgg0706`.
2. Sign into <https://open-vsx.org> with that GitHub account. In Settings, connect the
   Eclipse account and read and accept the Publisher Agreement if you agree.
3. Generate an Open VSX access token and store it as repository secret `OVSX_PAT`.
   The workflow creates namespace `chirayuagg0706` if it is absent, then publishes.
   Existing namespaces must grant this account permission; a matching name alone
   does not establish ownership.

See [Open VSX's publishing instructions](https://github.com/eclipse-openvsx/openvsx/wiki/Publishing-Extensions).
Its Trusted Publishing requires an existing published extension and verified namespace
ownership, so it is a follow-up to the first token-based upload.

Dispatch `publish-extension.yml` with registry `marketplace`, `open-vsx` or `both`,
tag `v0.1.0` and the publisher-qualified asset filename. The workflow verifies its
checksum, identity, version, platform, bundled adapter/helper and public screenshot
URLs before uploading the same file. It never rebuilds the package during publication.
For a manual Marketplace upload, dispatch only `open-vsx` after its setup is complete.

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
