# OptionMaster

OptionMaster is an independent, paper-first options intelligence and execution application for Indian index options through Dhan.

## Safety defaults

- Paper trading is enabled by default.
- Live order placement is disabled unless explicitly configured.
- Dhan credentials are loaded from environment variables and are never stored in source control.
- The first release will focus on NIFTY and BANKNIFTY option buying, momentum scalping, and explainable strike selection.

## Initial architecture

```text
Dhan market data / option chain
             |
             v
      Feature pipeline
             |
             v
      Regime detector
             |
             v
    CE/PE + strike selector
             |
             v
      Risk and sizing gate
             |
             v
   Paper broker -> Live broker
```

## Local setup

```powershell
cd D:\Projects\OptionMaster
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
uvicorn optionmaster.main:app --reload --port 8300
```

Open `http://127.0.0.1:8300/docs` for the API documentation.

In a second terminal, start the dashboard on its reserved port:

```powershell
cd D:\Projects\OptionMaster
.\scripts\start_optionmaster_frontend.ps1
```

Open `http://127.0.0.1:5275`. The dashboard is read-only, refreshes key paper and learning evidence every 30 seconds, and calls the local backend at port `8300`.

For a one-step Windows startup, double-click `start_optionmaster.bat` in the project folder. It starts whichever local services are not already running and opens the dashboard.

The read-only Dhan connectivity endpoints are available at `/api/v1/dhan/status` and `/api/v1/dhan/expiries`. The paper-only live analysis endpoint is `/api/v1/dhan/analyze`. Use `scripts\start_optionmaster.ps1` after installing the project dependencies.

## Cost-aware testing and evidence journal

Every option backtest result and paper mark is calculated net of configurable brokerage, exchange/IPFT/SEBI charges, GST, sell-side STT, and buy-side stamp duty. Treat these as estimates and reconcile them to the broker contract note before relying on results.

OptionMaster keeps durable test evidence in `data/optionmaster.db` (excluded from source control):

- `POST /api/v1/backtests/evaluate-option-trade` calculates a cost-adjusted result without saving it.
- `POST /api/v1/backtests/record-option-trade` calculates and saves it for later evaluation.
- `GET /api/v1/reports/backtest-performance` returns aggregate gross P&L, charges, net P&L, win rate, and profit factor.
- `GET /api/v1/reports/paper-performance` reports only completed paper trades; `GET /api/v1/paper-trades` returns all persisted paper trades, including open ones.

Only non-`SKIP` scalping decisions are retained from a live data feed, keeping enough evidence for later strategy review without storing every raw tick.

## Stored-data backtesting (minute scalp)

The AlphaEdge strategy-lab collector stores Dhan option-chain snapshots (ATM±N
strikes, roughly one per minute during market hours) in
`D:/alphaedge/strategy-lab/data/options`. OptionMaster replays that archive
through a deterministic minute-scalp strategy (`stored-scalp-v1`):

- Entry: spot momentum burst (≥ 0.16% over 3 minutes) with the ATM option
  premium and volume confirming, spread ≤ 1%, premium between ₹20 and ₹600
  (gamma-rich contracts only), entries 10:00–13:00 IST.
- Exit: −10% stop, +15% target, 12-minute time stop, 15:15 square-off.
- Fills buy the stored ask and sell the stored bid; every trade carries the
  full NSE cost schedule.

Endpoints:

- `GET /api/v1/backtests/data-status` — what the local archive contains.
- `POST /api/v1/backtests/run` — run and persist a backtest (parameters optional).
- `GET /api/v1/backtests/runs` / `GET /api/v1/backtests/runs/{id}` — saved runs.

All parameters (`momentum_threshold_pct`, `stop_loss_pct`, `target_pct`,
`max_hold_minutes`, entry window, `max_premium`, symbols, date range, lots) can
be overridden in the request body. Treat results as in-sample evidence: the
archive currently covers only a few weeks and default parameters were tuned on
that same window.

## Telegram position alerts

Set `OPTIONMASTER_TELEGRAM_BOT_TOKEN` and `OPTIONMASTER_TELEGRAM_CHAT_ID` in
`.env`, then confirm delivery with `POST /api/v1/alerts/test`
(`GET /api/v1/alerts/status` shows configuration state). Once configured,
every paper-trade open and close sends a Telegram message with the contract,
entry/exit price, stop, target, and net P&L. Alerts are fire-and-forget: a
Telegram outage can never block or fail a trading decision.

## History & analysis dashboard

The dashboard has a "History & analysis" section that works over either the
forward paper journal or any saved backtest run: equity curve, win rate,
profit factor, max drawdown, average hold, and breakdowns by symbol, entry
hour, and exit reason, plus the full trade table. A "Run backtest" button
triggers `POST /api/v1/backtests/run` with the default parameters.

## Guarded learning loop

OptionMaster now labels every analysis signal and new paper trade with the active, versioned strategy profile. The baseline profile remains active by default. The included `tight-momentum-v1` profile is only a paper candidate.

- `GET /api/v1/learning/profiles` lists the deterministic profiles.
- `GET /api/v1/learning/active-profile` shows the rule set currently applied to analysis and paper entries.
- `GET /api/v1/learning/profiles/{profile_id}/evaluation` evaluates closed, net-of-cost forward paper results for one profile.
- `POST /api/v1/learning/review-and-promote` selects a candidate only when it has at least 30 closed paper trades, at least 5 observed losses, positive net P&L, profit factor of at least 1.15, and win rate of at least 45%.

Profile changes are locked whenever execution mode is not `PAPER`. Historical backtests can be recorded by profile for comparison, but cannot promote a candidate; promotion requires forward paper evidence.

## Shadow-mode market context

OptionMaster now runs an observation-only context engine beside every Dhan analysis and paper-entry request. It computes a 5-minute ATR, signal freshness, prior-session and swing levels, pivot and round-number references, and max-OI strike candidates. It then caps a proposed **underlying** target just before the nearest opposing barrier and evaluates whether the available structure offers at least 1.5R.

- `GET /api/v1/dhan/context` fetches current Dhan data and returns the context assessment.
- `POST /api/v1/context/evaluate` evaluates supplied candles and a snapshot for repeatable backtests.
- `GET /api/v1/reports/context-shadow` summarizes `would allow` and `would skip` decisions.
- `GET /api/v1/reports/context-outcomes` compares fresh vs. extended signals, confluence bands, and available structure room with the net result of linked, closed paper trades.

These filters are shadow-only: they record context and suggested pullback/target behavior, but do not alter paper or live entry logic. A paper entry saves the exact context decision ID so its eventual net P&L becomes research evidence. The outcome report marks itself ready only after 200 linked closed trades. Max-OI and chart levels are treated as candidates, not guarantees.

## Port allocation

OptionMaster uses backend port `8300` and reserves frontend port `5275`.
These do not conflict with AlphaEdge (`5000`, `5001`), SMT (`5173`, `8000`), IntelliTrade (`3000`, `8100`), or TradingBrain (`5174`, `8200`).

## Development stages

1. Validate Dhan authentication and market-data connectivity.
2. Store option-chain snapshots and derived features.
3. Implement deterministic regime and strike selection.
4. Run historical backtests and live paper trading.
5. Add controlled model evaluation and promotion.
6. Enable live execution only after explicit validation.
