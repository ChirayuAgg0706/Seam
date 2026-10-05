"""Stepping through coroutines, tasks, async generators and plain generators.

A suspension is not a return: a step stays with the coroutine or generator it was started
in until that frame runs its next line, whatever the event loop or the consumer runs in
between. When the frame finishes into code that is not the user's (the event loop), the
step ends at the next line of user code the thread runs.
"""
import pytest

from conftest import CAPI_SRC, at_line, marker_line, target
from test_python import ground_truth

# What sys.monitoring reports around yields and resumes depends on the interpreter
# version, so these run on every version of the matrix.
pytestmark = pytest.mark.smoke

COROUTINES = target("coroutines.py")
GENERATORS = target("generators.py")


def at(marker, path=COROUTINES):
    return marker_line(path, marker)


def top(dap, tid):
    frame = dap.stack(tid)[0]
    return frame["name"], frame["line"]


def step(dap, tid, command="next"):
    """One step; where it ended, as (function, line)."""
    stop = dap.step(command, tid)
    assert stop["reason"] == "step"
    frame = dap.stack(tid)[0]
    assert "source" in frame, "stopped in code without a source file: %s" % frame
    return frame["name"], frame["line"]


def user_frames(stack, path):
    """The frames of a merged stack that are in the target script, as (name, line)."""
    return [(f["name"], f["line"]) for f in stack if f.get("source", {}).get("path") == path]


def python_part(stack):
    """The Python frames of a merged stack as (bare name, line), for the ground truth."""
    return [(f["name"].split(".")[-1], f["line"]) for f in stack
            if f.get("source", {}).get("path", "").endswith(".py")]


def assert_nothing_armed(dap):
    status = dap.status()
    assert status["stepInBreakpointsEnabled"] is False
    assert status["nativeStepInProgress"] is False
    assert status["pythonStepArmed"] is False
    if status["safe"]:
        assert status["agent"]["stepping"] is None
    return status


def finish(dap, *paths):
    """Remove the breakpoints and let the program run to its end."""
    for path in paths or (COROUTINES,):
        dap.set_breakpoints(path, [])
    status = assert_nothing_armed(dap)
    assert status["agent"]["global_events"] == 0 and status["agent"]["local_events"] == {}
    dap.cont()
    assert dap.wait_exit() == 0


def launch(dap, scenario, *markers, **extra):
    dap.launch(COROUTINES, dap.python, args=[scenario],
               breakpoints={COROUTINES: [at(m) for m in markers]}, **extra)
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    return stop["threadId"]


# ------------------------------------------------------------------ coroutines

def test_step_over_an_await_stays_in_the_coroutine(dap, iteration):
    tid = launch(dap, "waits", "waits-sleep")
    assert top(dap, tid) == ("waits", at("waits-sleep"))
    # The await really suspends, and another task runs its own lines meanwhile.
    assert step(dap, tid) == ("waits", at("waits-inner"))
    frame = dap.stack(tid)[0]["id"]
    assert int(dap.evaluate("len(seen)", frame)["result"]) > 0
    assert_nothing_armed(dap)
    # An awaited coroutine that suspends twice on the way.
    assert step(dap, tid) == ("waits", at("waits-after"))
    assert dap.scope(dap.stack(tid)[0]["id"])["value"]["value"] == "4"
    finish(dap)
    assert "waits (5," in dap.output


def test_step_in_on_an_await_that_only_suspends_acts_like_step_over(dap, iteration):
    tid = launch(dap, "waits", "waits-sleep")
    # Nothing of the user's is called by this line. Not asyncio's files, and not the
    # other task, whose lines run while this coroutine is suspended.
    assert step(dap, tid, "stepIn") == ("waits", at("waits-inner"))
    finish(dap)


def test_step_in_and_out_of_an_awaited_coroutine(dap, iteration):
    tid = launch(dap, "waits", "waits-inner")
    assert step(dap, tid, "stepIn") == ("inner", at("inner-first"))
    stack = dap.stack(tid)
    assert user_frames(stack, COROUTINES) == [
        ("inner", at("inner-first")), ("waits", at("waits-inner")),
        ("main", at("main-run")), ("<module>", at("module-main"))]
    # The Python frames, the event loop's included, are what Python itself reports.
    assert python_part(stack) == ground_truth(dap, stack[0]["id"])
    assert dap.scope(stack[1]["id"])["before"]["value"] == "1"

    assert step(dap, tid) == ("inner", at("inner-sleep"))
    assert step(dap, tid) == ("inner", at("inner-again"))
    # Out: `inner` suspends once more before it returns to the coroutine awaiting it.
    assert step(dap, tid, "stepOut") == ("waits", at("waits-inner"))
    assert step(dap, tid) == ("waits", at("waits-after"))
    assert dap.scope(dap.stack(tid)[0]["id"])["value"]["value"] == "4"
    finish(dap)


def test_step_out_of_a_coroutine_that_has_not_suspended_yet(dap, iteration):
    tid = launch(dap, "waits", "inner-first")
    assert step(dap, tid, "stepOut") == ("waits", at("waits-inner"))
    finish(dap)


def test_stepping_off_the_end_of_a_task_goes_to_the_next_user_line(dap, iteration):
    tid = launch(dap, "tasks", "worker-return")
    frame = dap.stack(tid)[0]["id"]
    assert dap.evaluate("name", frame)["result"] == "'a'"
    # The task is done. Its caller is the event loop; the next user code to run is the
    # coroutine that started it, which is still asleep on the line before.
    assert step(dap, tid) == ("tasks", at("tasks-join"))
    assert_nothing_armed(dap)

    dap.cont()
    assert dap.wait_stopped()["reason"] == "breakpoint"
    frame = dap.stack(tid)[0]["id"]
    assert dap.evaluate("name", frame)["result"] == "'b'"
    dap.set_breakpoints(COROUTINES, [])
    # Now the next user code is the other task under asyncio.gather.
    assert step(dap, tid) == ("worker", at("worker-return"))
    assert dap.evaluate("name", dap.stack(tid)[0]["id"])["result"] == "'c'"
    assert step(dap, tid) == ("tasks", at("tasks-return"))
    # Off the end of the coroutine given to asyncio.run(): back on the line that called it.
    assert step(dap, tid) == ("main", at("main-run"))
    assert step(dap, tid) == ("main", at("main-print"))
    finish(dap)
    assert "tasks ['A', 'B', 'C']" in dap.output


def test_step_over_follows_its_own_frame_not_the_function(dap, iteration):
    tid = launch(dap, "tasks", "worker-sleep")
    assert dap.evaluate("name", dap.stack(tid)[0]["id"])["result"] == "'a'"
    dap.cont()
    assert dap.wait_stopped()["reason"] == "breakpoint"
    assert dap.evaluate("name", dap.stack(tid)[0]["id"])["result"] == "'b'"
    dap.set_breakpoints(COROUTINES, [])
    # While this worker sleeps, asyncio.gather starts the other one: the same function,
    # the same lines, another frame.
    assert step(dap, tid) == ("worker", at("worker-return"))
    assert dap.evaluate("name", dap.stack(tid)[0]["id"])["result"] == "'b'"
    finish(dap)


def test_with_just_my_code_off_a_finished_task_returns_into_the_event_loop(dap, iteration):
    tid = launch(dap, "tasks", "worker-return", justMyCode=False)
    stop = dap.step("next", tid)
    assert stop["reason"] == "step"
    frame = dap.stack(tid)[0]
    assert frame["name"] == "Handle._run"
    assert frame["source"]["path"].endswith("asyncio/events.py")
    finish(dap)


def test_a_breakpoint_in_another_task_ends_a_step_over_an_await(dap, iteration):
    tid = launch(dap, "interleaved", "interleaved-sleep", "other-body")
    stop = dap.step("next", tid)
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(tid)
    # The stack is that task's: the stepped coroutine is suspended, not on it.
    assert user_frames(stack, COROUTINES) == [
        ("other", at("other-body")), ("main", at("main-run")),
        ("<module>", at("module-main"))]
    assert python_part(stack) == ground_truth(dap, stack[0]["id"])
    assert dap.status()["agent"]["stepping"] is None
    # The step is over: nothing stops on the line after the await.
    finish(dap)
    assert "interleaved other ran" in dap.output


def test_step_over_async_with_and_async_for(dap, iteration):
    tid = launch(dap, "protocols", "protocols-with")
    dap.set_breakpoints(COROUTINES, [])
    visited = []
    while not visited or visited[-1] != at("protocols-return"):
        name, line = step(dap, tid)
        assert name == "protocols"
        visited.append(line)
        assert len(visited) < 20
    loop = [at("protocols-for"), at("protocols-loop")]
    assert visited == [
        at("protocols-body"),
        at("protocols-with"),  # leaving the block: __aexit__ belongs to the `with` line
        at("protocols-between"),
        *loop, *loop, at("protocols-for"),
        at("protocols-comprehension"), at("protocols-return")]
    assert step(dap, tid) == ("main", at("main-run"))
    finish(dap)
    assert "protocols (3, [1, 4])" in dap.output


def test_step_into_an_async_context_manager(dap, iteration):
    tid = launch(dap, "protocols", "protocols-with")
    dap.set_breakpoints(COROUTINES, [])
    assert step(dap, tid, "stepIn") == ("Resource.__aenter__", at("aenter-first"))
    assert step(dap, tid) == ("Resource.__aenter__", at("aenter-return"))
    assert step(dap, tid, "stepOut") == ("protocols", at("protocols-with"))
    assert step(dap, tid) == ("protocols", at("protocols-body"))
    # Leaving the block is the `with` line's business: it calls __aexit__, which suspends
    # before it returns.
    assert step(dap, tid, "stepIn") == ("protocols", at("protocols-with"))
    assert step(dap, tid, "stepIn") == ("Resource.__aexit__", at("aexit-first"))
    assert step(dap, tid, "stepOut") == ("protocols", at("protocols-with"))
    assert step(dap, tid) == ("protocols", at("protocols-between"))
    finish(dap)


def test_step_through_an_async_generator(dap, iteration):
    tid = launch(dap, "protocols", "protocols-for")
    dap.set_breakpoints(COROUTINES, [])
    assert step(dap, tid, "stepIn") == ("numbers", at("numbers-first"))
    assert step(dap, tid) == ("numbers", at("numbers-sleep"))
    assert step(dap, tid) == ("numbers", at("numbers-yield"))
    # Step in at a `yield` follows the value to its consumer...
    assert step(dap, tid, "stepIn") == ("protocols", at("protocols-loop"))
    assert dap.scope(dap.stack(tid)[0]["id"])["value"]["value"] == "1"
    assert step(dap, tid) == ("protocols", at("protocols-for"))
    # ...and step in on the loop line goes back into the generator, where it left off.
    assert step(dap, tid, "stepIn") == ("numbers", at("numbers-first"))
    assert step(dap, tid) == ("numbers", at("numbers-sleep"))
    assert step(dap, tid) == ("numbers", at("numbers-yield"))
    # Step over a `yield` stays in the generator: the consumer's loop body runs unseen.
    assert step(dap, tid) == ("numbers", at("numbers-first"))
    stack = dap.stack(tid)
    assert user_frames(stack, COROUTINES)[:2] == [
        ("numbers", at("numbers-first")), ("protocols", at("protocols-for"))]
    assert dap.scope(stack[1]["id"])["total"]["value"] == "3"
    # The generator is exhausted: off its end, back in the consumer on the loop line.
    assert step(dap, tid) == ("protocols", at("protocols-for"))
    assert step(dap, tid) == ("protocols", at("protocols-comprehension"))
    finish(dap)


def test_step_out_of_an_async_generator_runs_it_to_its_end(dap, iteration):
    tid = launch(dap, "protocols", "numbers-sleep")
    dap.set_breakpoints(COROUTINES, [])
    # Yields are not returns: out means out of the generator for good.
    assert step(dap, tid, "stepOut") == ("protocols", at("protocols-for"))
    assert dap.scope(dap.stack(tid)[0]["id"])["total"]["value"] == "3"
    finish(dap)


def test_cancellation_ends_a_step_at_the_handler(dap, iteration):
    tid = launch(dap, "thrown", "slow-sleep")
    # The task is cancelled while the step waits for its sleep. The breakpoint on the
    # await's line stays set: from 3.13 on the interpreter reports that line once more
    # when the exception arrives, and that must be neither a hit nor the step's end.
    assert step(dap, tid) == ("slow", at("slow-except"))
    assert step(dap, tid) == ("slow", at("slow-cancelled"))
    assert step(dap, tid) == ("slow", at("slow-raise"))
    # The exception leaves the task. The next user code is the coroutine awaiting it,
    # where the exception is raised again and handled.
    assert step(dap, tid) == ("thrown", at("thrown-except"))
    assert step(dap, tid) == ("thrown", at("thrown-caught"))
    assert_nothing_armed(dap)
    finish(dap)
    assert "thrown timed out" in dap.output


def test_a_timeout_ends_a_step_at_the_handler(dap, iteration):
    tid = launch(dap, "thrown", "thrown-long")
    dap.set_breakpoints(COROUTINES, [])
    # asyncio.timeout() cancels the sleep; its __aexit__ (on the `with` line) turns the
    # cancellation into TimeoutError.
    assert step(dap, tid) == ("thrown", at("thrown-timeout"))
    assert step(dap, tid) == ("thrown", at("thrown-handler"))
    assert step(dap, tid) == ("thrown", at("thrown-timed-out"))
    assert step(dap, tid) == ("thrown", at("thrown-return"))
    finish(dap)


def test_an_unhandled_cancellation_ends_a_step_out_where_it_is_caught(dap, iteration):
    tid = launch(dap, "thrown", "slow-cancelled")
    dap.set_breakpoints(COROUTINES, [])
    # Out of a coroutine that ends by raising: the step follows the exception.
    assert step(dap, tid, "stepOut") == ("thrown", at("thrown-except"))
    finish(dap)


def test_step_from_a_coroutine_into_native_code_and_back(dap, capi, iteration):
    line = at("native-add")
    dap.launch(COROUTINES, dap.python, args=["native"], env=capi.env,
               breakpoints={COROUTINES: [line]})
    tid = dap.wait_stopped()["threadId"]
    assert top(dap, tid) == ("native", line)

    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert [f["name"] for f in stack[:2]] == ["st_add", "native"]
    assert stack[0]["source"]["path"] == CAPI_SRC
    assert at_line(capi, stack[0]["line"], marker_line(CAPI_SRC, "add-first"))
    assert_nothing_armed(dap)
    assert step(dap, tid, "stepOut") == ("native", line)
    assert step(dap, tid) == ("native", at("native-callback"))

    # Through native code into the Python function it calls back, and out again.
    stop = dap.step("stepIn", tid)
    assert dap.stack(tid)[0]["name"] == "st_call_back"
    dap.set_breakpoints(CAPI_SRC, [marker_line(CAPI_SRC, "callback-call")])
    dap.cont()
    assert dap.wait_stopped()["reason"] == "breakpoint"
    dap.set_breakpoints(CAPI_SRC, [])
    assert step(dap, tid, "stepIn") == ("double", at("double-body"))
    stack = dap.stack(tid)
    assert [f["name"] for f in stack[:3]] == ["double", "st_call_back", "native"]
    stop = dap.step("stepOut", tid)
    assert stop["reason"] == "step" and dap.stack(tid)[0]["name"] == "st_call_back"
    stop = dap.step("stepOut", tid)
    assert stop["reason"] == "step"
    assert top(dap, tid) == ("native", at("native-callback"))

    assert step(dap, tid) == ("native", at("native-sleep"))
    assert step(dap, tid) == ("native", at("native-return"))
    assert dap.scope(dap.stack(tid)[0]["id"])["doubled"]["value"] == "10"
    finish(dap)


# ------------------------------------------------------------------ generators

def launch_generators(dap, *markers):
    dap.launch(GENERATORS, dap.python,
               breakpoints={GENERATORS: [at(m, GENERATORS) for m in markers]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    return stop["threadId"]


def g(marker):
    return at(marker, GENERATORS)


def test_step_into_a_generator_and_over_its_yield(dap, iteration):
    tid = launch_generators(dap, "consume-for")
    dap.set_breakpoints(GENERATORS, [])
    assert step(dap, tid, "stepIn") == ("countdown", g("countdown-first"))
    assert step(dap, tid) == ("countdown", g("countdown-yield"))
    # As in pdb and debugpy, stepping over a `yield` stays in the generator: the step
    # ends on its next line when the consumer asks for the next value.
    assert step(dap, tid) == ("countdown", g("countdown-after"))
    stack = dap.stack(tid)
    assert user_frames(stack, GENERATORS) == [
        ("countdown", g("countdown-after")), ("consume", g("consume-for")),
        ("<module>", g("module-call"))]
    assert dap.scope(stack[1]["id"])["seen"]["value"] == "[2]"
    finish(dap, GENERATORS)


def test_step_in_at_a_yield_follows_the_value_to_the_consumer(dap, iteration):
    tid = launch_generators(dap, "countdown-yield")
    dap.set_breakpoints(GENERATORS, [])
    assert step(dap, tid, "stepIn") == ("consume", g("consume-body"))
    assert dap.scope(dap.stack(tid)[0]["id"])["value"]["value"] == "2"
    assert step(dap, tid, "stepIn") == ("consume", g("consume-for"))
    # Step in on the loop line resumes the generator and stops there.
    assert step(dap, tid, "stepIn") == ("countdown", g("countdown-after"))
    finish(dap, GENERATORS)


def after_the_loop(dap):
    """Where a step lands in `consume` when the generator its `for` loop runs is finished.

    The step ends at the consumer's next instruction. From 3.13 on that is the loop's
    own clean-up, still on the `for` line; 3.12 has nothing left to run on that line
    and goes straight to the statement after the loop.
    """
    version = tuple(dap.status()["agent"]["version"][:2])
    return ("consume", g("consume-for") if version >= (3, 13) else g("consume-make"))


def test_step_out_of_a_generator_runs_it_to_its_end(dap, iteration):
    tid = launch_generators(dap, "countdown-after")
    dap.set_breakpoints(GENERATORS, [])
    # Yields are not returns: the loop body runs for every remaining value first.
    assert step(dap, tid, "stepOut") == after_the_loop(dap)
    assert dap.scope(dap.stack(tid)[0]["id"])["seen"]["value"] == "[2, 1]"
    for _ in range(2):
        if top(dap, tid) != ("consume", g("consume-next")):
            step(dap, tid)
    assert top(dap, tid) == ("consume", g("consume-next"))
    # next(gen) is a builtin: step in goes through it into the generator.
    assert step(dap, tid, "stepIn") == ("countdown", g("countdown-first"))
    finish(dap, GENERATORS)


def test_step_over_the_return_of_a_generator_lands_in_the_consumer(dap, iteration):
    tid = launch_generators(dap, "countdown-return")
    dap.set_breakpoints(GENERATORS, [])
    assert step(dap, tid) == after_the_loop(dap)
    finish(dap, GENERATORS)


def test_a_step_left_in_an_abandoned_generator_does_not_get_in_the_way(dap, iteration):
    tid = launch_generators(dap, "consume-next")
    assert step(dap, tid, "stepIn") == ("countdown", g("countdown-first"))
    assert step(dap, tid) == ("countdown", g("countdown-yield"))
    # The consumer closes this generator instead of resuming it.
    dap.set_breakpoints(GENERATORS, [g("consume-return")])
    stop = dap.step("next", tid)
    if stop["reason"] == "step":
        # The interpreter closed it by raising GeneratorExit in it: the step follows the
        # exception out, to the next line of the frame that closed the generator.
        assert top(dap, tid) == ("consume", g("consume-close") + 1)
        assert_nothing_armed(dap)
        dap.cont()
        stop = dap.wait_stopped()
    # Some interpreters (3.12.3 here) close a generator that is suspended outside any
    # `try` without running it at all. Then the step has nowhere to end and the program
    # runs on, as it does under pdb; the next breakpoint is an ordinary stop.
    assert stop["reason"] == "breakpoint"
    assert top(dap, tid) == ("consume", g("consume-return"))
    finish(dap, GENERATORS)


def test_closing_a_generator_ends_a_step_in_its_finally_block(dap, iteration):
    tid = launch_generators(dap, "guarded-yield")
    dap.set_breakpoints(GENERATORS, [])
    assert step(dap, tid) == ("guarded", g("guarded-finally"))
    # GeneratorExit carries on out of the generator, into the frame that closed it.
    assert step(dap, tid) == ("consume", g("consume-chain"))
    finish(dap, GENERATORS)


def test_step_in_and_out_through_yield_from(dap, iteration):
    tid = launch_generators(dap, "chain-from")
    dap.set_breakpoints(GENERATORS, [])
    assert step(dap, tid, "stepIn") == ("countdown", g("countdown-first"))
    assert user_frames(dap.stack(tid), GENERATORS) == [
        ("countdown", g("countdown-first")), ("chain", g("chain-from")),
        ("consume", g("consume-chain")), ("<module>", g("module-call"))]
    assert step(dap, tid, "stepOut") == ("chain", g("chain-from"))
    assert step(dap, tid) == ("chain", g("chain-result"))
    assert dap.scope(dap.stack(tid)[0]["id"])["result"]["value"] == "'liftoff'"
    # Over the last `yield`: when list() asks again the generator falls off its end.
    assert step(dap, tid) == ("consume", g("consume-chain"))
    assert step(dap, tid) == ("consume", g("consume-return"))
    finish(dap, GENERATORS)
    assert "([2, 1], 1, [1, 'liftoff'])" in dap.output
