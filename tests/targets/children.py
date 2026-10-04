"""Starts child processes in the ways Python programs commonly do.

Every child reports back through its exit status and the parent prints what became of it
("exit 7", "signal SIGTRAP"), so a failing scenario says what happened to the child.
"""
import concurrent.futures
import ctypes
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time

import seamtest


def describe(status):
    """A wait status in words."""
    if os.WIFSIGNALED(status):
        return "signal " + signal.Signals(os.WTERMSIG(status)).name
    return "exit %d" % os.WEXITSTATUS(status)


def exit_code(code):
    """A `Process.exitcode` or `Popen.returncode` in the same words."""
    if code is not None and code < 0:
        return "signal " + signal.Signals(-code).name
    return "exit %s" % code


def helper():
    """Who holds the debugger's slot in sys.monitoring in this process ("seam" or None)."""
    return sys.monitoring.get_tool(sys.monitoring.DEBUGGER_ID)


def work(n):
    total = seamtest.add(n, 1)  # work-add
    return total * 2  # work-return


def child_main(tag):
    """What a forked child does: Python lines and native code that have breakpoints."""
    result = work(5)
    print("%s child: work %d, helper %s" % (tag, result, helper()), flush=True)
    os._exit(7 if result == 12 else 9)


def reap(tag, pid):
    status = os.waitpid(pid, 0)[1]
    print("%s child: %s" % (tag, describe(status)), flush=True)


def forking():
    before = work(1)
    pid = os.fork()  # fork-here
    if pid == 0:
        child_main("fork")
    reap("fork", pid)  # fork-after
    after = work(2)
    print("parent: work %d %d, helper %s" % (before, after, helper()), flush=True)


def native_fork():
    """A fork made by native code: Python's at-fork hooks do not run in the child."""
    before = work(1)
    pid = ctypes.CDLL(None).fork()
    if pid == 0:
        child_main("native fork")
    reap("native fork", pid)
    print("parent: work %d %d" % (before, work(2)), flush=True)


PYTHON_CHILD = (
    "import os, sys\n"
    "private = sorted(k for k in os.environ if k in ('SEAM_DAP_FD', 'SEAM_NOTE_FD', "
    "'SEAM_PYTHON'))\n"
    "seam = sorted(m for m in sys.modules if 'seam' in m)\n"
    "paths = [p for p in sys.path if 'seam' in p and '_target' in p]\n"
    "print('python child: helper %s, modules %s, env %s, path %s'\n"
    "      % (sys.monitoring.get_tool(sys.monitoring.DEBUGGER_ID), seam, private, paths),\n"
    "      flush=True)\n"
    "sys.exit(5)\n")


def spawning():
    before = work(1)
    done = subprocess.run([sys.executable, "-c", PYTHON_CHILD])  # spawn-python
    print("subprocess python:", exit_code(done.returncode), flush=True)
    done = subprocess.run(["/bin/true"])
    print("subprocess true:", exit_code(done.returncode), flush=True)
    done = subprocess.run(["ls", os.path.dirname(os.path.abspath(__file__))],
                          capture_output=True, text=True)
    print("subprocess ls:", exit_code(done.returncode), "children.py" in done.stdout,
          flush=True)
    print("system:", describe(os.system("exit 3")), flush=True)
    pid = os.posix_spawn("/bin/true", ["true"], os.environ)
    print("posix_spawn:", describe(os.waitpid(pid, 0)[1]), flush=True)
    with subprocess.Popen([sys.executable, "-c", "print(input().upper())"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as proc:
        answer = proc.communicate("piped\n")[0].strip()
    print("popen:", exit_code(proc.returncode), answer, flush=True)
    print("parent: work %d %d" % (before, work(2)), flush=True)


def pool(method):
    context = multiprocessing.get_context(method)
    before = work(1)
    with context.Pool(2) as workers:
        members = list(workers._pool)
        try:
            results = workers.map_async(work, range(6)).get(timeout=40)
        except multiprocessing.TimeoutError:
            results = "timed out; workers: %s" % [exit_code(p.exitcode) for p in members]
    print("pool %s: %s" % (method, results), flush=True)
    child = context.Process(target=work, args=(3,))
    child.start()
    child.join(40)
    print("process %s: %s" % (method, exit_code(child.exitcode)), flush=True)
    print("parent: work %d %d" % (before, work(2)), flush=True)


def executor():
    before = work(1)
    try:
        with concurrent.futures.ProcessPoolExecutor(2) as workers:
            results = list(workers.map(work, range(6), timeout=40))
    except Exception as exc:  # a worker that died breaks the pool
        results = "failed: %r" % exc
    print("executor: %s" % results, flush=True)
    print("parent: work %d %d" % (before, work(2)), flush=True)


def spin(stop, counts, index):
    while not stop.is_set():
        counts[index] += seamtest.add(0, 1)
        time.sleep(0.001)


def fork_with_threads():
    """Other threads are running Python and native code while the main thread forks."""
    stop = threading.Event()
    counts = [0, 0, 0]
    threads = [threading.Thread(target=spin, args=(stop, counts, i)) for i in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    for round_number in range(3):
        pid = os.fork()
        if pid == 0:
            child_main("threads %d" % round_number)
        reap("threads %d" % round_number, pid)
        done = subprocess.run(["/bin/true"])
        print("threads %d subprocess: %s" % (round_number, exit_code(done.returncode)),
              flush=True)
    stop.set()  # threads-stop
    for thread in threads:
        thread.join()
    print("parent: threads counted %s" % all(counts), flush=True)


def stepping():
    """Lines to step over that start children."""
    pid = os.fork()  # step-fork
    if pid == 0:
        child_main("step")
    reap("step", pid)  # step-reap
    done = subprocess.run(["/bin/true"])  # step-subprocess
    code = os.system("exit 3")  # step-system
    print("parent: stepped %s %s" % (exit_code(done.returncode), describe(code)),  # step-print
          flush=True)


def orphan(marker):
    """A child that outlives the program: it writes `marker` a moment after the parent left."""
    pid = os.fork()
    if pid == 0:
        result = work(5)
        time.sleep(1.5)
        with open(marker, "w") as fh:
            fh.write("orphan %d work %d helper %s\n" % (os.getpid(), result, helper()))
        os._exit(0)
    print("parent: leaving child %d behind" % pid, flush=True)


def lingering():
    """Children that are still alive when the debug session ends."""
    forked = os.fork()
    if forked == 0:
        time.sleep(600)
        os._exit(0)
    spawned = subprocess.Popen(["sleep", "600"])
    detached = subprocess.Popen(["sleep", "600"], start_new_session=True)
    print("children: %d %d %d" % (forked, spawned.pid, detached.pid), flush=True)
    time.sleep(600)  # linger-wait


def watched():
    """A native variable has a data breakpoint on it; a child changes its own copy."""
    first = seamtest.bump()  # watch-first
    pid = os.fork()
    if pid == 0:
        os._exit(40 + seamtest.bump())
    reap("watch", pid)
    second = seamtest.bump()
    print("parent: bumped %d %d" % (first, second), flush=True)


def fail(kind):
    if kind == "caught":
        try:
            raise KeyError("handled in the child")
        except KeyError:
            return
    raise ValueError("raised by " + kind)  # raise-here


def raising():
    """Exceptions in a child while the parent has exception breakpoints set."""
    pid = os.fork()
    if pid == 0:
        fail("caught")
        try:
            seamtest.fail()
        except ValueError:
            pass
        fail("the child")  # uncaught: Python prints it and the child exits with 1
    reap("raise", pid)
    worker = threading.Thread(target=fail, args=("a thread",))
    pid = os.fork()
    if pid == 0:
        worker.start()
        worker.join()
        os._exit(6)
    reap("raise thread", pid)
    try:
        fail("the parent")  # parent-raise
    except ValueError as exc:
        print("parent: caught %s" % exc, flush=True)


def logging_lines():
    """A logpoint on a line that the parent and a child both run."""
    for n in range(2):
        work(n)
    pid = os.fork()
    if pid == 0:
        for n in range(50):
            work(n)
        os._exit(7)
    reap("log", pid)
    work(10)
    print("parent: logged", flush=True)


def main():
    mode = sys.argv[1]
    if mode == "fork":
        forking()
    elif mode == "native-fork":
        native_fork()
    elif mode == "spawn":
        spawning()
    elif mode == "pool":
        pool(sys.argv[2])
    elif mode == "executor":
        executor()
    elif mode == "threads":
        fork_with_threads()
    elif mode == "step":
        stepping()
    elif mode == "orphan":
        orphan(sys.argv[2])
    elif mode == "linger":
        lingering()
    elif mode == "watch":
        watched()
    elif mode == "raise":
        raising()
    elif mode == "log":
        logging_lines()
    print("end", flush=True)


if __name__ == "__main__":
    main()
