#!/usr/bin/env bash
# Package the VS Code extension with Seam's debug adapter inside it.
#
#   scripts/build-vsix.sh                    the adapter from src/, its helper compiled here
#                                            (needs gcc and the Python headers)
#   scripts/build-vsix.sh dist/seam-*.whl    the adapter and helper from a built wheel
#                                            (the release: a manylinux helper for old glibc)
#   scripts/build-vsix.sh --stage DIR [WHL]  only lay the adapter out as DIR/seam and
#                                            DIR/__main__.py, as the extension carries
#                                            it; no Node.js needed (used by the tests)
#
# Output: vscode/seam-debugger-<platform>-<version>.vsix. Packaging needs Node.js (npx).
# The package target must match the helper's architecture and operating system.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER=seam/_target/_seam_trap.abi3.so
target="${SEAM_VSIX_TARGET:-}"
if [ -z "$target" ]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) target=linux-x64 ;;
    Darwin-arm64) target=darwin-arm64 ;;
    *) echo "build-vsix: unsupported build host; set SEAM_VSIX_TARGET for a built wheel" >&2; exit 1 ;;
  esac
fi

stage_only=""
if [ "${1:-}" = "--stage" ]; then
  stage_only="${2:?--stage needs a directory}"
  shift 2
fi
wheel="${1:-}"
dest="${stage_only:-$ROOT/vscode/bundled}"

fail() {
  echo "build-vsix: $*" >&2
  exit 1
}

mkdir -p "$dest"
rm -rf "$dest/seam"
if [ -n "$wheel" ]; then
  [ -f "$wheel" ] || fail "no such wheel: $wheel"
  unpacked="$(mktemp -d)"
  python3 -m zipfile -e "$wheel" "$unpacked"
  [ -f "$unpacked/$HELPER" ] || fail "$wheel has no compiled helper ($HELPER) in it"
  cp -r "$unpacked/seam" "$dest/seam"
  rm -rf "$unpacked"
else
  # The sources only: a helper built in src/ for the tests is not what ships.
  (cd "$ROOT/src" && find seam \( -name '*.py' -o -name '*.c' \) -print0) |
    while IFS= read -r -d '' file; do
      mkdir -p "$dest/$(dirname "$file")"
      cp "$ROOT/src/$file" "$dest/$file"
    done
  include="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"
  [ -f "$include/Python.h" ] ||
    fail "the Python headers are missing ($include/Python.h). Install them (Debian/Ubuntu: apt install python3-dev) or pass a built wheel."
  # As setup.py builds it. The helper uses the limited API, so headers of any 3.12+ do.
  shared=(-shared -fPIC)
  if [ "$target" = darwin-arm64 ]; then
    shared=(-bundle -undefined dynamic_lookup)
  fi
  "${CC:-gcc}" "${shared[@]}" -O2 -g -Wall -I "$include" \
    "$ROOT/src/seam/_target/_seam_trap.c" -o "$dest/$HELPER"
fi
[ "$dest" -ef "$ROOT/vscode/bundled" ] || cp "$ROOT/vscode/bundled/__main__.py" "$dest/__main__.py"

# Listing-only extension updates can carry an unchanged released adapter.
# Its explicit version must still match the package we put inside the VSIX.
python3 - "$dest" "$ROOT/vscode/package.json" <<'EOF'
import json, re, sys
dest, manifest = sys.argv[1:]
with open(dest + "/seam/__init__.py") as fh:
    bundled = re.search(r'^__version__ = "([^"]+)"', fh.read(), re.MULTILINE).group(1)
with open(manifest) as fh:
    metadata = json.load(fh)
expected = metadata.get("seamAdapterVersion", metadata["version"])
if bundled != expected:
    sys.exit("build-vsix: the adapter is version %s, the extension expects adapter %s"
             % (bundled, expected))
EOF

# What the helper asks of the machine it will run on.
if [ "$target" = darwin-arm64 ]; then
  file "$dest/$HELPER" | grep -q 'Mach-O' || fail "the helper is not a Mach-O library"
  lipo "$dest/$HELPER" -verify_arch arm64 || fail "the helper has no Apple Silicon code"
elif [ "$target" = linux-x64 ] && command -v objdump >/dev/null; then
  objdump -f "$dest/$HELPER" | grep -q 'elf64-x86-64' ||
    fail "the helper is not a Linux x86-64 library: $(objdump -f "$dest/$HELPER" | grep 'file format')"
  glibc="$(objdump -T "$dest/$HELPER" | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1)"
  echo "helper: $HELPER needs glibc ${glibc#GLIBC_} or newer"
fi

if [ -n "$stage_only" ]; then
  echo "staged: $dest"
  exit 0
fi

cd "$ROOT/vscode"
cp ../LICENSE LICENSE.txt
rm -f ./*.vsix
npx --yes @vscode/vsce package --target "$target" --allow-missing-repository \
  --baseContentUrl https://github.com/ChirayuAgg0706/Seam/blob/main/vscode/ \
  --baseImagesUrl https://raw.githubusercontent.com/ChirayuAgg0706/Seam/main/vscode/
vsix="$(ls -1 ./*.vsix)"
# The adapter really is inside, and the editor check is not.
contents="$(python3 -m zipfile -l "$vsix")"
for wanted in extension/bundled/__main__.py extension/bundled/seam/cli.py "extension/bundled/$HELPER" \
              extension/lib/adapter.js; do
  grep -q "^$wanted " <<<"$contents" || fail "$vsix does not contain $wanted"
done
if grep -q 'extension/test/\|__pycache__' <<<"$contents"; then
  fail "$vsix contains files that should not ship"
fi
echo "$vsix"
