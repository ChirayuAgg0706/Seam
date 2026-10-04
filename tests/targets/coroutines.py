"""Coroutines, tasks, async generators and async context managers to step through."""
import asyncio
import sys


async def ticker(seen):
    """A second task that is always about to run: whatever suspends, this runs meanwhile."""
    while True:
        seen.append(len(seen))  # ticker-body
        await asyncio.sleep(0.001)


async def inner(n):
    first = n + 1  # inner-first
    await asyncio.sleep(0.01)  # inner-sleep
    await asyncio.sleep(0.01)  # inner-again
    return first * 2  # inner-return


async def waits():
    seen = []
    background = asyncio.create_task(ticker(seen))
    before = 1  # waits-first
    await asyncio.sleep(0.01)  # waits-sleep
    value = await inner(before)  # waits-inner
    after = value + 1  # waits-after
    background.cancel()
    return after, len(seen)  # waits-return


async def worker(name, delay):
    await asyncio.sleep(delay)  # worker-sleep
    return name.upper()  # worker-return


async def tasks():
    task = asyncio.create_task(worker("a", 0.01))
    await asyncio.sleep(0.05)  # tasks-wait
    first = await task  # tasks-join
    both = await asyncio.gather(worker("b", 0.01), worker("c", 0.03))  # tasks-gather
    return [first] + both  # tasks-return


async def other():
    await asyncio.sleep(0.005)
    noted = "other ran"  # other-body
    return noted


async def interleaved():
    task = asyncio.create_task(other())
    await asyncio.sleep(0.05)  # interleaved-sleep
    result = await task  # interleaved-after
    return result


class Resource:
    async def __aenter__(self):
        await asyncio.sleep(0.005)  # aenter-first
        return self  # aenter-return

    async def __aexit__(self, *exc):
        await asyncio.sleep(0.005)  # aexit-first
        return False


async def numbers(n):
    for i in range(n):  # numbers-first
        await asyncio.sleep(0.005)  # numbers-sleep
        yield i + 1  # numbers-yield


async def protocols():
    async with Resource() as res:  # protocols-with
        held = res  # protocols-body
    total = 0  # protocols-between
    async for value in numbers(2):  # protocols-for
        total += value  # protocols-loop
    squares = [v * v async for v in numbers(2)]  # protocols-comprehension
    return total, squares  # protocols-return


async def slow():
    try:
        await asyncio.sleep(10)  # slow-sleep
    except asyncio.CancelledError:  # slow-except
        cleaned = True  # slow-cancelled
        raise  # slow-raise


async def thrown():
    task = asyncio.create_task(slow())
    await asyncio.sleep(0.01)
    task.cancel()  # thrown-cancel
    try:
        await task  # thrown-join
    except asyncio.CancelledError:  # thrown-except
        outcome = "cancelled"  # thrown-caught
    try:
        async with asyncio.timeout(0.01):  # thrown-timeout
            await asyncio.sleep(10)  # thrown-long
    except TimeoutError:  # thrown-handler
        outcome = "timed out"  # thrown-timed-out
    return outcome  # thrown-return


def double(v):
    return v * 2  # double-body


async def native():
    import seamtest

    total = seamtest.add(2, 3)  # native-add
    doubled = seamtest.call_back(double, total)  # native-callback
    await asyncio.sleep(0.005)  # native-sleep
    return doubled  # native-return


def main(which):
    result = asyncio.run(globals()[which]())  # main-run
    print(which, result)  # main-print


main(sys.argv[1])  # module-main
