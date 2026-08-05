import unittest
from decimal import Decimal

from core.order import _post_open_extra_margin


class PostOpenExtraMarginTests(unittest.TestCase):
    def test_adds_one_and_a_half_times_initial_margin(self):
        detail = {"data": {"priceAvg": "100", "baseVolume": "2"}}
        cfg = {"auto_trade": {"post_open_extra_margin_multiplier": 1.5}}

        self.assertEqual(
            _post_open_extra_margin(detail, 10, cfg),
            Decimal("30.0"),
        )

    def test_is_disabled_when_multiplier_is_missing(self):
        detail = {"data": {"priceAvg": "100", "baseVolume": "2"}}

        self.assertEqual(
            _post_open_extra_margin(detail, 10, {"auto_trade": {}}),
            Decimal("0"),
        )


if __name__ == "__main__":
    unittest.main()
