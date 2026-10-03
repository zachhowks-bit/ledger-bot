# ledger-bot

PAPER trading only. No API keys, no real orders. Daily trend rule (close above 100-day SMA) on BTC and ETH, volatility-targeted, simulated at real Luno MYR bid/ask with assumed fee and slippage.

- `bot/ledger.py` the whole bot. `tests/` unit and fault-injection tests (run before every bot run).
- `.github/workflows/ledger.yml` runs every 6 hours, acts once per closed daily bar, commits state to `docs/data/`.
- `docs/index.html` dashboard (GitHub Pages, folder `/docs`).
- Kill switch: add a file named `KILL` to the repo root and the next run flattens everything and stops buying.
- Drawdown halt: after -15% from peak the bot sets `manual_halt` in `docs/data/state.json`; edit it to `false` to resume.
- Alerts: optional free ntfy.sh topic stored as repo secret `NTFY_TOPIC`.

Assumptions (unverified): fee 0.35% per side, slippage 0.2%, Luno minimum order sizes. A 7-day run proves plumbing only, not profitability.
