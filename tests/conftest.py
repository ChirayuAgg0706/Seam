import os
import subprocess
import sys
import sysconfig
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dapclient import ROOT, DapClient  # noqa: E402

TARGETS = os.path.join(ROOT, "tests", "targets")
HELPER_SRC = os.path.join(ROOT, "src", "seam", "_target", "_seam_trap.c")
HELPER_SO = os.path.join(ROOT, "src", "seam", "_target", "_seam_trap.abi3.so")


def pytest_addoption(parser):
    parser.addoption("--target-python", default=os.environ.get("SEAM_TEST_PYTHON", "/usr/bin/python3.12"),
                     help="interpreter to debug")
    parser.addoption("--repeat", type=int, default=int(os.environ.get("SEAM_TEST_REPEAT", "1")),
                     help="run each stepping scenario this many times")


def pytest_generate_tests(metafunc):
    if "iteration" in metafunc.fixturenames:
        metafunc.parametrize("iteration", range(metafunc.config.getoption("--repeat")))


@pytest.fixture(scope="session", autouse=True)
def helper():
    """Build the in-process helper if it is missing or older than its source."""
    if (not os.path.exists(HELPER_SO)
            or os.path.getmtime(HELPER_SO) < os.path.getmtime(HELPER_SRC)):
        subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2", "-g", "-Wall", "-Werror",
             "-I", sysconfig.get_paths()["include"], HELPER_SRC, "-o", HELPER_SO],
            check=True)
    return HELPER_SO


@pytest.fixture(scope="session")
def python(request):
    return request.config.getoption("--target-python")


def marker_line(path, marker):
    """Line number of the line carrying `# <marker>` in `path`."""
    with open(path) as fh:
        for number, text in enumerate(fh, 1):
            if text.rstrip().endswith("# " + marker):
                return number
    raise AssertionError("marker %r not found in %s" % (marker, path))


def target(name):
    return os.path.join(TARGETS, name)


def pid_alive(pid):
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@pytest.fixture
def dap(tmp_path, python):
    """A connected client. Teardown checks that nothing is left behind."""
    client = DapClient(log_path=str(tmp_path / "seam.log"))
    client.python = python
    yield client
    lldb_children = _children(client.proc.pid)
    client.close()
    deadline = time.monotonic() + 10
    leftovers = [client.target_pid] if client.target_pid else []
    leftovers += lldb_children
    while time.monotonic() < deadline and any(pid_alive(p) for p in leftovers):
        time.sleep(0.05)
    alive = [p for p in leftovers if pid_alive(p)]
    assert not alive, "processes left behind after the session: %s" % alive


def _children(pid):
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]
