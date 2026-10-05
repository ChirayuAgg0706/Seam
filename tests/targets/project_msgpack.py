"""Target of the real-project scenarios: msgpack (Cython). python project_msgpack.py [error]"""
import sys

import msgpack


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def encode(obj):
    return {"x": obj.x, "y": obj.y}  # encode-body


def main(mode):
    packer = msgpack.Packer(default=encode)  # build
    data = packer.pack([1, "two", 3.0])  # pack
    packed = packer.pack(Point(1, 2))  # callback
    print("bytes", len(data), len(packed), msgpack.unpackb(data))  # print
    if mode == "error":
        msgpack.unpackb(b"\xc1")  # error


main(sys.argv[1] if len(sys.argv) > 1 else "")  # module-main
