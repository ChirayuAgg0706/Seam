"""Talks to its terminal: reads a line, then waits to be interrupted."""
import os
import sys
import time

print("tty", os.isatty(0), os.isatty(1), flush=True)
name = input("name? ")
greeting = "hello " + name  # after-input
print(greeting, flush=True)
if sys.argv[1:] == ["wait"]:
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("interrupted", flush=True)
print("bye", flush=True)
