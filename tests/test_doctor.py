"""`seam doctor`, and what `seam dap` says when it cannot start at all."""
import os
import subprocess
import sys

import pytest

from conftest import ROOT
from test_unsupported import uv_python


@pytest.mark.parametrize("available, override, expected", [
    (("lldb", "lldb-19", "lldb-20"), None, "lldb-20"),
    (("lldb", "lldb-19"), None, "lldb-19"),
    (("lldb",), None, "lldb"),
    (("lldb-18",), None, "lldb-18"),
    (("lldb-21",), None, "lldb-21"),
    ((), None, None),
    (("lldb", "lldb-20"), "lldb", "lldb"),
    (("/custom/lldb", "lldb-20"), "/custom/lldb", "/custom/lldb"),
    (("lldb-20",), "/missing/lldb", None),
])
def test_lldb_selection(monkeypatch, available, override, expected):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "src"))
    from seam import cli

    monkeypatch.delenv("SEAM_LLDB", raising=False)
    if override is not None:
        monkeypatch.setenv("SEAM_LLDB", override)
    monkeypatch.setattr(cli.shutil, "which", lambda name: name if name in available else None)
    assert cli.find_lldb() == expected


def test_doctor_warns_when_lldb_18_is_selected(monkeypatch, capsys):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "src"))
    from seam import doctor

    monkeypatch.setattr(doctor.cli, "find_lldb", lambda: "/usr/bin/lldb-18")
    monkeypatch.setattr(doctor, "_run", lambda cmd: (
        0, "lldb version 18.0.0" if "--version" in cmd else "seam-python 3 12"))
    assert doctor._check_lldb(doctor._Report())
    output = capsys.readouterr().out
    assert "/usr/bin/lldb-18, version 18.0.0" in output
    assert "lose track of the program" in output
    assert "apt install lldb-19" in output
    assert "SEAM_LLDB is set, it takes priority" in output


def seam(*args, **env):
    full = dict(os.environ, **env)
    full["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + full.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "seam", *args], capture_output=True,
                          text=True, env=full, stdin=subprocess.DEVNULL, timeout=180)


def test_doctor_passes_on_a_working_setup(python):
    done = seam("doctor", "--python", python)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PROBLEM" not in done.stdout
    platform_name = "macOS Apple Silicon" if sys.platform == "darwin" else "Linux x86-64"
    for expected in ("platform: " + platform_name, "LLDB: Python scripting works",
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
    assert "No `lldb` was found on PATH" in done.stderr


def test_dap_with_an_lldb_that_cannot_load_the_adapter(tmp_path):
    # What an LLDB without its Python bindings does: complain, and exit normally.
    fake = tmp_path / "lldb"
    fake.write_text("#!/bin/sh\necho 'error: no script interpreter for this language'\n")
    fake.chmod(0o755)
    done = seam("dap", SEAM_LLDB=str(fake))
    assert done.returncode == 1
    assert "LLDB did not load Seam's adapter" in done.stderr
    assert "no script interpreter" in done.stderr
