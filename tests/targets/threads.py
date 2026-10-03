import threading
import time

import seamtest

sleeping = threading.Event()


def sleeper():
    sleeping.set()
    seamtest.sleep_nogil(1500)  # sleeper-call
    return "slept"


def work(value):
    result = seamtest.add(value, 2)  # work-add
    return result  # work-return


def worker():
    sleeping.wait()
    time.sleep(0.2)
    got = seamtest.call_back(work, 40)  # worker-callback
    return got


def main():
    a = threading.Thread(target=sleeper, name="sleeper")
    b = threading.Thread(target=worker, name="worker")
    a.start()
    b.start()
    b.join()  # main-join
    a.join()
    print("threads done")


main()
