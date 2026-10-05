"""The user's native function reached through a library's Python code."""
import functools
import heapq

import seamtest


def main():
    plus_one = functools.partial(seamtest.add, 1)
    top = heapq.nlargest(2, [3, 1, 2], key=plus_one)  # main-native-key
    return top  # main-return


print(main())
