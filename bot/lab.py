"""Ledger Lab - PAPER ONLY. No API keys needed for crypto, no orders.

Runs several long-only spot strategies side by side on the same data, with realistic
costs, and reports two things:
  backtest : the whole history (signals use closed bars only; trades fill one bar later)
  forward  : a fresh paper account per strategy started on FWD_START (the honest test)
Stateless: every run rebuilds both from fetched history, so it is idempotent.
"""
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger as L  # noqa: E402  (reuses http_json + notify)

DATA = Path(os.environ.get("LEDGER_DATA", "docs/data"))
START = 100.0                # RM per strategy account
FWD_START = "2026-10-05"     # forward paper test starts here
WARMUP = 120                 # bars before the first backtest trade
REBAL_PP = 0.05
MIN_TRADE = 5.0              # RM

GROUPS = {
    # costs are ASSUMPTIONS: Luno ~0.3-0.35% per side + slippage; Moomoo MY ~USD0.99 per order (~RM4.2)
    "crypto": {"assets": ["BTC", "ETH", "SOL", "LINK", "DOGE"], "fee_pct": 0.0035, "slip": 0.002,
               "fee_flat": 0.0, "ann": 365, "base": ["BTC", "ETH"]},
    "us": {"assets": ["SPY", "QQQ", "GLD"], "fee_pct": 0.0, "slip": 0.001,
           "fee_flat": 4.2, "ann": 252, "base": ["SPY", "QQQ", "GLD"]},
}


# ---------- indicators ----------
def sma(x, n):
    return sum(x[-n:]) / n


def vol(x, ann, n=20):
    r = [math.log(x[i] / x[i - 1]) for i in range(len(x) - n, len(x))]
    return statistics.pstdev(r) * math.sqrt(ann)


# ---------- strategies: f(hist, st) -> {asset: weight}; hist = closes up to and including today ----------
def hold_eq(hist, st):
    return {a: 1.0 / len(hist) for a in hist}


def trend_base(hist, st):
    """The live bot's rule: close > 100d SMA, vol-targeted, cap 33% per asset."""
    w = {a: 0.0 for a in hist}
    for a in st["base"]:
        x = hist[a]
        if len(x) > 100 and x[-1] > sma(x, 100):
            v = vol(x, st["ann"])
            w[a] = 0.33 if v <= 0 else min(0.33, 0.15 / v)
    return w


def trend_bold(hist, st):
    """Fully invested: equal share to every asset above its 50d SMA."""
    n = len(hist)
    return {a: (1.0 / n if len(x) > 50 and x[-1] > sma(x, 50) else 0.0) for a, x in hist.items()}


def trend_vol(hist, st):
    """Bolder vol-targeting: 100d trend, 40% vol target, cap 50% per asset, total <= 100%."""
    w = {}
    for a, x in hist.items():
        if len(x) > 100 and x[-1] > sma(x, 100):
            v = vol(x, st["ann"])
            w[a] = 0.5 if v <= 0 else min(0.5, 0.40 / v)
        else:
            w[a] = 0.0
    tot = sum(w.values())
    return {a: (v / tot if tot > 1 else v) for a, v in w.items()}


def momo2(hist, st):
    """Weekly: hold the top 2 assets by 30d return if positive, else cash."""
    st["n"] = st.get("n", -1) + 1
    if st["n"] % 7 == 0 or "w" not in st:
        ranked = sorted(((x[-1] / x[-31] - 1, a) for a, x in hist.items() if len(x) > 31), reverse=True)
        top = [a for r, a in ranked[:2] if r > 0]
        st["w"] = {a: (0.5 if a in top else 0.0) for a in hist}
    return dict(st["w"])


def breakout(hist, st):
    """Donchian: enter on a new 20d high, exit on a new 10d low; equal split of held, max 50% each."""
    held = st.setdefault("in", {})
    for a, x in hist.items():
        if len(x) < 22:
            continue
        if not held.get(a) and x[-1] > max(x[-21:-1]):
            held[a] = True
        elif held.get(a) and x[-1] < min(x[-11:-1]):
            held[a] = False
    on = [a for a in hist if held.get(a)]
    return {a: (min(0.5, 1.0 / len(on)) if a in on else 0.0) for a in hist}


STRATEGIES = {"hold_eq": hold_eq, "trend_base": trend_base, "trend_bold": trend_bold,
              "trend_vol": trend_vol, "momo2": momo2, "breakout": breakout}


# ---------- engine ----------
def rebalance(cfg, cash, units, px, weights):
    """Move toward target weights at prices px. Returns (cash, trades)."""
    eq = cash + sum(units[a] * px[a] for a in units)
    trades = 0
    for a in units:  # sells first
        cur = units[a] * px[a] / eq if eq > 0 else 0.0
        w = weights.get(a, 0.0)
        if units[a] > 0 and (w == 0.0 or cur - w >= REBAL_PP):
            val = units[a] * px[a] if w == 0.0 else (cur - w) * eq
            if val < MIN_TRADE and w != 0.0:
                continue
            n = units[a] if w == 0.0 else min(units[a], val / px[a])
            got = n * px[a] * (1 - cfg["slip"])
            cash += got - got * cfg["fee_pct"] - cfg["fee_flat"]
            units[a] -= n
            trades += 1
    for a in units:  # then buys
        cur = units[a] * px[a] / eq if eq > 0 else 0.0
        w = weights.get(a, 0.0)
        if w - cur >= REBAL_PP:
            spend = min((w - cur) * eq, (cash - cfg["fee_flat"]) / (1 + cfg["fee_pct"]))
            if spend < MIN_TRADE:
                continue
            units[a] += spend / (px[a] * (1 + cfg["slip"]))
            cash -= spend * (1 + cfg["fee_pct"]) + cfg["fee_flat"]
            trades += 1
    return cash, trades


def simulate(cfg, dates, closes, strat, start_i):
    """Signal at close of bar t, fill at close of bar t+1. Account starts at bar start_i."""
    cash, units, st = START, {a: 0.0 for a in closes}, {"ann": cfg["ann"], "base": cfg["base"]}
    pending, curve, trades = None, [], 0
    for t in range(max(start_i - 1, 0), len(dates)):
        if t >= start_i:
            px = {a: closes[a][t] for a in closes}
            if pending is not None:
                cash, k = rebalance(cfg, cash, units, px, pending)
                trades += k
            curve.append(cash + sum(units[a] * px[a] for a in units))
        pending = strat({a: closes[a][:t + 1] for a in closes}, st)
    last = {a: round((units[a] * closes[a][-1]) / curve[-1], 3) for a in units} if curve else {}
    return curve, trades, last


def metrics(curve, trades, dates):
    if len(curve) < 2:
        return {"days": len(curve), "equity": round(curve[-1], 2) if curve else START, "ret": 0.0,
                "cagr": 0.0, "maxdd": 0.0, "trades": trades}
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    d0 = datetime.strptime(dates[-len(curve)], "%Y-%m-%d")
    d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
    yrs = max((d1 - d0).days, 1) / 365.0
    return {"days": len(curve), "equity": round(curve[-1], 2), "ret": round(curve[-1] / START - 1, 4),
            "cagr": round((curve[-1] / START) ** (1 / yrs) - 1, 4) if yrs >= 0.5 else None,
            "maxdd": round(mdd, 4), "trades": trades}


# ---------- data ----------
def fetch_crypto(asset, today, years=5):
    """{date: close} from Coinbase, closed candles only, paged 300 bars at a time."""
    out, end = {}, datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    midnight = int(end.timestamp())
    for _ in range(years * 365 // 290 + 1):
        start = end - timedelta(days=299)
        q = urllib.parse.urlencode({"granularity": 86400, "start": start.isoformat(), "end": end.isoformat()})
        rows = L.http_json("https://api.exchange.coinbase.com/products/%s-USD/candles?%s" % (asset, q))
        if not rows:
            break
        for r in rows:
            if r[0] < midnight:
                out[datetime.fromtimestamp(r[0], timezone.utc).strftime("%Y-%m-%d")] = float(r[4])
        end = start - timedelta(days=1)
        time.sleep(0.4)
    return out


def fetch_us(sym, key):
    d = L.http_json("https://api.twelvedata.com/time_series?symbol=%s&interval=1day&outputsize=1500&apikey=%s"
                    % (sym, key))
    if "values" not in d:
        raise RuntimeError("twelvedata %s: %s" % (sym, str(d)[:120]))
    return {v["datetime"][:10]: float(v["close"]) for v in d["values"]}


def align(series):
    dates = sorted(set.intersection(*[set(s) for s in series.values()]))
    return dates, {a: [s[d] for d in dates] for a, s in series.items()}


# ---------- run ----------
def run_group(name, cfg, series, now):
    dates, closes = align(series)
    fi = next((i for i, d in enumerate(dates) if d >= FWD_START), None)
    res = {"assets": cfg["assets"], "from": dates[0], "to": dates[-1], "bars": len(dates),
           "backtest": {}, "forward": {"start": FWD_START, "strategies": {}}, "curve": {"dates": [], "s": {}}}
    for sname, f in STRATEGIES.items():
        c, tr, _ = simulate(cfg, dates, closes, f, WARMUP)
        res["backtest"][sname] = metrics(c, tr, dates)
        if fi is not None:
            c, tr, w = simulate(cfg, dates, closes, f, fi)
            m = metrics(c, tr, dates)
            m["weights"] = w
            res["forward"]["strategies"][sname] = m
            res["curve"]["s"][sname] = [round(v, 2) for v in c]
    if fi is not None:
        res["curve"]["dates"] = dates[fi:]
    return res


def summary(out):
    lines = ["ledger-lab " + out["generated"][:10]]
    for g, r in out["groups"].items():
        fw = r["forward"]["strategies"]
        if fw:
            lines.append("%s forward (RM100 each): " % g + ", ".join(
                "%s %.2f" % (k, v["equity"]) for k, v in fw.items()))
        else:
            bt = r["backtest"]
            lines.append("%s backtest %s..%s: " % (g, r["from"], r["to"]) + ", ".join(
                "%s %+.0f%%/dd %.0f%%" % (k, v["ret"] * 100, v["maxdd"] * 100) for k, v in bt.items()))
    return "\n".join(lines)


def main():
    now = datetime.now(timezone.utc)
    out = {"generated": now.isoformat(timespec="seconds"), "note": "PAPER ONLY. Costs are assumptions.",
           "groups": {}}
    cfg = GROUPS["crypto"]
    series = {a: fetch_crypto(a, now.date()) for a in cfg["assets"]}
    out["groups"]["crypto"] = run_group("crypto", cfg, series, now)
    key = os.environ.get("TWELVE_DATA_KEY")
    if key:
        try:
            cfg = GROUPS["us"]
            series = {}
            for a in cfg["assets"]:
                series[a] = fetch_us(a, key)
                time.sleep(8)  # free plan: 8 requests/min
            out["groups"]["us"] = run_group("us", cfg, series, now)
        except Exception as e:  # noqa: BLE001
            print("us group failed:", type(e).__name__, str(e).replace(key, "***"))
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "lab.json").write_text(json.dumps(out, indent=1, sort_keys=True))
    text = summary(out)
    print(text)
    if os.environ.get("LAB_NOTIFY") == "1":
        L.notify(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
