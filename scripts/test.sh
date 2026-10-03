#!/usr/bin/env bash
# Run the test suite. Usage: scripts/test.sh [pytest args...]
# The virtual environment lives on the Linux filesystem (the repo may be on a slow mount).
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${SEAM_VENV:-$HOME/.cache/seam/venv}"
if [ ! -x "$VENV/bin/python" ]; then
  uv venv --quiet --python "${SEAM_VENV_PYTHON:-/usr/bin/python3.12}" "$VENV"
fi
# pytest runs the suite; the other packages build the binding-layer test extensions.
if ! "$VENV/bin/python" -c "import pytest, pybind11, nanobind, Cython" 2>/dev/null; then
  uv pip install --quiet --python "$VENV/bin/python" "pytest>=8" pybind11 nanobind cython
fi
cd "$ROOT"
exec "$VENV/bin/python" -m pytest "$@"
