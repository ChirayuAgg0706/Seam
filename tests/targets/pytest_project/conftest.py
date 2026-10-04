"""Fixtures of the small project that Seam's pytest scenarios debug."""
import pytest


@pytest.fixture
def numbers():
    values = [1, 2, 3]
    yield values  # fixture-yield
    values.clear()  # fixture-teardown
