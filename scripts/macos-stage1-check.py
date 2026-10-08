"""Test the installed wheel on Apple Silicon, without importing Seam from the checkout."""
import argparse
import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from dapclient import DapClient  # noqa: E402


def check(output, repeat):
    import seam
    import seam._target._seam_trap as helper

    assert platform.system() == "Darwin", platform.platform()
    assert platform.machine() == "arm64", platform.machine()
    checkout = Path(__file__).resolve().parents[1]
    assert not Path(seam.__file__).is_relative_to(checkout / "src"), seam.__file__
    output.mkdir(parents=True, exist_ok=True)
    target = output.resolve() / "stage1_target.py"
    target.write_text(
        'def calculate():\n'
        '    number = 7\n'
        '    label = "apple silicon"\n'
        '    result = number * 6\n'
        '    print(label, result, flush=True)\n'
        '    return result\n'
        '\n'
        'assert calculate() == 42\n', encoding="utf-8")
    report = {"platform": platform.platform(), "python": sys.version,
              "installed_package": seam.__file__, "helper": helper.__file__, "runs": []}
    try:
        for index in range(repeat):
            start = time.monotonic()
            with (output / ("run-%d.stderr" % index)).open("w") as stderr:
                client = DapClient(command=[sys.executable, "-m", "seam", "dap"],
                                   log_path=str(output.resolve() / ("run-%d.log" % index)),
                                   stderr=stderr)
                try:
                    client.launch(str(target), sys.executable,
                                  breakpoints={str(target): [4]}, debugInfoLookup=False)
                    stopped = client.wait_stopped()
                    elapsed = time.monotonic() - start
                    assert stopped["reason"] == "breakpoint", stopped
                    frames = client.stack(stopped["threadId"])
                    frame = next(f for f in frames if f["name"] == "calculate")
                    assert frame["line"] == 4, frames
                    assert Path(frame["source"]["path"]).resolve() == target, frame
                    variables = client.scope(frame["id"])
                    assert variables["number"]["value"] == "7", variables
                    assert "apple silicon" in variables["label"]["value"], variables
                    evaluated = client.evaluate("number + 35", frame["id"])
                    assert evaluated["result"] == "42", evaluated
                    status = client.status()
                    stepped = client.step("next", stopped["threadId"])
                    frames = client.stack(stepped["threadId"])
                    frame = next(f for f in frames if f["name"] == "calculate")
                    assert frame["line"] == 5, frames
                    assert client.scope(frame["id"])["result"]["value"] == "42"
                    client.cont(stepped["threadId"])
                    assert client.wait_exit() == 0
                    client.wait_event("terminated")
                    assert "apple silicon 42" in client.plain_output, client.plain_output
                    report["runs"].append({"index": index, "launch_to_breakpoint_seconds": elapsed,
                                           "status": status, "passed": True})
                    print("Run %d passed, breakpoint in %.2fs" % (index + 1, elapsed), flush=True)
                finally:
                    client.close()
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    check(args.output, args.repeat)
