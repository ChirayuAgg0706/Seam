"""Test functions run by a test runner (python -m unittest unit_cases): every frame
between them is the runner's, none is the user's."""
import unittest


class Cases(unittest.TestCase):
    def setUp(self):
        self.base = 10  # cases-setup

    def test_first(self):
        value = self.base + 1  # first-body
        self.assertEqual(value, 11)  # first-assert

    def test_second(self):
        value = self.base + 2  # second-body
        self.assertEqual(value, 12)
