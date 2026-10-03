"""Entry point loaded by `lldb -o "command script import .../lldb_entry.py"`."""
import os
import sys


def __lldb_init_module(debugger, internal_dict):
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if src not in sys.path:
        sys.path.insert(0, src)
    from seam.adapter import server

    server.serve(debugger)
