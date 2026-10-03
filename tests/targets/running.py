import time

import seamtest

STOP = False


def tick(n):
    value = seamtest.add(n, 1)  # tick-add
    return value  # tick-return


def main():
    n = 0
    deadline = time.time() + 30
    while not STOP and time.time() < deadline:
        n = tick(n)
        time.sleep(0.4)  # leaves the tests a window in which no breakpoint can be hit
    print("stopped" if STOP else "timeout")
    return 0 if STOP else 1


raise SystemExit(main())
