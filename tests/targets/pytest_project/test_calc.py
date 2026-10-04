"""A small test module. Seam's own tests (tests/test_pytest.py) debug it under pytest.

Two of its tests are meant to go wrong: `test_fails` and `test_crashes`.
"""
import pytest
import seamtest


def test_addition():
    a = 2
    b = 3
    total = a + b  # addition-total
    assert total == 5  # addition-assert
    assert (total,
            a) == (5,
                   2), "pytest rewrites this statement"
    label = "sum %d" % total  # addition-after
    assert label == "sum 5"


def test_native(numbers):
    total = seamtest.add(numbers[0], numbers[1])  # native-call
    assert total == 3  # native-after


@pytest.mark.parametrize("value, doubled", [(1, 2), (4, 8), (10, 20)])
def test_doubling(value, doubled):
    result = value * 2  # param-body
    assert result == doubled


class TestGroup:
    def test_method(self, numbers):
        count = len(numbers)  # method-body
        assert count == 3


def test_prints():
    print("printed by a test")  # print-line
    assert True


def helper_that_checks(actual, expected):
    assert actual == expected, "helper says no"  # helper-assert


def test_fails():
    expected = 4
    actual = seamtest.add(1, 2)
    assert actual == expected  # failing-assert


def test_fails_in_a_helper():
    helper_that_checks(seamtest.add(1, 2), 4)  # helper-call


def test_expected_exceptions():
    with pytest.raises(ZeroDivisionError):
        1 / 0  # raises-body
    try:
        int("x")
    except ValueError:
        pass
    pytest.importorskip("no_such_module_for_seam")  # skip-line


def test_crashes():
    seamtest.crash()  # crash-call
