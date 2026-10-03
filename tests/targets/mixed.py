import seamtest


def leaf(v):
    doubled = seamtest.add(v, v)  # leaf-add
    return doubled  # leaf-return


def middle(v):
    label = "middle"
    out = seamtest.call_back(leaf, v)  # middle-callback
    return out + 1  # middle-after


def main():
    numbers = [1, 2.5, "three", None, True]
    total = middle(20)  # main-middle
    print("total", total)  # main-print
    try:
        seamtest.fail()  # main-fail
    except ValueError as exc:
        print("caught", exc)  # main-caught
    print("done")  # main-done


main()  # module-main
