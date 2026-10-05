"""Exceptions that leave the user's code for a library that called it, and ones that do not."""
import heapq
import os
import pathlib
import re
import sys
import threading


def explode(word):
    detail = "cannot replace " + word
    raise ValueError(detail)  # explode-raise


def check(match):
    word = match.group(0)
    if word == "bad":
        raise ValueError("cannot replace " + word)  # callback-raise
    if word == "worse":
        explode(word)  # callback-call
    return word.upper()


def replace(text):
    return re.sub(r"\w+", check, text)  # library-call


class Countdown:
    """An iterator: ending an iteration is not an error, whoever consumes it."""

    def __init__(self, start):
        self.left = start

    def __iter__(self):
        return self

    def __next__(self):
        if self.left == 0:
            raise StopIteration
        self.left -= 1
        return self.left


def lookup(table, key):
    return table[key]  # lookup-raise


def quiet():
    """Nothing here leaves user code for a library with an error."""
    try:
        lookup({}, "missing")  # quiet-call
    except KeyError:  # quiet-except
        pass  # quiet-pass
    try:
        int("x")
    except ValueError:
        pass
    # Control flow inside libraries, which never passes through a user frame.
    os.makedirs(os.path.dirname(os.path.abspath(__file__)), exist_ok=True)
    present = pathlib.Path("/no/such/file/for/seam").exists()
    # A library's Python code consumes a user iterator until it raises StopIteration.
    merged = list(heapq.merge(Countdown(2), Countdown(3)))
    print("quiet: %s %s" % (present, merged), flush=True)


def main():
    mode = sys.argv[1]
    if mode == "callback":
        for text in ("fine bad", "fine worse"):
            try:
                replace(text)  # module-call
            except ValueError as exc:
                print("caught: %s" % exc, flush=True)
    elif mode == "quiet":
        quiet()
    elif mode == "thread":
        worker = threading.Thread(target=lookup, args=({}, "missing"))
        worker.start()
        worker.join()
    elif mode == "uncaught":
        lookup({}, "missing")  # uncaught-call
    elif mode == "exit":
        sys.exit(3)
    print("end", flush=True)


main()
