"""Target of tests/test_cython_exceptions.py: a Cython function raises, twice."""
import seam_cyraise


def main():
    try:
        seam_cyraise.fail(3)  # fail-call
    except ValueError as exc:
        print("caught", exc)  # caught
    seam_cyraise.fail(4)  # fail-again


main()  # module-main
