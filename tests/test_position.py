import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.position import _get_trailing_stop_tier, cut_profit


TIERS = [[0.25, 0.01]]


class TrailingStopTierTests(unittest.TestCase):
    def test_starts_at_exactly_twenty_five_percent(self):
        self.assertEqual(
            _get_trailing_stop_tier(100, 125, TIERS, 0.05, 0.01),
            (0.25, 0.01),
        )

    def test_uses_highest_reached_tier(self):
        self.assertEqual(
            _get_trailing_stop_tier(100, 127, TIERS, 0.05, 0.01),
            (0.25, 0.01),
        )
        self.assertEqual(
            _get_trailing_stop_tier(100, 130, TIERS, 0.05, 0.01),
            (0.30, 0.02),
        )
        self.assertEqual(
            _get_trailing_stop_tier(100, 140, TIERS, 0.05, 0.01),
            (0.40, 0.04),
        )

    def test_does_not_start_below_twenty_five_percent(self):
        self.assertIsNone(
            _get_trailing_stop_tier(100, 124.99, TIERS, 0.05, 0.01)
        )


class CutProfitTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "trailing_stop_tiers": TIERS,
            "trailing_stop_gain_step": 0.05,
            "trailing_stop_pullback_step": 0.01,
        }

    @staticmethod
    def _state(price_high):
        return SimpleNamespace(
            position={
                "BTCUSDT": {"openPriceAvg": "100", "holdSide": "long"}
            },
            price_track={"BTCUSDT": {"priceHigh": price_high}},
        )

    @staticmethod
    def _sym_data(price):
        return {"15m": {"data": [[0, 0, 0, 0, str(price)]]}}

    @patch("core.position.notify")
    @patch("core.position.get_config")
    def test_pullback_is_calculated_from_buy_price(
        self, mock_get_config, _mock_notify
    ):
        mock_get_config.return_value = self.config
        orders = []

        closed = cut_profit(
            "BTCUSDT",
            self._sym_data(124),
            self._state(125),
            lambda *args, **kwargs: orders.append((args, kwargs)),
        )

        self.assertTrue(closed)
        self.assertEqual(len(orders), 1)
        self.assertIn("1%", orders[0][1]["close_reason"])

    @patch("core.position.notify")
    @patch("core.position.get_config")
    def test_does_not_close_before_buy_price_pullback_is_reached(
        self, mock_get_config, _mock_notify
    ):
        mock_get_config.return_value = self.config
        orders = []

        closed = cut_profit(
            "BTCUSDT",
            self._sym_data(124.01),
            self._state(125),
            lambda *args, **kwargs: orders.append((args, kwargs)),
        )

        self.assertFalse(closed)
        self.assertEqual(orders, [])


if __name__ == "__main__":
    unittest.main()
