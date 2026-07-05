import unittest

from flipping.ge_tax import TAX_CAP, flip_margin, post_tax_proceeds, sale_tax


class TestSaleTax(unittest.TestCase):
    def test_two_percent_rounds_down(self):
        self.assertEqual(sale_tax(1000), 20)
        self.assertEqual(sale_tax(999), 19)   # 19.98 -> 19
        self.assertEqual(sale_tax(101), 2)    # 2.02 -> 2
        self.assertEqual(sale_tax(149), 2)    # 2.98 -> 2

    def test_under_50gp_is_tax_free(self):
        self.assertEqual(sale_tax(49), 0)     # 0.98 -> 0
        self.assertEqual(sale_tax(1), 0)
        self.assertEqual(sale_tax(50), 1)     # first taxed price

    def test_cap_at_5m(self):
        self.assertEqual(sale_tax(250_000_000), TAX_CAP)   # 2% would be 5m exactly
        self.assertEqual(sale_tax(1_000_000_000), TAX_CAP)
        self.assertEqual(sale_tax(249_999_999), 4_999_999)

    def test_exempt_items(self):
        self.assertEqual(sale_tax(10_000, "Old school bond"), 0)
        self.assertEqual(sale_tax(500, "Lobster"), 0)
        self.assertEqual(sale_tax(500, "Abyssal whip"), 10)


class TestMargin(unittest.TestCase):
    def test_post_tax_proceeds(self):
        self.assertEqual(post_tax_proceeds(1000), 980)
        self.assertEqual(post_tax_proceeds(49), 49)

    def test_flip_margin(self):
        # buy 950, sell 1000: proceeds 980, profit 30
        self.assertEqual(flip_margin(950, 1000), 30)

    def test_flip_margin_can_be_negative(self):
        # tax turns a 10gp gross spread into a loss
        self.assertEqual(flip_margin(995, 1000), -15)

    def test_exempt_margin(self):
        self.assertEqual(flip_margin(950, 1000, "Lobster"), 50)


if __name__ == "__main__":
    unittest.main()
