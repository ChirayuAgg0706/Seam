"""The adapter as the VS Code extension carries it.

The extension holds a copy of the seam package, laid out by scripts/build-vsix.sh, and
starts it as `python -I <extension>/bundled dap` with nothing added to the environment
(vscode/lib/adapter.js). These scenarios run that layout through that command line: out
of a .vsix if SEAM_VSIX names one, otherwise laid out from this source tree.
"""
import ast
import os
import subprocess
import zipfile

import pytest

from conftest import ROOT, marker_line, target
from dapclient import DapClient, Terminal
from test_robust import descendants

BASIC = target("basic.py")
INTERACTIVE = target("interactive.py")


@pytest.fixture(scope="session")
def bundle(tmp_path_factory):
    """The directory the extension runs: `bundled`, with the seam package inside."""
    out = tmp_path_factory.mktemp("extension")
    vsix = os.environ.get("SEAM_VSIX")
    if vsix:
        with zipfile.ZipFile(vsix) as archive:
            archive.extractall(out)
        return str(out / "extension" / "bundled")
    staged = subprocess.run(
        ["bash", os.path.join(ROOT, "scripts", "build-vsix.sh"), "--stage", str(out / "bundled")],
        capture_output=True, text=True)
    assert staged.returncode == 0, staged.stdout + staged.stderr
    return str(out / "bundled")


@pytest.fixture
def bundled(bundle, tmp_path, python, monkeypatch):
    """Start the bundled adapter the way the extension does. Returns a client factory."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    clients = []

    def start(launcher=python):
        client = DapClient(log_path=str(tmp_path / ("seam-%d.log" % len(clients))),
                           command=[launcher, "-I", bundle, "dap"])
        client.python = python
        clients.append(client)
        return client
    yield start
    for client in clients:
        client.close()


def environment(pid):
    with open("/proc/%d/environ" % pid, "rb") as fh:
        return dict(entry.split("=", 1) for entry in fh.read().decode(errors="replace").split("\0")
                    if "=" in entry)


def test_bundle_debugs_a_virtual_environment(bundled, bundle, python, tmp_path):
    # The first-run story: a project with a virtual environment, whose interpreter both
    # runs the adapter and is debugged; no Seam installed anywhere else.
    venv = tmp_path / "project" / ".venv"
    subprocess.run([python, "-m", "venv", "--without-pip", str(venv)], check=True)
    venv_python = str(venv / "bin" / "python")
    line = marker_line(BASIC, "inner-first")
    client = bundled(launcher=venv_python)
    client.launch(BASIC, venv_python, breakpoints={BASIC: [line]})
    stop = client.wait_stopped()
    frame = client.stack(stop["threadId"])[0]
    assert (frame["name"], frame["line"]) == ("inner", line)

    def value(expression):
        return ast.literal_eval(client.evaluate(expression, frame["id"])["result"])

    assert value("__import__('sys').prefix") == str(venv)
    assert value("__import__('sys').executable") == venv_python
    # The helper in the program is the one the extension carries, and it got there
    # without the bundle being on anybody's module path.
    helper = value("__import__('sys').modules['_seam_trap'].__file__")
    assert helper == os.path.join(bundle, "seam", "_target", "_seam_trap.abi3.so")
    assert value("__import__('os').environ.get('PYTHONPATH')") is None
    assert not [entry for entry in value("__import__('sys').path") if entry.startswith(bundle)]
    for pid in descendants(client.proc.pid):   # LLDB, its debug server, the program
        assert "PYTHONPATH" not in environment(pid), pid
    client.set_breakpoints(BASIC, [])
    client.cont()
    assert client.wait_exit() == 0
    assert "result 33" in client.output


def test_bundle_runs_the_program_in_a_terminal(bundled, bundle, python):
    client = bundled()
    client.terminal = Terminal()
    client.launch(INTERACTIVE, python, console="integratedTerminal")
    client.terminal.read_until("name? ")
    # What the editor was asked to run in its terminal: the holder inside the extension,
    # with the interpreter that runs the adapter.
    assert client.terminal.proc.args[:2] == [python, os.path.join(bundle, "seam", "terminal.py")]
    client.terminal.type("seam\n")
    assert client.wait_exit() == 0
    client.terminal.read_until("hello seam")


def test_bundle_checks_the_machine(bundle, python, monkeypatch):
    # What "Seam: Check This Machine" types into a terminal: `seam doctor`, run out of
    # the extension, whose live check has to start the adapter out of the extension too.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    done = subprocess.run([python, "-I", bundle, "doctor", "--python", python],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "debug session: launched a program, stopped at a breakpoint" in done.stdout
    assert done.stdout.rstrip().endswith("Seam is ready to use.")


def test_bundle_without_lldb_says_so(bundle, python):
    done = subprocess.run([python, "-I", bundle, "dap"], capture_output=True, text=True,
                          env=dict(os.environ, SEAM_LLDB="/no/such/lldb"),
                          stdin=subprocess.DEVNULL, timeout=60)
    assert done.returncode == 1
    assert "LLDB 18 or newer is required" in done.stderr
