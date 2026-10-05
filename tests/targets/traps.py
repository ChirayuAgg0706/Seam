"""Target of the entry-trap scenarios: python traps.py threads|fork|sleep"""
import os
import sys
import threading
import time

import seamtest

STOP = False


def hammer(results):
    count = 0
    while not STOP:
        if seamtest.add(count, 1) != count + 1:
            results.append("wrong sum")
            return
        count += 1
    results.append(count)


def threads():
    """Other threads call the function the main thread is about to step into."""
    global STOP
    results = []
    workers = [threading.Thread(target=hammer, args=(results,)) for _ in range(2)]
    for worker in workers:
        worker.start()
    time.sleep(0.2)
    value = (time.sleep(0.1), seamtest.add(20, 22))[1]  # threads-call
    STOP = True  # threads-after
    for worker in workers:
        worker.join()
    print("value", value, "workers", [isinstance(r, int) and r > 0 for r in results])


def fork():
    """A child forked during a step-in calls native code the step-in had traps on."""
    pid = os.fork()  # fork-call
    if pid == 0:
        os._exit(0 if seamtest.add(2, 3) == 5 else 1)
    _, status = os.waitpid(pid, 0)  # fork-after
    print("child status", status)
    print("parent sum", seamtest.add(2, 3))


def sleep():
    """A step-in that stays in flight for a while, without reaching user code."""
    time.sleep(1.0)  # sleep-call
    total = seamtest.add(1, 2)  # sleep-after
    print("total", total)


{"threads": threads, "fork": fork, "sleep": sleep}[sys.argv[1]]()  # module-main
