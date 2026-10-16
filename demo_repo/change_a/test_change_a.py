import unittest
from service import checkout


class CurrencyNormalizationTests(unittest.TestCase):
    def test_euro_is_normalized(self):
        self.assertEqual(checkout({"subtotal": 100, "currency": "EUR"}), 108.0)


if __name__ == "__main__":
    unittest.main()
