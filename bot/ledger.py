"""Ledger Bot v2 - PAPER trading only. No API keys, no real orders, ever.

Strategy: long-only daily trend (close > 100d SMA) on BTC and ETH, sized by
volatility target, filled in a simulator at real Luno MYR bid/ask plus fee and
slippage assumptions. Designed to run from GitHub Actions every ~6h; it is
idempotent (acts once per closed daily bar) and marks to market every run.

All numbers below are ASSUMPTIONS to be tested, not facts (see proposal doc).
"""
import csv
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(os.environ.get("LEDGER_DATA", "docs/data"))
KILL_FILE = Path(os.environ.get("LEDGER_KILL", "KILL"))

START_CASH = 300.0          # RM, paper
FEE = 0.0035                # per side (assumption: Luno 0.30-0.35%)
SLIP = 0.002                # assumption
SMA_N = 100
VOL_N = 20
VOL_TARGET = 0.15           # annualised
CAP = 0.33                  # max weight per asset
REBAL_PP = 0.05             # rebalance when weight differs by >= 5 percentage points
MIN_TRADE = 10.0            # RM, skip dust (Luno minimums UNVERIFIED)
DAILY_HALT = -0.02
WEEKLY_HALT = -0.05
DD_HALT = -0.15
MAX_JUMP = 0.10             # reject >10% move vs last seen mid
MAX_SPREAD = 0.005
STALE_SECONDS = 600
MAX_ERRORS = 3

ASSETS = {
    "BTC": {"luno": "XBTMYR", "cb": "BTC-USD", "kr": "XBTUSD"},
    "ETH": {"luno": "ETHMYR", "cb": "ETH-USD", "kr": "ETHUSD"},
}


# ---------- pure logic (unit tested) ----------
def target_weight(closes):
    """Return (weight, info) from CLOSED daily closes, oldest first."""
    if len(closes) < SMA_N + 1:
        raise ValueError("not enough history: %d closes" % len(closes))
    sma = sum(closes[-SMA_N:]) / SMA_N
    last = closes[-1]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - VOL_N, len(closes))]
    vol = statistics.pstdev(rets) * math.sqrt(365)
    if last <= sma:
        return 0.0, {"sma": sma, "last": last, "vol": vol, "long": False}
    w = CAP if vol <= 0 else min(CAP, VOL_TARGET / vol)
    return w, {"sma": sma, "last": last, "vol": vol, "long": True}


def equity_of(state, mids):
    return state["cash"] + sum(state["units"][a] * mids[a] for a in ASSETS)


def plan_trade(state, asset, weight, bid, ask, equity):
    """Return ('buy'|'sell'|None, units) given target weight."""
    units = state["units"][asset]
    cur_w = units * bid / equity if equity > 0 else 0.0
    if weight == 0.0 and units > 0:
        return "sell", units
    if abs(weight - cur_w) < REBAL_PP:
        return None, 0.0
    if weight > cur_w:
        spend = min((weight - cur_w) * equity, state["cash"] / (1 + FEE))
        if spend < MIN_TRADE:
            return None, 0.0
        px = ask * (1 + SLIP)
        return "buy", spend / px
    sell_value = (cur_w - weight) * equity
    if sell_value < MIN_TRADE:
        return None, 0.0
    return "sell", min(units, sell_value / bid)


def execute(state, asset, side, units, bid, ask, now_iso, reason):
    if side == "buy":
        px = ask * (1 + SLIP)
        cost = units * px
        fee = cost * FEE
        state["cash"] -= cost + fee
        state["units"][asset] += units
    else:
        px = bid * (1 - SLIP)
        proceeds = units * px
        fee = proceeds * FEE
        state["cash"] += proceeds - fee
        state["units"][asset] -= units
    return {"ts": now_iso, "asset": asset, "side": side, "units": round(units, 8),
            "price": round(px, 2), "fee": round(fee, 4), "cash_after": round(state["cash"], 2),
            "reason": reason}


def check_halts(state, equity, now):
    """Update peak/day/week anchors; return halt reason or None."""
    today = now.strftime("%Y-%m-%d")
    monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"], state["day_start"] = today, equity
    if state.get("week") != monday:
        state["week"], state["week_start"] = monday, equity
    state["peak"] = max(state.get("peak", equity), equity)
    if state.get("manual_halt"):
        return "manual halt (drawdown) - edit state.json to resume"
    if equity / state["peak"] - 1 <= DD_HALT:
        state["manual_halt"] = True
        return "max drawdown %.1f%% reached" % ((equity / state["peak"] - 1) * 100)
    if state.get("halt_until") and today < state["halt_until"]:
        return "halted until %s" % state["halt_until"]
    if equity / state["day_start"] - 1 <= DAILY_HALT:
        state["halt_until"] = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        return "daily loss limit"
    if equity / state["week_start"] - 1 <= WEEKLY_HALT:
        nxt = now + timedelta(days=7 - now.weekday())
        state["halt_until"] = nxt.strftime("%Y-%m-%d")
        return "weekly loss limit"
    return None


# ---------- IO ----------
def http_json(url, timeout=15, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ledger-bot/2 (paper)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError("fetch failed %s: %s" % (url, last))


def closed_daily_closes(asset, now):
    """Closed daily closes in USD (oldest first); Coinbase, fallback Kraken."""
    midnight = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    cfg = ASSETS[asset]
    try:
        rows = http_json("https://api.exchange.coinbase.com/products/%s/candles?granularity=86400" % cfg["cb"])
        rows = sorted([r for r in rows if r[0] < midnight], key=lambda r: r[0])
        closes, last_ts = [r[4] for r in rows], rows[-1][0]
        src = "coinbase"
    except Exception:  # noqa: BLE001
        d = http_json("https://api.kraken.com/0/public/OHLC?pair=%s&interval=1440" % cfg["kr"])
        key = [k for k in d["result"] if k != "last"][0]
        rows = [r for r in d["result"][key] if r[0] < midnight]
        closes, last_ts = [float(r[4]) for r in rows], rows[-1][0]
        src = "kraken"
    return closes, datetime.fromtimestamp(last_ts, timezone.utc).strftime("%Y-%m-%d"), src


def luno_quote(asset, now):
    d = http_json("https://api.luno.com/api/1/ticker?pair=%s" % ASSETS[asset]["luno"])
    bid, ask = float(d["bid"]), float(d["ask"])
    age = now.timestamp() - d["timestamp"] / 1000.0
    return bid, ask, age


def notify(msg):
    print("NOTIFY:", msg)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            req = urllib.request.Request("https://ntfy.sh/" + topic, data=msg.encode(), method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:  # noqa: BLE001
            print("ntfy failed:", e)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
            urllib.request.urlopen(urllib.request.Request(
                "https://api.telegram.org/bot" + tok + "/sendMessage", data=data), timeout=10)
        except Exception as e:  # noqa: BLE001
            print("telegram failed:", type(e).__name__)  # never print the URL: it holds the token


def load_state():
    p = DATA / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"cash": START_CASH, "units": {a: 0.0 for a in ASSETS}, "errors": 0, "last_bar": {},
            "last_mid": {}, "runs": 0, "started": datetime.now(timezone.utc).isoformat()}


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "state.json").write_text(json.dumps(state, indent=1, sort_keys=True))


def append_csv(name, row, header):
    p = DATA / name
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new:
            w.writeheader()
        w.writerow(row)


TRADE_HDR = ["ts", "asset", "side", "units", "price", "fee", "cash_after", "reason"]
EQ_HDR = ["ts", "equity", "cash", "btc_units", "eth_units", "btc_mid", "eth_mid", "status"]


def run(now=None):
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state = load_state()
    state["runs"] += 1
    state["last_run"] = now_iso
    status = "ok"
    try:
        quotes = {}
        for a in ASSETS:
            bid, ask, age = luno_quote(a, now)
            mid = (bid + ask) / 2
            if age > STALE_SECONDS:
                raise RuntimeError("%s stale quote (%.0fs)" % (a, age))
            last = state["last_mid"].get(a)
            if last and abs(mid / last - 1) > MAX_JUMP:
                raise RuntimeError("%s price jump %.1f%% rejected" % (a, (mid / last - 1) * 100))
            quotes[a] = (bid, ask, mid)
        mids = {a: quotes[a][2] for a in ASSETS}
        equity = equity_of(state, mids)
        halt = check_halts(state, equity, now)
        killed = KILL_FILE.exists()
        flatten = bool(halt) or killed
        if halt:
            status = "HALT: " + halt
            notify("ledger-bot " + status)
        if killed:
            status = "KILL file present"
        for a in ASSETS:
            bid, ask, mid = quotes[a]
            closes, bar_day, src = closed_daily_closes(a, now)
            state["data_source_" + a] = src
            if flatten:
                if state["units"][a] > 0:
                    t = execute(state, a, "sell", state["units"][a], bid, ask, now_iso, status)
                    append_csv("trades.csv", t, TRADE_HDR)
                continue
            if state["last_bar"].get(a) == bar_day:
                continue  # already acted on this closed bar
            w, info = target_weight(closes)
            spread = (ask - bid) / mid
            side, units = plan_trade(state, a, w, bid, ask, equity)
            if side == "buy" and spread > MAX_SPREAD:
                side = None
                status = "skipped buy: spread %.2f%%" % (spread * 100)
            if side:
                reason = "bar %s close %.0f sma %.0f vol %.2f -> w %.2f" % (
                    bar_day, info["last"], info["sma"], info["vol"], w)
                t = execute(state, a, side, units, bid, ask, now_iso, reason)
                append_csv("trades.csv", t, TRADE_HDR)
                notify("paper %s %s %.6f @ RM%.0f" % (side, a, units, t["price"]))
                equity = equity_of(state, mids)
            state["last_bar"][a] = bar_day
            state["last_signal_" + a] = {"bar": bar_day, "weight": round(w, 3), **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in info.items()}}
        state["last_mid"] = {a: quotes[a][2] for a in ASSETS}
        state["errors"] = 0
        equity = equity_of(state, mids)
        state["equity"] = round(equity, 2)
        append_csv("equity.csv", {"ts": now_iso, "equity": round(equity, 2), "cash": round(state["cash"], 2),
                                  "btc_units": state["units"]["BTC"], "eth_units": state["units"]["ETH"],
                                  "btc_mid": round(mids["BTC"], 2), "eth_mid": round(mids["ETH"], 2),
                                  "status": status}, EQ_HDR)
        state["status"] = status
        save_state(state)
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":  # manual run = alert test
            notify("ledger-bot manual run ok: equity RM%.2f, %s" % (equity, status))
        return 0
    except Exception as e:  # noqa: BLE001
        state["errors"] = state.get("errors", 0) + 1
        state["status"] = "ERROR: %s" % e
        if state["errors"] >= MAX_ERRORS:
            state["manual_halt"] = True
            notify("ledger-bot HALTED after %d errors: %s" % (state["errors"], e))
        else:
            notify("ledger-bot error %d/%d: %s" % (state["errors"], MAX_ERRORS, e))
        save_state(state)
        print("ERROR:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
