"""`seam doctor`, and what `seam dap` says when it cannot start at all."""
import os
import subprocess
import sys

from conftest import ROOT
from test_unsupported import uv_python


def seam(*args, **env):
    full = dict(os.environ, **env)
    full["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + full.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "seam", *args], capture_output=True,
                          text=True, env=full, stdin=subprocess.DEVNULL, timeout=180)


def test_doctor_passes_on_a_working_setup(python):
    done = seam("doctor", "--python", python)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PROBLEM" not in done.stdout
    for expected in ("platform: Linux x86-64", "LLDB: Python scripting works",
                     "helper: built", "debug session: launched a program",
                     "Seam is ready to use."):
        assert expected in done.stdout, done.stdout


def test_doctor_explains_a_missing_lldb(python):
    done = seam("doctor", "--python", python, SEAM_LLDB="/no/such/lldb")
    assert done.returncode == 1
    assert "PROBLEM  LLDB: not found" in done.stdout
    assert "debug session: not tried" in done.stdout
    assert "1 problem found." in done.stdout


def test_doctor_explains_an_unsupported_python():
    done = seam("doctor", "--python", uv_python("3.11"))
    assert done.returncode == 1
    assert "PROBLEM  Python to debug" in done.stdout and "3.11" in done.stdout
    assert "Seam needs CPython 3.12 or newer" in done.stdout


def test_doctor_explains_a_python_that_does_not_exist():
    done = seam("doctor", "--python", "/no/such/python")
    assert done.returncode == 1 and "/no/such/python not found" in done.stdout


def test_dap_without_lldb_says_so():
    done = seam("dap", SEAM_LLDB="/no/such/lldb")
    assert done.returncode == 1
    assert "LLDB 18 or newer is required" in done.stderr


def test_dap_with_an_lldb_that_cannot_load_the_adapter(tmp_path):
    # What an LLDB without its Python bindings does: complain, and exit normally.
    fake = tmp_path / "lldb"
    fake.write_text("#!/bin/sh\necho 'error: no script interpreter for this language'\n")
    fake.chmod(0o755)
    done = seam("dap", SEAM_LLDB=str(fake))
    assert done.returncode == 1
    assert "LLDB did not load Seam's adapter" in done.stderr
    assert "no script interpreter" in done.stderr
