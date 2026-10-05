"""Target of the real-project scenarios: contourpy (pybind11).

python project_contourpy.py [error]
"""
import sys

import numpy as np
from contourpy import contour_generator


def main(mode):
    z = np.array([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]])
    generator = contour_generator(z=z)  # build
    lines = generator.lines(0.5)  # lines
    print("lines", len(lines), "points", len(lines[0]))  # print
    if mode == "error":
        generator.filled(1.0, 0.0)  # error


main(sys.argv[1] if len(sys.argv) > 1 else "")  # module-main
