"""Variables of several shapes, changed from the debugger while stopped."""
import seamtest


class Point:
    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x = x
        self.y = y


class Box:
    def __init__(self):
        self.label = "box"
        self.items = [1, 2, 3]


LIMIT = 10


def compute(count):
    scale = 2
    point = Point(3, 4)
    box = Box()
    table = {"a": 1, "b": 2}
    big = list(range(1000))
    total = seamtest.add(count, scale)  # before-call
    result = total * scale + point.x + box.items[0] + table["a"] + LIMIT  # after-call
    return result, box.label, len(big)


print("result", *compute(5), flush=True)
