"""Clean-machine check: debug the PyO3 demo with the *installed* `seam`.

Usage: python tools/check_demo.py [python-to-debug]

Drives `seam dap` (found on PATH) through a real session against examples/pyo3-demo and
exits non-zero if anything is wrong. Used by scripts/clean-machine-check.sh.
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from dapclient import DapClient  # noqa: E402

DEMO = os.path.join(ROOT, "examples", "pyo3-demo")
SCRIPT = os.path.join(DEMO, "demo.py")
RUST = os.path.join(DEMO, "src", "lib.rs")


def line_of(path, text):
    with open(path) as fh:
        for number, line in enumerate(fh, 1):
            if text in line:
                return number
    raise SystemExit("marker %r not found in %s" % (text, path))


def check(condition, message):
    if not condition:
        raise SystemExit("FAILED: " + message)
    print("ok:", message)


def main():
    python = sys.argv[1] if len(sys.argv) > 1 else sys.executable
    seam = shutil.which("seam")
    if not seam:
        raise SystemExit("`seam` is not on PATH")
    if not os.path.exists(os.path.join(DEMO, "seam_demo.so")):
        raise SystemExit("build the demo first (see examples/pyo3-demo/README.md)")
    py_line = line_of(SCRIPT, "# step in here")
    rust_line = line_of(RUST, "// break here")

    dap = DapClient(log_path=os.environ.get("SEAM_LOG"), command=[seam, "dap"])
    try:
        dap.launch(SCRIPT, python, env={"PYTHONPATH": DEMO}, breakpoints={SCRIPT: [py_line]})
        tid = dap.wait_stopped()["threadId"]
        top = dap.stack(tid)[0]
        check((top["name"], top["line"]) == ("report", py_line), "Python breakpoint hit")
        check(dap.evaluate("label", top["id"])["result"] == "'squares'",
              "Python expression evaluated at a Python stop")

        dap.step("stepIn", tid)
        stack = dap.stack(tid)
        check(stack[0].get("source", {}).get("path") == RUST and "sum_squares" in stack[0]["name"],
              "step in from Python landed in the Rust function: %s" % stack[0]["name"])
        names = [f["name"] for f in stack]
        check("report" in names and names[-1] == "<module>",
              "merged stack shows Python frames below Rust: %s" % names)

        dap.set_breakpoints(RUST, [rust_line])
        dap.cont()
        tid = dap.wait_stopped()["threadId"]
        stack = dap.stack(tid)
        check(stack[0]["line"] == rust_line, "Rust breakpoint hit")
        rust_locals = dap.scope(stack[0]["id"])
        check("total" in rust_locals and "n" in rust_locals,
              "Rust locals visible: %s" % sorted(rust_locals))
        report = next(f for f in stack if f["name"] == "report")
        check(dap.scope(report["id"])["label"]["value"] == "'squares'",
              "Python locals readable while stopped in Rust")
        refused = dap.evaluate("label", report["id"], check=False)
        check(not refused["success"], "Python evaluation refused at a native stop")

        dap.set_breakpoints(RUST, [])
        dap.set_breakpoints(SCRIPT, [])
        dap.step("stepOut", tid)
        top = dap.stack(tid)[0]
        check((top["name"], top["line"]) == ("report", py_line),
              "step out of Rust returned to the calling Python line")
        dap.step("next", tid)
        top = dap.stack(tid)[0]
        check(dap.evaluate("result", top["id"])["result"] == "30", "result is 30 after the call")
        dap.cont()
        check(dap.wait_exit() == 0, "program exited normally")
        check("squares(5) = 30" in dap.output, "program output captured")
    finally:
        dap.close()
    print("clean-machine check passed")


if __name__ == "__main__":
    main()
