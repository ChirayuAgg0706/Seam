"""Changes a native static variable five times."""
import seamtest


def main():
    last = 0
    for i in range(5):
        last = seamtest.bump()  # loop-bump
    print("bumped", last, flush=True)


main()
