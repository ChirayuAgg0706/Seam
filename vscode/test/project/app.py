"""A small order calculator: Python that calls Rust (examples/pyo3-demo's seam_demo).

The editor check copies this into a fresh project folder and debugs it in VS Code.
"""
import sys
from dataclasses import dataclass, field

import seam_demo


@dataclass
class Order:
    customer: str
    quantities: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)


def total(order):
    limit = len(order.quantities)
    result = seam_demo.sum_squares(limit)  # step in here
    return result


def check(order):
    share = total(order) / order.notes["discount"]  # raises here
    return share


def main():
    order = Order("Ada", [3, 1, 4, 1, 5], {"discount": 0, "gift": True})
    print("interpreter:", sys.executable)
    print("total:", total(order))
    if sys.argv[1:] != ["--no-check"]:
        check(order)


main()
