import unittest
from pydantic import ValidationError
from commerce_lab.agent import CartChange, Search, Shipping


class ArgumentTests(unittest.TestCase):
    def test_observed_price_strings_are_normalized_without_changing_value(self):
        for value in ('50', '50.0', 50, 50.0):
            self.assertEqual(Search(query='shirt', max_price_usd=value).max_price_usd, 50)

    def test_quantity_and_price_reject_ambiguous_or_invalid_values(self):
        for value in (True, 'NaN', 'Infinity', '$50', '2 shirts', '1.5'):
            with self.assertRaises(ValidationError):
                CartChange(product_id='us:X', quantity=value)
        for value in (True, 'NaN', '$50', float('inf'), float('nan'), '9' * 400):
            with self.assertRaises(ValidationError):
                Search(query='shirt', max_price_usd=value)

    def test_shipping_numbers_preserve_constraints(self):
        args = Shipping(destination='US', deadline_days='10', shipping_budget_usd='30.0')
        self.assertEqual(args.deadline_days, 10)
        self.assertEqual(args.shipping_budget_usd, 30)


if __name__ == '__main__':
    unittest.main()
