"""Target for the source-path scenarios: calls into extensions that were built elsewhere."""
import sys

import seam_mapped


def compute(value):
    scaled = seam_mapped.scale(value, 6)  # scale-call
    return scaled  # scale-after


def through_glue(value):
    doubled = seam_mapped.via_shim(value)  # shim-call
    return doubled  # shim-after


def throw():
    import seam_throwing

    try:
        seam_throwing.fail("why")  # throw-call
    except RuntimeError as exc:
        return "caught %s" % exc


def main():
    total = 0
    for i in range(3):
        total += compute(i + 1)  # loop-call
    print("total", total, flush=True)  # main-print
    print("shim", through_glue(5), flush=True)
    if "throw" in sys.argv[1:]:
        print(throw(), flush=True)
    if "crash" in sys.argv[1:]:
        seam_mapped.crash()  # crash-call
    print("done", flush=True)


main()  # module-main
