import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
os.environ["LEDGER_DATA"] = tempfile.mkdtemp()
import ledger as L  # noqa: E402


def series(n, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


def fresh():
    return {"cash": 300.0, "units": {"BTC": 0.0, "ETH": 0.0}}


class Tests(unittest.TestCase):
    def test_uptrend_goes_long_capped(self):
        w, info = L.target_weight(series(130))
        self.assertTrue(info["long"])
        self.assertLessEqual(w, L.CAP)
        self.assertGreater(w, 0)

    def test_downtrend_flat(self):
        w, info = L.target_weight(series(130, 200.0, -0.5))
        self.assertEqual(w, 0.0)
        self.assertFalse(info["long"])

    def test_not_enough_history(self):
        with self.assertRaises(ValueError):
            L.target_weight(series(50))

    def test_high_vol_reduces_weight(self):
        calm = L.target_weight([100 + i * 0.3 for i in range(130)])[0]
        wild = [100 + i * 0.3 + (15 if i % 2 else -15) for i in range(130)]
        self.assertLessEqual(L.target_weight(wild)[0], calm)

    def test_buy_then_sell_round_trip_costs_money(self):
        s = fresh()
        eq = 300.0
        side, units = L.plan_trade(s, "BTC", 0.30, 100.0, 101.0, eq)
        self.assertEqual(side, "buy")
        L.execute(s, "BTC", side, units, 100.0, 101.0, "t", "x")
        self.assertGreater(s["units"]["BTC"], 0)
        L.execute(s, "BTC", "sell", s["units"]["BTC"], 100.0, 101.0, "t", "x")
        self.assertLess(s["cash"], 300.0)          # fees + spread + slippage
        self.assertAlmostEqual(s["units"]["BTC"], 0.0, places=9)

    def test_small_change_no_trade(self):
        s = fresh()
        s["units"]["BTC"] = 0.9
        side, _ = L.plan_trade(s, "BTC", 0.31, 100.0, 100.5, 300.0)  # cur w = 0.30
        self.assertIsNone(side)

    def test_never_spends_more_than_cash(self):
        s = {"cash": 20.0, "units": {"BTC": 0.0, "ETH": 0.0}}
        side, units = L.plan_trade(s, "BTC", 0.33, 100.0, 100.0, 300.0)
        if side:
            L.execute(s, "BTC", side, units, 100.0, 100.0, "t", "x")
        self.assertGreaterEqual(s["cash"], -1e-9)

    def test_dust_skipped(self):
        s = fresh()
        side, _ = L.plan_trade(s, "BTC", 0.04, 100.0, 100.0, 300.0)
        self.assertIsNone(side)

    def test_daily_halt(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        s = {"day": "2026-10-07", "day_start": 300.0, "week": "2026-10-05", "week_start": 300.0, "peak": 300.0}
        self.assertIn("daily", L.check_halts(s, 293.0, now))
        self.assertEqual(s["halt_until"], "2026-10-08")

    def test_drawdown_manual_halt_sticks(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        s = {"day": "2026-10-07", "day_start": 255.0, "week": "2026-10-05", "week_start": 255.0, "peak": 300.0}
        self.assertIn("drawdown", L.check_halts(s, 254.0, now))
        self.assertTrue(s["manual_halt"])
        self.assertIn("manual", L.check_halts(s, 300.0, now))

    def test_no_halt_normal(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        s = {"day": "2026-10-07", "day_start": 300.0, "week": "2026-10-05", "week_start": 300.0, "peak": 300.0}
        self.assertIsNone(L.check_halts(s, 299.0, now))


class FaultInjection(unittest.TestCase):
    """End-to-end run() with fake network: idempotency, stale/jump rejection, error halt."""

    def setUp(self):
        L.DATA = Path(tempfile.mkdtemp())
        L.KILL_FILE = Path(tempfile.mkdtemp()) / "KILL"
        self.now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.mid = {"BTC": 400000.0, "ETH": 15000.0}
        self.fail = False
        self.age = 5.0
        L.time.sleep = lambda *_: None
        L.luno_quote = self._quote
        L.closed_daily_closes = lambda a, now: (series(130, 100.0, 0.5), "2026-10-06", "fake")

    def _quote(self, a, now):
        if self.fail:
            raise RuntimeError("api down")
        m = self.mid[a]
        return m * 0.999, m * 1.001, self.age

    def test_buys_once_then_idempotent(self):
        self.assertEqual(L.run(self.now), 0)
        st = L.load_state()
        self.assertGreater(st["units"]["BTC"], 0)
        n1 = (L.DATA / "trades.csv").read_text().count("\n")
        L.run(self.now)
        n2 = (L.DATA / "trades.csv").read_text().count("\n")
        self.assertEqual(n1, n2)  # same closed bar -> no duplicate orders

    def test_stale_quote_rejected(self):
        self.age = 9999
        self.assertEqual(L.run(self.now), 1)
        self.assertEqual(L.load_state()["units"]["BTC"], 0.0)

    def test_jump_rejected(self):
        L.run(self.now)
        self.mid["BTC"] *= 1.5
        self.assertEqual(L.run(self.now), 1)

    def test_three_errors_halt(self):
        self.fail = True
        for _ in range(3):
            L.run(self.now)
        self.assertTrue(L.load_state()["manual_halt"])

    def test_kill_file_flattens(self):
        L.run(self.now)
        L.KILL_FILE.write_text("x")
        L.run(self.now)
        st = L.load_state()
        self.assertEqual(st["units"]["BTC"], 0.0)
        self.assertEqual(st["units"]["ETH"], 0.0)


if __name__ == "__main__":
    unittest.main()
