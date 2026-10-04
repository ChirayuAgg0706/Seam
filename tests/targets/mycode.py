"""User code that calls into the standard library, and is called back by it."""
import heapq
import string
from contextlib import contextmanager
from dataclasses import dataclass


def weight(item):
    return len(item)  # weight-body


@contextmanager
def managed(log):
    log.append("enter")  # managed-enter
    yield log  # managed-yield
    log.append("exit")  # managed-exit


@dataclass
class Point:
    x: int
    y: int


def main():
    words = ["pear", "fig", "banana"]
    title = string.capwords("just my code")  # main-library
    longest = heapq.nlargest(2, words, key=weight)  # main-callback
    log = []
    with managed(log) as held:  # main-with
        held.append("body")  # main-body
    point = Point(1, 2)  # main-dataclass
    ordered = sorted(words, key=weight)  # main-sorted
    return title, longest, log, point, ordered  # main-return


print(main())  # module-call
