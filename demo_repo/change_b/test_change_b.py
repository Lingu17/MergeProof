import unittest
from service import checkout


class LoyaltyTests(unittest.TestCase):
    def test_member_gets_discount(self):
        self.assertEqual(checkout({"subtotal": 100, "loyalty": True}), 90.0)


if __name__ == "__main__":
    unittest.main()
