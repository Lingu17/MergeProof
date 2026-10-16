import unittest
from service import checkout


class CombinedCheckoutTests(unittest.TestCase):
    def test_euro_member_checkout_uses_normalized_total(self):
        self.assertEqual(checkout({"subtotal": 100, "currency": "EUR", "loyalty": True}), 97.2)


if __name__ == "__main__":
    unittest.main()
