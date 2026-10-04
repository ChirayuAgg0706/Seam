"""The program runs in the client's terminal (launch option "console"): input works."""
import os
import signal

import pytest

from conftest import marker_line, target
from dapclient import Terminal
from test_python import py_frames

pytestmark = pytest.mark.smoke

INTERACTIVE = target("interactive.py")


@pytest.mark.parametrize("console", ["integratedTerminal", "externalTerminal"])
def test_program_reads_from_the_terminal(dap, console):
    dap.terminal = Terminal()
    line = marker_line(INTERACTIVE, "after-input")
    dap.launch(INTERACTIVE, dap.python, console=console, breakpoints={INTERACTIVE: [line]})
    dap.terminal.read_until("name? ")
    assert "tty True True" in dap.terminal.text
    dap.terminal.type("seam\n")

    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack) == [("<module>", line)]
    assert dap.scope(stack[0]["id"])["name"]["value"] == "'seam'"
    dap.cont()
    assert dap.wait_exit() == 0
    dap.terminal.read_until("hello seam")
    dap.terminal.read_until("bye")
    # The program's output went to the terminal, not to the debug console.
    assert "hello seam" not in dap.output

    # The session's end releases the terminal.
    holder = dap.terminal.proc
    dap.close()
    assert holder.wait(timeout=10) == 0


def test_ctrl_c_in_the_terminal_interrupts_the_program(dap):
    dap.terminal = Terminal()
    dap.launch(INTERACTIVE, dap.python, args=["wait"], console="integratedTerminal")
    dap.terminal.read_until("name? ")
    dap.terminal.type("seam\n")
    dap.terminal.read_until("hello seam")
    dap.terminal.type("\x03")
    dap.terminal.read_until("interrupted")
    assert dap.wait_exit() == 0
    dap.terminal.read_until("bye")


def test_closing_the_terminal_ends_the_program(dap):
    dap.terminal = Terminal()
    dap.launch(INTERACTIVE, dap.python, args=["wait"], console="integratedTerminal")
    dap.terminal.read_until("name? ")
    dap.terminal.type("seam\n")
    dap.terminal.read_until("hello seam")
    os.close(dap.terminal.master)  # the terminal goes away, as when its tab is closed
    assert dap.wait_exit() == 128 + signal.SIGHUP
    assert "terminated by signal SIGHUP" in dap.output


def test_a_client_without_a_terminal_gets_the_debug_console(dap):
    dap.launch(INTERACTIVE, dap.python, console="integratedTerminal")
    # Nobody can type: the program's input is empty rather than waiting for ever.
    assert dap.wait_exit() == 1
    assert "cannot run the program in a terminal" in dap.output
    assert "name? " in dap.output and "EOFError" in dap.plain_output


def test_input_is_empty_in_the_debug_console(dap):
    dap.launch(INTERACTIVE, dap.python)
    assert dap.wait_exit() == 1
    assert "EOFError" in dap.plain_output
