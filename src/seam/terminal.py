"""Runs in the editor's terminal on behalf of `seam dap` (launch option "console").

It tells the adapter which terminal it is on, then stays in the foreground without ever
reading from it. The adapter starts the debugged program with that terminal as its
standard input and output, so the program has the keyboard to itself. Ctrl-C lands here,
because this process is the terminal's foreground job, and is passed on to the program.

Stand-alone on purpose (standard library only): it is started by the editor, in whatever
environment the editor's terminal has.
"""
import os
import signal
import socket
import sys

FORWARDED = (signal.SIGINT, signal.SIGQUIT, signal.SIGHUP)


def main(channel_path):
    channel = socket.socket(socket.AF_UNIX)
    channel.connect(channel_path)
    channel.sendall(("tty %s\n" % os.ttyname(0)).encode())

    def forward(number, frame):
        try:
            channel.sendall(b"signal %d\n" % number)
        except OSError:
            pass
        if number == signal.SIGHUP:  # the terminal is gone
            os._exit(0)

    for number in FORWARDED:
        signal.signal(number, forward)
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)  # Ctrl-Z would only suspend this holder
    # The adapter hangs up when the debug session ends.
    try:
        while channel.recv(4096):
            pass
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
