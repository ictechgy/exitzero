import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from catalog import normalize_items


class CatalogContract(unittest.TestCase):
    def test_exact_error_text(self):
        with self.assertRaises(TypeError) as caught:
            normalize_items(["valid", 3])
        self.assertEqual(str(caught.exception), "items must contain only strings")

    def test_type_order_and_duplicates(self):
        result = normalize_items([" Zebra ", "APPLE", "apple"])
        self.assertIs(type(result), list)
        self.assertEqual(result, ["apple", "zebra"])


if __name__ == "__main__":
    unittest.main()
