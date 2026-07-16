import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.position import _get_trailing_stop_tier, cut_profit


TIERS = [[0.11, 0.01], [0.15, 0.02]]


class TrailingStopTierTests(unittest.TestCase):
    def test_starts_at_exactly_eleven_percent(self):
        self.assertEqual(
            _get_trailing_stop_tier(100, 111, TIERS, 0.05, 0.01),
            (0.11, 0.01),
        )

    def test_uses_highest_reached_tier(self):
        self.assertEqual(
            _get_trailing_stop_tier(100, 117, TIERS, 0.05, 0.01),
            (0.15, 0.02),
        )
        self.assertEqual(
            _get_trailing_stop_tier(100, 120, TIERS, 0.05, 0.01),
            (0.20, 0.03),
        )
        self.assertEqual(
            _get_trailing_stop_tier(100, 130, TIERS, 0.05, 0.01),
            (0.30, 0.05),
        )

    def test_does_not_start_below_eleven_percent(self):
        self.assertIsNone(
            _get_trailing_stop_tier(100, 110.99, TIERS, 0.05, 0.01)
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
            self._sym_data(115),
            self._state(117),
            lambda *args, **kwargs: orders.append((args, kwargs)),
        )

        self.assertTrue(closed)
        self.assertEqual(len(orders), 1)
        self.assertIn("按买入价回撤2%", orders[0][1]["close_reason"])

    @patch("core.position.notify")
    @patch("core.position.get_config")
    def test_does_not_close_before_buy_price_pullback_is_reached(
        self, mock_get_config, _mock_notify
    ):
        mock_get_config.return_value = self.config
        orders = []

        closed = cut_profit(
            "BTCUSDT",
            self._sym_data(115.01),
            self._state(117),
            lambda *args, **kwargs: orders.append((args, kwargs)),
        )

        self.assertFalse(closed)
        self.assertEqual(orders, [])


if __name__ == "__main__":
    unittest.main()
