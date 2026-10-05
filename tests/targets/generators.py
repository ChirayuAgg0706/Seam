"""Plain generators to step through."""


def countdown(n):
    while n:  # countdown-first
        yield n  # countdown-yield
        n -= 1  # countdown-after
    return "liftoff"  # countdown-return


def guarded():
    try:
        yield 1  # guarded-yield
    finally:
        closed = True  # guarded-finally


def chain():
    result = yield from countdown(1)  # chain-from
    yield result  # chain-result


def consume():
    seen = []
    for value in countdown(2):  # consume-for
        seen.append(value)  # consume-body
    gen = countdown(1)  # consume-make
    first = next(gen)  # consume-next
    gen.close()  # consume-close
    guard = guarded()
    next(guard)
    guard.close()  # consume-guard-close
    relayed = list(chain())  # consume-chain
    return seen, first, relayed  # consume-return


print(consume())  # module-call
