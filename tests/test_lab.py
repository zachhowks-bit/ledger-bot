import math
import os
import random
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
os.environ["LEDGER_DATA"] = tempfile.mkdtemp()
import lab as X  # noqa: E402

CFG = X.GROUPS["crypto"]


def walk(n, drift=0.0, sigma=0.03, seed=1, p0=100.0):
    r = random.Random(seed)
    p, out = p0, []
    for _ in range(n):
        p *= math.exp(drift + sigma * r.gauss(0, 1))
        out.append(p)
    return out


def dates(n):
    d0 = date(2022, 1, 1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def data(n=400, drift=0.0):
    return {a: walk(n, drift, 0.03, seed=i) for i, a in enumerate(CFG["assets"])}


class LabTests(unittest.TestCase):
    def test_all_strategies_weights_valid(self):
        d = data()
        for name, f in X.STRATEGIES.items():
            st = {"ann": 365, "base": CFG["base"]}
            for t in range(120, 300):
                w = f({a: x[:t + 1] for a, x in d.items()}, st)
                self.assertTrue(all(0.0 <= v <= 1.0 + 1e-9 for v in w.values()), name)
                self.assertLessEqual(sum(w.values()), 1.0 + 1e-9, name)

    def test_no_lookahead(self):
        d = data()
        d2 = {a: x[:250] + [v * 50 for v in x[250:]] for a, x in d.items()}  # change only the future
        for name, f in X.STRATEGIES.items():
            s1, s2 = {"ann": 365, "base": CFG["base"]}, {"ann": 365, "base": CFG["base"]}
            for t in range(120, 250):
                w1 = f({a: x[:t + 1] for a, x in d.items()}, s1)
                w2 = f({a: x[:t + 1] for a, x in d2.items()}, s2)
                self.assertEqual(w1, w2, name)

    def test_cash_never_negative_and_costs_bite(self):
        d = data()
        for name, f in X.STRATEGIES.items():
            curve, trades, _ = X.simulate(CFG, dates(400), d, f, X.WARMUP)
            self.assertTrue(all(v > 0 for v in curve), name)
        free = dict(CFG, fee_pct=0.0, slip=0.0)
        c1, t1, _ = X.simulate(CFG, dates(400), d, X.breakout, X.WARMUP)
        c0, t0, _ = X.simulate(free, dates(400), d, X.breakout, X.WARMUP)
        if t1:
            self.assertLess(c1[-1], c0[-1])

    def test_buy_and_hold_tracks_uptrend(self):
        d = {a: [100 * 1.01 ** i for i in range(300)] for a in CFG["assets"]}
        curve, _, _ = X.simulate(CFG, dates(300), d, X.hold_eq, X.WARMUP)
        self.assertGreater(curve[-1], X.START * 2)

    def test_downtrend_trend_rules_stay_flat(self):
        d = {a: [200 * 0.995 ** i for i in range(300)] for a in CFG["assets"]}
        for f in (X.trend_base, X.trend_bold, X.trend_vol):
            curve, trades, _ = X.simulate(CFG, dates(300), d, f, X.WARMUP)
            self.assertEqual(trades, 0)
            self.assertAlmostEqual(curve[-1], X.START, places=6)

    def test_flat_fee_hurts_small_account(self):
        us = X.GROUPS["us"]
        d = {a: [100 + 0.3 * i + (3 if i % 2 else -3) for i in range(300)] for a in us["assets"]}
        c, tr, _ = X.simulate(us, dates(300), d, X.breakout, X.WARMUP)
        if tr:
            self.assertLess(c[-1], X.START * 1.5)

    def test_metrics_and_forward_split(self):
        d = data(400, 0.002)
        series = {a: dict(zip(dates(400), x)) for a, x in d.items()}
        X.FWD_START = dates(400)[350]
        res = X.run_group("crypto", CFG, series, None)
        self.assertEqual(set(res["backtest"]), set(X.STRATEGIES))
        fw = res["forward"]["strategies"]["hold_eq"]
        self.assertEqual(fw["days"], 400 - 350)
        self.assertLessEqual(res["backtest"]["trend_base"]["maxdd"], 0.0)

    def test_forward_empty_before_start(self):
        d = data(300)
        series = {a: dict(zip(dates(300), x)) for a, x in d.items()}
        X.FWD_START = "2999-01-01"
        res = X.run_group("crypto", CFG, series, None)
        self.assertEqual(res["forward"]["strategies"], {})


if __name__ == "__main__":
    unittest.main()
