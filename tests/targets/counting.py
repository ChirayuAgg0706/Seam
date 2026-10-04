"""Calls a native function ten times."""
import seamtest


def main():
    total = 0
    for i in range(10):
        total = seamtest.add(total, i)  # loop-add
    print("total", total, flush=True)  # after-loop


main()
