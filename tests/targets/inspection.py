import sys
import threading
import unittest

import seam_inspection as native


def run():
    mixer = native.Mixer()
    native.inspect(1)  # inspection-python
    native.inspect(2)
    native.watched(10)
    native.reuse(20)
    return mixer


if len(sys.argv) > 1 and sys.argv[1] == "thread":
    worker = threading.Thread(target=run, name="worker-0")
    worker.start()
    worker.join()
elif len(sys.argv) > 1 and sys.argv[1] == "library":
    class Trial(unittest.TestCase):
        def runTest(self):
            run()
    unittest.TextTestRunner().run(Trial())
else:
    run()
print("inspection done")
