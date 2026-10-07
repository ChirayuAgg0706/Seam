#!/usr/bin/env bash
# Clean-machine check: on a fresh Ubuntu 24.04, follow the README's source-install steps and
# debug a PyO3 project. Run inside a new container or as a new user:
#
#   docker run --rm --cap-add=SYS_PTRACE -v "$PWD":/seam -w /seam ubuntu:24.04 \
#       scripts/clean-machine-check.sh
#
# The only difference from the README is that Seam is installed from this checkout
# instead of being cloned first.
set -euo pipefail
SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo"
export DEBIAN_FRONTEND=noninteractive

echo "### README source install: install prerequisites"
$SUDO apt-get update
$SUDO apt-get install -y lldb-19 gcc python3-dev python3-venv git

echo "### README source install: install Seam into its own environment"
python3 -m venv "$HOME/.venvs/seam"
"$HOME/.venvs/seam/bin/pip" install .
export PATH="$HOME/.venvs/seam/bin:$PATH"
seam --version

echo "### README: check the installation"
seam doctor --python python3

echo "### Not part of Seam: a Rust toolchain for the demo project"
if ! command -v cargo >/dev/null; then
  $SUDO apt-get install -y curl
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  export PATH="$HOME/.cargo/bin:$PATH"
fi

echo "### examples/pyo3-demo/README.md: build the demo"
(cd examples/pyo3-demo && cargo build && cp target/debug/libseam_demo.so seam_demo.so)

echo "### Debug the demo with the installed seam"
python3 tools/check_demo.py /usr/bin/python3
