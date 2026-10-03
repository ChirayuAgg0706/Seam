#!/usr/bin/env bash
# Package the VS Code extension. Needs Node.js (npx). Output: vscode/seam-debugger-<version>.vsix
set -euo pipefail
cd "$(dirname "$0")/../vscode"
cp ../LICENSE LICENSE.txt
npx --yes @vscode/vsce package --allow-missing-repository
ls -1 ./*.vsix
