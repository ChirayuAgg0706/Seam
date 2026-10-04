"""Test functions for the scenario that debugs pytest running them. The file's name keeps
it out of Seam's own test suite; the scenario hands it to pytest by name."""
import pytest


@pytest.fixture
def base():
    value = 10  # fixture-body
    yield value  # fixture-yield
    value = None  # fixture-teardown


def test_first(base):
    total = base + 1  # first-body
    assert total == 11  # first-assert


def test_second(base):
    total = base + 2  # second-body
    assert total == 12
