"""Uses real third-party wheels that ship without debug info: numpy (C) and orjson (Rust)."""
import sys
import time

import numpy as np
import orjson


def scale(x):
    doubled = x * 2  # scale-body
    return doubled


def use_numpy():
    values = np.arange(4)
    scaled = np.vectorize(scale)(values)  # numpy-call
    return int(scaled.sum())


class Odd:
    pass


def encode(obj):
    name = type(obj).__name__  # encode-body
    return {"odd": name}


def use_orjson():
    text = orjson.dumps({"value": Odd()}, default=encode)  # orjson-call
    return text.decode()


def busy(seconds):
    data = np.random.default_rng(0).random(2_000_000)
    print("ready to pause", flush=True)
    end = time.monotonic() + seconds
    rounds = 0
    while time.monotonic() < end:
        np.sort(data)  # busy-sort
        rounds += 1
    return rounds


def bad_json():
    try:
        orjson.loads("{bad")  # error-call
    except ValueError as exc:
        return type(exc).__name__


mode = sys.argv[1]
if mode == "callbacks":
    print("numpy", use_numpy(), flush=True)  # module-numpy
    print("orjson", use_orjson(), flush=True)  # module-orjson
elif mode == "busy":
    print("rounds", busy(float(sys.argv[2])) > 0, flush=True)  # module-busy
elif mode == "error":
    print("caught", bad_json(), flush=True)
print("done", flush=True)
