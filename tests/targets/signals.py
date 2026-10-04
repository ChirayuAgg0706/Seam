"""Uses signals as part of normal operation. None of them is the debugger's business."""
import os
import signal
import subprocess
import time

seen = []
signal.signal(signal.SIGUSR1, lambda number, frame: seen.append("usr1"))
signal.signal(signal.SIGTERM, lambda number, frame: seen.append("term"))
signal.signal(signal.SIGALRM, lambda number, frame: seen.append("alrm"))

os.kill(os.getpid(), signal.SIGUSR1)
os.kill(os.getpid(), signal.SIGTERM)
signal.setitimer(signal.ITIMER_REAL, 0.05)
time.sleep(0.2)
subprocess.run(["true"], check=True)  # SIGCHLD
try:
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(1)
except KeyboardInterrupt:
    seen.append("int")

print("signals", ",".join(seen), flush=True)
