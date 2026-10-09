"""Illustrative example: a checkout bug."""
import seam_checkout
import sys


def checkout(price_cents, discount_percent):
    total_cents = seam_checkout.apply_discount(price_cents, discount_percent)
    return total_cents


def test_checkout_total():
    total_cents = checkout(10_000, 10)
    print(f"Checkout total: ${total_cents / 100:.2f}", flush=True)
    assert total_cents == 9_000, f"Expected $90.00, got ${total_cents / 100:.2f}"
    print("PASS: checkout total is $90.00", flush=True)


if __name__ == "__main__":
    try:
        test_checkout_total()
    except AssertionError as error:
        print(f"FAIL: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
