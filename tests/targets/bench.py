"""Benchmark workloads for the overhead check: python bench.py cpu|threads|native <module dir>.

Prints "elapsed <seconds>", timed inside the program so interpreter start-up and the
debugger's launch work are not counted.
"""
import sys
import threading
import time


def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)


def cpu():
    total = fib(29)
    for i in range(1_500_000):
        total += i * i % 7
    return total


def threads():
    for _ in range(300):
        t = threading.Thread(target=fib, args=(8,))
        t.start()
        t.join()


def native():
    import seamtest

    total = 0
    for i in range(1_500_000):
        total = seamtest.add(total, i)
    return total


WORKLOADS = {"cpu": cpu, "threads": threads, "native": native}

if __name__ == "__main__":
    work = WORKLOADS[sys.argv[1]]
    start = time.perf_counter()
    work()
    print("elapsed %.6f" % (time.perf_counter() - start))
