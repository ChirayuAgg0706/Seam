import hashlib
import os
import shutil
import subprocess
import sys
import sysconfig
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dapclient import ROOT, DapClient  # noqa: E402

TARGETS = os.path.join(ROOT, "tests", "targets")
SHARED_FLAGS = (["-bundle", "-undefined", "dynamic_lookup"] if sys.platform == "darwin"
                else ["-shared", "-fPIC"])
HELPER_SRC = os.path.join(ROOT, "src", "seam", "_target", "_seam_trap.c")
HELPER_SO = os.path.join(ROOT, "src", "seam", "_target", "_seam_trap.abi3.so")
CAPI_SRC = os.path.join(ROOT, "tests", "ext", "capi", "seamtest.c")
BUILD = os.environ.get("SEAM_TEST_BUILD", os.path.join(ROOT, "tests", "build"))


def pytest_addoption(parser):
    parser.addoption("--target-python",
                     default=os.environ.get("SEAM_TEST_PYTHON", "/usr/bin/python3.12"),
                     help="interpreter to debug")
    parser.addoption("--opt", default=os.environ.get("SEAM_TEST_OPT", "O0"),
                     choices=["O0", "O2"], help="optimisation level of the native test code")
    parser.addoption("--repeat", type=int, default=int(os.environ.get("SEAM_TEST_REPEAT", "1")),
                     help="run each stepping scenario this many times")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A failed scenario's report carries the end of the adapter's log.

    Most failures are plain assertions about what the adapter said; why it said it is in
    the log, and on CI the report is all there is.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        client = getattr(item, "funcargs", {}).get("dap")
        log_path = getattr(client, "log_path", None)
        if log_path and os.path.exists(log_path):
            with open(log_path, errors="replace") as fh:
                lines = [line[:300] for line in fh.readlines()[-70:]]
            report.sections.append(("adapter log (last 70 lines)", "".join(lines)))


def pytest_generate_tests(metafunc):
    if "iteration" in metafunc.fixturenames:
        metafunc.parametrize("iteration", range(metafunc.config.getoption("--repeat")))


@pytest.fixture(scope="session", autouse=True)
def helper():
    """Build the in-process helper if it is missing or older than its source."""
    if (not os.path.exists(HELPER_SO)
            or os.path.getmtime(HELPER_SO) < os.path.getmtime(HELPER_SRC)):
        subprocess.run(
            ["gcc", *SHARED_FLAGS, "-O2", "-g", "-Wall", "-Werror",
             "-I", sysconfig.get_paths()["include"], HELPER_SRC, "-o", HELPER_SO],
            check=True)
    return HELPER_SO


@pytest.fixture(scope="session")
def python(request):
    return request.config.getoption("--target-python")


def marker_line(path, marker):
    """Line number of the line ending in `# <marker>`, `/* <marker> */` or `// <marker>`."""
    endings = ("# " + marker, "/* %s */" % marker, "// " + marker)
    with open(path) as fh:
        for number, text in enumerate(fh, 1):
            if text.rstrip().endswith(endings):
                return number
    raise AssertionError("marker %r not found in %s" % (marker, path))


def at_line(ext, actual, *expected):
    """Exact native line check at -O0 only.

    With -O2 the compiler merges and reorders lines (a breakpoint on a statement whose
    variable was optimised away moves to the next line with code, a function's first
    stop is its opening line, and so on), so those cells assert the function, not the line.
    """
    return ext.opt != "O0" or actual in expected


class Extension:
    def __init__(self, directory, opt, source=None, module=None, layer="capi"):
        self.dir = directory
        self.opt = opt
        self.env = {"PYTHONPATH": directory}
        self.source = source
        self.module = module
        self.layer = layer


EXT = os.path.join(ROOT, "tests", "ext")
LAYERS = {
    # layer: (module name, source file)
    "capi": ("seamtest", CAPI_SRC),
    "pybind11": ("seam_pybind11", os.path.join(EXT, "pybind11", "seam_pybind11.cpp")),
    "nanobind": ("seam_nanobind", os.path.join(EXT, "nanobind", "seam_nanobind.cpp")),
    "cython": ("seam_cython", os.path.join(EXT, "cython", "seam_cython.pyx")),
    "pyo3": ("seam_pyo3", os.path.join(EXT, "pyo3", "src", "lib.rs")),
}
SELECTED_LAYERS = [name for name in os.environ.get("SEAM_TEST_LAYERS", ",".join(LAYERS)).split(",")
                   if name]
STRICT = os.environ.get("SEAM_TEST_STRICT") == "1"


def _unavailable(reason):
    """A toolchain is missing: skip locally, fail where the full matrix is required."""
    if STRICT:
        pytest.fail(reason)
    pytest.skip(reason)


def _stale(out, *sources):
    return not os.path.exists(out) or any(
        os.path.getmtime(out) < os.path.getmtime(s) for s in sources)


def _run(cmd, **kw):
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        raise RuntimeError("build failed: %s\n%s\n%s" % (" ".join(cmd), proc.stdout[-3000:],
                                                         proc.stderr[-3000:]))


@pytest.fixture(scope="session")
def pyinfo(python):
    """Header directory and version tag of the interpreter under test."""
    out = subprocess.run(
        [python, "-c", "import sys, sysconfig; print(sysconfig.get_paths()['include']); "
                       "print('cp%d%d' % sys.version_info[:2])"],
        capture_output=True, text=True, check=True).stdout.split()
    return {"include": out[0], "tag": out[1]}


def _build_layer(layer, opt, pyinfo):
    module, source = LAYERS[layer]
    out_dir = os.path.join(BUILD, "%s-%s-%s" % (layer, opt, pyinfo["tag"]))
    out = os.path.join(out_dir, module + ".so")
    common = [*SHARED_FLAGS, "-g", "-" + opt, "-I", pyinfo["include"]]
    if layer == "pyo3":
        if not shutil.which("cargo"):
            _unavailable("cargo is not installed")
        # Cargo may reuse a copied crate's output across checkouts, including the old
        # absolute DWARF source paths. Keep the default cache local to this checkout.
        checkout = hashlib.sha1(ROOT.encode()).hexdigest()[:12]
        target_dir = os.environ.get(
            "CARGO_TARGET_DIR", os.path.expanduser("~/.cache/seam/cargo-target/" + checkout))
        profile = "debug" if opt == "O0" else "release"
        cmd = ["cargo", "build", "--quiet"] + (["--release"] if opt != "O0" else [])
        _run(cmd, cwd=os.path.dirname(os.path.dirname(source)),
             env=dict(os.environ, CARGO_TARGET_DIR=target_dir, PYO3_NO_PYTHON="1"))
        os.makedirs(out_dir, exist_ok=True)
        suffix = ".dylib" if sys.platform == "darwin" else ".so"
        shutil.copy2(os.path.join(target_dir, profile, "libseam_pyo3" + suffix), out)
        return Extension(out_dir, opt, source, module, layer)
    if not _stale(out, source):
        return Extension(out_dir, opt, source, module, layer)
    os.makedirs(out_dir, exist_ok=True)
    if layer == "pybind11":
        try:
            import pybind11
        except ImportError:
            _unavailable("pybind11 is not installed in the test environment")
        _run(["g++", "-std=c++17", "-fvisibility=hidden", *common, "-I", pybind11.get_include(),
              source, "-o", out])
    elif layer == "nanobind":
        try:
            import nanobind
        except ImportError:
            _unavailable("nanobind is not installed in the test environment")
        base = os.path.dirname(nanobind.__file__)
        _run(["g++", "-std=c++17", "-fvisibility=hidden", *common,
              "-I", os.path.join(base, "include"),
              "-I", os.path.join(base, "ext", "robin_map", "include"),
              source, os.path.join(base, "src", "nb_combined.cpp"), "-o", out])
    elif layer == "cython":
        try:
            import Cython  # noqa: F401
        except ImportError:
            _unavailable("Cython is not installed in the test environment")
        c_file = os.path.join(out_dir, module + ".c")
        cwd = os.path.dirname(source)
        # --line-directives makes the debug info point at the .pyx file.
        _run([sys.executable, "-m", "cython", "-3", "--line-directives",
              os.path.basename(source), "-o", c_file], cwd=cwd)
        _run(["gcc", *common, c_file, "-o", out], cwd=cwd)
    else:
        _run(["gcc", "-Wall", *common, source, "-o", out])
    return Extension(out_dir, opt, source, module, layer)


@pytest.fixture(scope="session")
def build_layer(request, pyinfo):
    cache = {}

    def build(layer):
        if layer not in cache:
            cache[layer] = _build_layer(layer, request.config.getoption("--opt"), pyinfo)
        return cache[layer]
    return build


@pytest.fixture(params=SELECTED_LAYERS)
def binding(request, build_layer):
    """One extension per binding layer, exposing add(a, b) and call_back(fn, x)."""
    return build_layer(request.param)


@pytest.fixture(scope="session")
def capi(request):
    """The plain C-API test extension, built at the requested optimisation level."""
    opt = request.config.getoption("--opt")
    out_dir = os.path.join(BUILD, "capi-" + opt)
    out = os.path.join(out_dir, "seamtest.abi3.so")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(CAPI_SRC):
        os.makedirs(out_dir, exist_ok=True)
        subprocess.run(
            ["gcc", *SHARED_FLAGS, "-g", "-" + opt, "-Wall",
             "-I", sysconfig.get_paths()["include"], CAPI_SRC, "-o", out], check=True)
    return Extension(out_dir, opt)


@pytest.fixture(scope="session")
def wheels_python(python, pyinfo):
    """The interpreter under test, in a virtual environment with numpy and orjson.

    Real wheels from PyPI: optimised, stripped of debug info, with hand-written assembly
    inside. Also the commonest real setup: debugging a virtual environment's `python`.
    """
    if not shutil.which("uv"):
        _unavailable("uv is not installed")
    label = "%s-%s" % (pyinfo["tag"], hashlib.sha1(python.encode()).hexdigest()[:8])
    venv = os.path.join(os.path.expanduser("~/.cache/seam/wheels"), label)
    exe = os.path.join(venv, "bin", "python")
    ready = os.path.join(venv, ".seam-ready")
    if not os.path.exists(ready):
        _run(["uv", "venv", "--quiet", "--python", python, venv])
        _run(["uv", "pip", "install", "--quiet", "--only-binary", ":all:", "--python", exe,
              "numpy", "orjson"])
        with open(ready, "w"):
            pass
    return exe


def target(name):
    return os.path.join(TARGETS, name)


def pid_alive(pid):
    if sys.platform == "darwin":
        state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                               capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")
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


@pytest.fixture
def make_client(tmp_path, python):
    """Factory for tests that need several sessions (each one is closed by the test)."""
    counter = [0]

    def make(**options):
        counter[0] += 1
        client = DapClient(log_path=str(tmp_path / ("seam-%d.log" % counter[0])), **options)
        client.python = python
        return client
    return make


def _children(pid):
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]
