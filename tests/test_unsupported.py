"""Interpreters Seam does not support are refused with a clear message, and cleanly."""
import shutil
import subprocess

import pytest

from conftest import _unavailable, target
from test_robust import descendants, survivors

BASIC = target("basic.py")


def uv_python(request):
    """Path of a uv-managed interpreter, e.g. "3.11" or "3.13t" (free-threaded)."""
    if not shutil.which("uv"):
        _unavailable("uv is not installed")
    found = subprocess.run(
        ["uv", "python", "find", "--python-preference", "only-managed", request],
        capture_output=True, text=True)
    if found.returncode != 0:
        _unavailable("no uv-managed Python %s (uv python install %s)" % (request, request))
    return found.stdout.strip()


@pytest.mark.parametrize("request_, expected", [
    ("3.11", "Seam needs CPython 3.12 or newer; this is 3.11"),
    ("3.13t", "free-threaded"),
])
def test_unsupported_interpreter_is_refused(make_client, request_, expected):
    python = uv_python(request_)
    client = make_client()
    client.request("initialize", {"adapterID": "seam"})
    reply = client.request("launch", {"program": BASIC, "python": python}, check=False)
    assert not reply["success"]
    assert expected in reply["message"], reply["message"]
    ours = descendants(client.proc.pid)
    client.close()
    assert survivors(ours) == []
