#!/usr/bin/env bash
# Build real open-source extension projects from source, with debug info, for the opt-in
# scenarios of tests/test_projects.py.
#
#   tools/build_projects.sh [regex] [msgpack] [contourpy] [pydantic_core] [numpy]
#
# Everything lives under ${SEAM_SCALE_DIR:-~/.cache/seam/scale}:
#   src/<project>   the sdist from PyPI, unpacked; built in place, so the debug info of
#                   the modules points at these files
#   venv            a virtual environment of uv's CPython ${SEAM_PROJECTS_PYTHON:-3.12}
#                   that the projects are installed into
#
# Each project keeps its normal optimisation level and gets debug info on top. Builds use
# at most ${JOBS:-4} jobs. Needs uv, gcc/g++ and, for pydantic_core, a Rust toolchain; the
# other build tools (Cython, maturin, meson, ninja, pybind11) are installed into the venv.
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
ROOT="${SEAM_SCALE_DIR:-$HOME/.cache/seam/scale}"
VENV="$ROOT/venv"
SRC="$ROOT/src"
JOBS="${JOBS:-4}"
PY="$VENV/bin/python"
mkdir -p "$SRC"

if [ ! -x "$PY" ]; then
  uv venv --quiet --seed --python "${SEAM_PROJECTS_PYTHON:-3.12}" \
    --python-preference only-managed "$VENV"
fi
uv pip install --quiet --python "$PY" \
  setuptools wheel cython maturin meson meson-python ninja pybind11
export PATH="$VENV/bin:$PATH"

# fetch NAME VERSION: download the sdist from PyPI and unpack it as $SRC/NAME (once).
fetch() {
  local name=$1 version=$2 tmp
  [ -d "$SRC/$name" ] && return
  tmp=$(mktemp -d "$ROOT/download.XXXXXX")
  "$PY" - "$name" "$version" "$tmp/sdist.tar.gz" <<'EOF'
import json
import sys
import urllib.request

name, version, out = sys.argv[1:]
with urllib.request.urlopen("https://pypi.org/pypi/%s/%s/json" % (name, version)) as fh:
    files = json.load(fh)["urls"]
urllib.request.urlretrieve(next(f["url"] for f in files if f["packagetype"] == "sdist"), out)
EOF
  mkdir "$tmp/unpacked"
  tar -xf "$tmp/sdist.tar.gz" -C "$tmp/unpacked"
  mv "$tmp"/unpacked/* "$SRC/$name"
  rm -rf "$tmp"
}

# pip builds a source directory in place. setuptools appends CFLAGS to the interpreter's
# own flags, so the optimisation level stays what it was and -g is added.
build_regex() {
  fetch regex 2024.11.6
  (cd "$SRC/regex" && CFLAGS="-g" "$PY" -m pip install --quiet --no-deps --no-build-isolation .)
}

build_msgpack() {
  fetch msgpack 1.1.0
  (cd "$SRC/msgpack" && CFLAGS="-g" "$PY" -m pip install --quiet --no-deps --no-build-isolation .)
}

# meson-python: release optimisation with debug info, in a build directory that stays
# (the debug info's paths are relative to it).
build_contourpy() {
  fetch contourpy 1.3.1
  uv pip install --quiet --python "$PY" numpy
  (cd "$SRC/contourpy" && "$PY" -m pip install --quiet --no-deps --no-build-isolation \
      -Cbuilddir=build -Csetup-args=-Ddebug=true -Ccompile-args=-j"$JOBS" .)
}

# The release profile of pydantic-core strips the module and uses fat LTO in one codegen
# unit, which needs more memory than a small machine can spare once debug info is on.
# Debug info is kept and optimisation stays at the project's level (opt-level 3); LTO is
# switched off.
build_pydantic_core() {
  fetch pydantic_core 2.27.2
  uv pip install --quiet --python "$PY" typing-extensions
  (cd "$SRC/pydantic_core" && CARGO_BUILD_JOBS="$JOBS" \
      CARGO_PROFILE_RELEASE_DEBUG=true CARGO_PROFILE_RELEASE_STRIP=false \
      CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
      VIRTUAL_ENV="$VENV" maturin develop --release --quiet)
}

build_numpy() {
  fetch numpy 2.2.1
  (cd "$SRC/numpy" && "$PY" -m pip install --quiet --no-deps --no-build-isolation \
      -Cbuilddir=build -Csetup-args=-Ddebug=true -Csetup-args=-Dallow-noblas=true \
      -Ccompile-args=-j"$JOBS" .)
}

for name in ${@:-regex msgpack contourpy pydantic_core}; do
  echo "== $name"
  start=$SECONDS
  "build_$name"
  echo "$name: built in $((SECONDS - start)) s"
done
