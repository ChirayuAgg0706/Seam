# Seam extension 0.1.2

Debug Python and C, C++ or Rust in one session on Linux x86-64, including WSL.

This update combines the Joined S icon, a recorded Python-to-Rust demo GIF,
and rewritten installation, debugging and troubleshooting instructions.
Settings descriptions and installation errors use the same plain wording.
The included Seam debugger is still 0.1.0.

![Step into Rust and return to Python](https://raw.githubusercontent.com/ChirayuAgg0706/Seam/main/vscode/images/python-rust-demo.gif)

## Install

Install LLDB with Python scripting support. On Ubuntu 24.04:

```bash
sudo apt-get update
sudo apt-get install -y lldb-19
```

Install [Seam from the Marketplace](https://marketplace.visualstudio.com/items?itemName=chirayuagg0706.seam-debugger)
in a Linux or WSL window. For this package, download the Linux x64 VSIX and run
**Extensions: Install from VSIX...**. Its companion `.sha256` records the checksum.

Run **Seam: Check This Machine**, open a Python file, set a breakpoint on a native
call, and press F5. Choose **Seam: Python + native** if prompted.
Step Into enters native code. Step Out returns to Python.

Use Linux x86-64 with glibc and CPython 3.12, 3.13 or 3.14. Python 3.15.0rc3
also passed the debugger's release tests; later 3.15 builds have not been
validated for this release. Build native modules with debug information for
source stepping. Read the [limitations](https://github.com/ChirayuAgg0706/Seam#limitations).

This file describes the prepared package. Marketplace availability requires
upload and validation; preparing it does not publish the update.
