"""Ends in the way named on the command line."""
import os
import sys
import time

how = sys.argv[1]
print("exiting by", how, flush=True)
if how == "sys-exit":
    sys.exit(3)
elif how == "os-exit":
    os._exit(7)
elif how == "raise":
    raise RuntimeError("uncaught on purpose")
elif how == "wait":
    while True:
        time.sleep(0.2)  # wait-loop
