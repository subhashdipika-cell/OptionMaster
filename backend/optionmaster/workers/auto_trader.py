"""Autonomous paper trader — OptionMaster's hands-free side.

Until now every OptionMaster paper trade required a human on the dashboard
(the app had recorded exactly zero paper trades since install). This worker
drives the SAME pipeline the dashboard uses — live Dhan snapshot -> features
-> regime -> select_signal -> risk gate -> PaperBroker — on a schedule:

  every 60 s during NSE hours (Mon-Fri 09:15-15:30 IST):
    1. mark every OPEN paper trade to market (stop/target close + Telegram
       alerts come free from PaperBroker.mark_to_market),
    2. square off anything still OPEN at >= 15:10 IST,
    3. between 09:30 and 14:30, if no trade is open and the daily cap isn't
       hit, ask the gate for an entry (rejections are normal and logged).

PAPER ONLY by design: entries go through main.create_dhan_paper_trade — the
identical full-gate endpoint the dashboard calls. The Super Order builder is
never touched. Expiry selection skips 0-DTE (buying into expiry-day theta is
a different game — the fleet's other apps gate it too).

Keep-awake: holds the machine out of Modern Standby DURING session hours
only, and releases it after the close — OptionMaster is NSE-only, so unlike
the 24/7 crypto apps there is no reason to block sleep overnight.

State (data/auto_trader.json) persists the runtime toggle and the day's
entry count across restarts.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = Path("data") / "auto_trader.json"

TICK_SECONDS = 60
SESSION_START = 9 * 60 + 15    # 09:15 IST
ENTRY_FROM = 9 * 60 + 30       # first entry attempt
ENTRY_TO = 14 * 60 + 30        # last entry attempt
SQUARE_OFF = 15 * 60 + 10      # force-close leftovers
SESSION_END = 15 * 60 + 30
ENTRY_RETRY_SECONDS = 300      # ask the gate at most every 5 min

_started = False
_last_attempt = 0.0
_last_reason: str | None = None
_awake_held = False


# ── state ─────────────────────────────────────────────────────────────────

def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(**patch) -> None:
    st = _state()
    st.update(patch)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def _entries_today(today: str) -> int:
    st = _state()
    return int(st.get("entries", 0)) if st.get("day") == today else 0


def is_enabled() -> bool:
    from optionmaster.config import get_settings
    return bool(_state().get("enabled", get_settings().auto_trader_enabled))


def set_enabled(on: bool) -> dict:
    _save(enabled=bool(on))
    return status()


def status() -> dict:
    now = datetime.now(IST)
    mins = now.hour * 60 + now.minute
    st = _state()
    today = now.strftime("%Y-%m-%d")
    return {
        "enabled": is_enabled(),
        "paper_only": True,
        "in_session": now.weekday() < 5 and SESSION_START <= mins <= SESSION_END,
        "entry_window": "09:30-14:30 IST",
        "square_off": "15:10 IST",
        "entries_today": _entries_today(today),
        "max_trades_per_day": _max_trades(),
        "last_reason": _last_reason,
        "day": st.get("day"),
    }


def _max_trades() -> int:
    from optionmaster.config import get_settings
    return get_settings().auto_max_trades_per_day


# ── keep-awake (session-scoped) ───────────────────────────────────────────

def _keep_awake(hold: bool) -> None:
    global _awake_held
    if hold == _awake_held:
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if hold else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        _awake_held = hold
    except Exception:
        pass


# ── per-tick work ─────────────────────────────────────────────────────────

def _open_trades(broker) -> list:
    from optionmaster.paper.models import PaperTradeStatus
    return [t for t in broker.list_trades() if t.status is PaperTradeStatus.OPEN]


def _snapshot_for(trade):
    """Live snapshot for an existing trade's underlying/expiry."""
    from optionmaster.config import get_settings
    from optionmaster.dhan.client import DhanClientFactory
    from optionmaster.dhan.service import DhanMarketService
    settings = get_settings()
    return DhanMarketService(DhanClientFactory(settings)).live_snapshot(
        symbol=trade.symbol,
        security_id=trade.underlying_security_id,
        segment=trade.underlying_segment,
        expiry=trade.expiry,
        instrument_type=trade.underlying_instrument_type,
        vix_security_id=settings.india_vix_security_id,
        vix_segment=settings.india_vix_segment,
        vix_instrument_type=settings.india_vix_instrument_type,
    )


def _pick_expiry() -> str | None:
    """Nearest expiry AFTER today — 0-DTE buying is deliberately skipped."""
    from optionmaster.config import get_settings
    from optionmaster.dhan.client import DhanClientFactory
    from optionmaster.dhan.service import DhanMarketService
    settings = get_settings()
    service = DhanMarketService(DhanClientFactory(settings))
    today = date.today().isoformat()
    upcoming = sorted(e for e in service.expiries(
        settings.auto_security_id, settings.auto_segment) if e > today)
    return upcoming[0] if upcoming else None


def _try_entry(today: str) -> None:
    global _last_attempt, _last_reason
    now = time.time()
    if now - _last_attempt < ENTRY_RETRY_SECONDS:
        return
    _last_attempt = now

    from optionmaster import main as app  # late: avoid circular import
    from optionmaster.config import get_settings
    from optionmaster.paper.models import CreatePaperTradeRequest
    settings = get_settings()

    expiry = _pick_expiry()
    if expiry is None:
        _last_reason = "no non-0DTE expiry available"
        return

    decision = app.create_dhan_paper_trade(CreatePaperTradeRequest(
        symbol=settings.auto_symbol,
        security_id=settings.auto_security_id,
        segment=settings.auto_segment,
        instrument_type=settings.auto_instrument_type,
        expiry=expiry,
        lots=settings.auto_lots,
        capital=settings.auto_capital,
    ))
    _last_reason = decision.reason
    if decision.accepted:
        _save(day=today, entries=_entries_today(today) + 1)
        print(f"[AutoTrader] OPENED paper trade: {decision.trade.symbol} "
              f"{decision.trade.strike} {decision.trade.side} x{decision.trade.quantity} "
              f"@ {decision.trade.entry_price} (expiry {expiry})")
    else:
        print(f"[AutoTrader] gate declined: {decision.reason}")


def _tick(broker) -> None:
    now = datetime.now(IST)
    mins = now.hour * 60 + now.minute
    in_session = now.weekday() < 5 and SESSION_START <= mins <= SESSION_END
    _keep_awake(in_session and is_enabled())
    if not in_session or not is_enabled():
        return
    today = now.strftime("%Y-%m-%d")

    # 1) mark open trades — stop/target exits + alerts happen inside broker
    for trade in _open_trades(broker):
        try:
            broker.mark_to_market(trade.id, _snapshot_for(trade))
        except Exception as exc:
            print(f"[AutoTrader] mark failed for {trade.id[:8]}: {exc}")

    # 2) square off leftovers near the close
    if mins >= SQUARE_OFF:
        for trade in _open_trades(broker):
            try:
                broker.square_off(trade.id, _snapshot_for(trade))
                print(f"[AutoTrader] squared off {trade.symbol} {trade.strike} {trade.side}")
            except Exception as exc:
                print(f"[AutoTrader] square-off failed for {trade.id[:8]}: {exc}")
        return  # no entries this late

    # 3) entry attempt
    if ENTRY_FROM <= mins <= ENTRY_TO and not _open_trades(broker) \
            and _entries_today(today) < _max_trades():
        try:
            _try_entry(today)
        except Exception as exc:
            print(f"[AutoTrader] entry attempt failed: {exc}")


def _loop(broker) -> None:
    while True:
        try:
            _tick(broker)
        except Exception as exc:  # the loop must survive anything
            print(f"[AutoTrader] tick failed: {exc}")
        time.sleep(TICK_SECONDS)


def start_auto_trader(broker) -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, args=(broker,), daemon=True,
                     name="om-auto-trader").start()
    print("[AutoTrader] started - paper-only, NIFTY, entries 09:30-14:30 IST, "
          "square-off 15:10, max "
          f"{_max_trades()}/day, enabled={is_enabled()}")
