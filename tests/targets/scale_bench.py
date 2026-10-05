"""Overhead workloads with a large module loaded: python scale_bench.py cpu|native MODULE FUNCTION

Like bench.py: prints "elapsed <seconds>", timed inside the program. `cpu` is pure Python
with MODULE merely imported; `native` calls MODULE.FUNCTION(a, b) in a loop.
"""
import importlib
import sys
import time


def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)


def cpu(call):
    total = fib(32)
    for i in range(6_000_000):
        total += i * i % 7
    return total


def native(call):
    total = 0
    for i in range(8_000_000):
        total = call(total & 0xFFFF, i)
    return total


WORKLOADS = {"cpu": cpu, "native": native}

if __name__ == "__main__":
    work = WORKLOADS[sys.argv[1]]
    function = getattr(importlib.import_module(sys.argv[2]), sys.argv[3])
    start = time.perf_counter()
    work(function)
    print("elapsed %.6f" % (time.perf_counter() - start))
