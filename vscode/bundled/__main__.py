"""Seam as it travels inside the VS Code extension.

The extension runs this directory, `python -I <extension>/bundled dap`. Python then puts
the directory on sys.path, so the `seam` package that scripts/build-vsix.sh lays out next
to this file is importable with nothing set in the environment: whatever was set there
would be inherited by LLDB and by the program being debugged.
"""
from seam.cli import main

main()
