"""Target of the inlined-glue scenarios (tests/test_inlined_glue.py)."""
import seam_inline


class Noisy:
    def __del__(self):
        self.gone = True  # del-body


def main():
    value = [1, 2]
    same = seam_inline.keep(value)  # keep-call
    print("same", same is value)  # after-keep
    seam_inline.drop(Noisy)  # drop-call
    print("dropped")  # after-drop


main()  # module-main
