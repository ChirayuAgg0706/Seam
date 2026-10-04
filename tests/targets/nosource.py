"""Target for the scenarios about code without source: a stripped extension, a crash in it,
and crashes inside the interpreter itself."""
import sys

import seam_nosource


def run(value):
    result = seam_nosource.work(value)  # work-call
    return result


def main():
    print("work", run(2), flush=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "crash":
        seam_nosource.crash()  # crash-call
    elif mode == "bad-object":
        seam_nosource.bad_object()  # bad-call
    elif mode == "sigsegv":
        import faulthandler

        faulthandler._sigsegv()  # sigsegv-call
    print("done", flush=True)


main()  # module-main
