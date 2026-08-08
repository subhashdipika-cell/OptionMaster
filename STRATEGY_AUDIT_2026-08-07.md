# Strategy vault audit — 7 August 2026

## Decision rule

OptionMaster adds an option-buying strategy only after a reproducible test
uses Indian index spot data for the signal, Dhan option-chain ask/bid fills,
the current exchange lot size, and the complete OptionMaster charge model.
Positive gross movement, a small sample, or a result from another asset class
is not sufficient.

## Direct, reproducible tests

| Strategy | Market and sample | Net result after costs | Decision |
| --- | --- | ---: | --- |
| Two Candle Theory | NIFTY50, 32 sessions, 21 trades | +₹747; PF 1.08 | Not added — too marginal and below the acceptance standard. |
| Two Candle Theory | NIFTY50 chronological holdout, 10 sessions, 9 trades | -₹655; PF 0.71 | Confirms the in-sample result did not hold up. |
| Two Candle Theory | BANKNIFTY, 32 sessions, 6 trades | -₹2,622; PF 0.44 | Not added. |
| Two Candle Theory | NIFTY50 + BANKNIFTY, 27 trades | -₹1,875; PF 0.86 | Rejected. |
| Book Pressure Scalp | NIFTY50, 89 trades | -₹8,903; PF 0.57 | Rejected. |
| Book Pressure Scalp | BANKNIFTY, 37 trades | -₹14,738; PF 0.12 | Rejected. |

The replay enters at the first available Dhan ask after a closed-bar signal,
exits at the first available bid, and includes brokerage, STT, exchange fees,
GST, SEBI/IPFT, and stamp duty. Two Candle and Book Pressure implementations
are retained as non-routed research code so the negative results remain
reproducible; neither appears in the dashboard nor the autonomous trader.

| 9/20 EMA Pullback Scalp (5m) | NIFTY50, 41 trades | -₹4,566; PF 0.68 | Rejected. IS: -₹3,832 / PF 0.65; chronological holdout: -₹733 / PF 0.78. |
| 9/20 EMA Pullback Scalp (5m) | BANKNIFTY, 10 trades | -₹5,704; PF 0.14 | Rejected. IS: -₹3,870 / PF 0.07; chronological holdout: -₹1,834 / PF 0.26. |
| Breakout–Retest Scalp (5m) | NIFTY50, 10 trades | +₹2,737; PF 2.01 | Not added. IS: +₹1,596 / PF 1.59; chronological holdout: one +₹1,140 trade â€” insufficient evidence. |
| Breakout–Retest Scalp (5m) | BANKNIFTY, 7 trades | +₹3,496; PF 3.46 | Not added. IS: +₹2,235 / PF 4.60; chronological holdout: two +₹1,261 trades / PF 2.57 â€” insufficient evidence. |

| Three-Bar Breakout-Retest + option momentum (5m; 5%/10%) | NIFTY50, 6 trades | -₹490; PF 0.78 | Rejected. IS: -₹754 / PF 0.45; holdout: +₹264 / PF 1.30, only 3 trades. |
| Three-Bar Breakout-Retest + option momentum (5m; 5%/10%) | BANKNIFTY, 7 trades | -₹2,931; PF 0.28 | Rejected. IS: -₹952 / PF 0.45; holdout: -₹1,979 / PF 0.16. |

The three-bar variant follows the later template: reference-bar support or
resistance, a subsequent close beyond that level, then a completed
retest-and-turn bar. It also requires option-premium momentum confirmation and
uses a 5% option-premium stop with a 10% target. It failed the independent
cost-aware replay and is withheld from all forward-paper controls.

The 9/20 EMA pullback was implemented as a research-only 5-minute adaptation:
EMA alignment and slope, higher-high/higher-low or lower-high/lower-low
structure, an EMA touch, and a completed reversal candle. It used the same
10% premium stop and 15% premium target as the tested momentum setup. The
15-minute version did not produce a valid intraday signal from the available
single-session warm-up window, so it is not a meaningful result. Neither
pullback variant is routed to paper or real execution.

The breakout–retest test is also research-only. Its rule was fixed before
replay: a 5-minute close beyond the preceding one-hour range by at least
0.05%, a retest within three bars, a level hold and directional confirmation
candle, followed by the same 10% premium stop and 15% premium target. The
positive result has too few trades and too few holdout observations to support
a paper trial or a profile change.

## Live-resolution research boundary

The stored option-chain archive is sampled roughly once per minute. It can
test completed 5-minute breakout/retest logic with conservative option
ask/bid fills, but it cannot substantiate a 15-second fill or exit claim.
OptionMaster's breakout-retest live monitor therefore builds 5-minute spot
bars from Dhan's streaming feed and checks the selected option bid/ask no more
often than every 15 seconds in **paper-only** mode. Those live observations
must be collected and assessed separately before any further decision.

## Vault-wide suitability review

| Strategy / note | Option-buying fit for Indian indices | Decision |
| --- | --- | --- |
| `two-candle-theory` | Direct fit: CE/PE directional rules, OI, volume, and defined stops. | Rejected by independent cost-aware replay above. |
| `convexity-buy-tb007` and tracker | Direct long-vol option-buying concept. | Not added. Its latest tracker records straddle PF 0.22 across 87 sessions and no directional trades. |
| `nse-intraday` | Useful playbook (VWAP bounce / ORB), but its catalyst, pre-open and relative-volume rules are not yet a single validated option-premium specification. | Deferred for a separate out-of-sample study. |
| `ema-vwap-scalping`, `9-20-ema-pullback` | Adaptable directional filters, but sourced from crypto and lack verified Indian option-premium evidence. | 9/20 pullback now rejected by the Indian option replay above; EMA-VWAP remains untested. |
| `book-pressure-scalp` | Adaptable, but only an OHLCV proxy for missing order-book data. | Rejected by replay above. |
| `pullback-to-ema`, `triple-screen`, `impulse-system` | Valuable higher-timeframe filters, not standalone intraday option-buying strategies. | Kept as future filters; current archive is too short for weekly/daily validation. |
| `eighty-twenty-bar`, `momentum-pinball` | D1 gold/crypto swing systems, not Indian index option scalps. | Out of scope. |
| `turtle-soup` | Failed its documented out-of-sample tests. | Rejected. |
| `trend-change-breakout-failure`, `golden-setup` | Conceptual / foreign-market strategies with no Indian option evidence. | Deferred; not force-fitted. |
| `premium-selling` | Explicitly short-premium. | Excluded: OptionMaster is option-buying only. |
| `can-slim` | Fundamental equity selection, not an option-buying execution strategy. | Out of scope. |
| `gold-m5-pullback-scalp` | Gold-specific and still unproven. | Out of scope. |
| `scalp-lab`, `tb007-backtest-tracker` | Results trackers, not independently specified strategies. | Reviewed as evidence only. |

## Next acceptable candidate

### ORB + VWAP + volume + ADX (five-minute; 5%/10%)

The first fixed research candidate from the local books is now implemented as
`stored-orb-vwap-v1`, with no forward-paper or live route. It uses the
09:15--09:30 range, a later five-minute close outside it, session VWAP and
Wilder +DI/-DI/ADX confirmation, 125% of average opening-range volume, and
same-direction ATM premium momentum. It enters at the next available Dhan ask
and exits at bid with a 5% premium stop, 10% premium target, 20-minute time
stop, one trade per day, and complete OptionMaster costs.

| Strategy | Market and sample | Net result after costs | Decision |
| --- | --- | ---: | --- |
| ORB + VWAP + volume + ADX | NIFTY50 + BANKNIFTY, 64 usable symbol-days, 2 trades | +â‚¹2,989; PF not meaningful (no losses) | Inconclusive. Both trades were NIFTY50 CE targets on 24 June and 1 July. Two observations cannot support a paper-forward trial. Chronological in-sample through 23 July contains both trades; the later 20 usable symbol-days contain zero qualifying trades. |

The exact specification is now available as the separate
`paper-orb-vwap-v1` forward-paper profile so new live Dhan observations can be
collected. During that trial, the autonomous worker uses this strategy alone,
caps it at one simulated trade per day, and applies the same 5%/10% risk,
spread, premium-momentum, lot-size, capital, brokerage, and tax checks. The
trial is explicitly blocked from the real-order route and must be ended before
Real Trade can be armed. It remains ineligible for promotion until the
pre-existing forward-evidence policy is met (at least 30 closed trades,
five observed losses, positive net P&L after costs, PF at least 1.15, and win
rate at least 45%).
