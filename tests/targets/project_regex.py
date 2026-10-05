"""Target of the real-project scenarios: regex (plain C API). python project_regex.py [error]"""
import sys

import regex


def shout(match):
    return match.group(0).upper()  # shout-body


def main(mode):
    pattern = regex.compile(r"(\w+)@(\w+)\.com")  # compile
    found = pattern.search("mail bob@example.com now")  # search
    out = pattern.sub(shout, "bob@example.com, eve@example.com")  # sub
    print("found", found.group(1), "out", out)  # print
    if mode == "error":
        pattern.search(5)  # error


main(sys.argv[1] if len(sys.argv) > 1 else "")  # module-main
