"""Entry point loaded by `lldb -o "command script import .../lldb_entry.py"`."""
import os
import sys
import traceback

ADAPTER_FAILED = 70  # exit status telling `seam dap` that the adapter itself broke


def __lldb_init_module(debugger, internal_dict):
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from seam.adapter import server

        server.serve(debugger)
    except BaseException:
        # LLDB would report this as a failed import and still exit with status 0.
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(ADAPTER_FAILED)
