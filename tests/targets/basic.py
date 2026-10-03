import sys


def inner(a, b):
    total = a + b  # inner-first
    return total * 2  # inner-return


def outer(n):  # outer-def
    items = [1, 2, 3]
    result = inner(n, 10)  # outer-call
    return result + len(items)  # outer-after


def main():
    name = "seam"
    x = outer(5)  # main-call
    print("result", x)  # main-print
    import late_module  # main-import

    print(late_module.late(x))
    return x


if __name__ == "__main__":
    sys.exit(0 if main() == 33 else 1)  # module-call
