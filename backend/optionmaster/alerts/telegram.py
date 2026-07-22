"""Telegram position alerts.

Alerts are strictly best-effort: they run on a background thread, swallow every
network error, and can never block or fail a trading decision. Configure with:

    OPTIONMASTER_TELEGRAM_BOT_TOKEN=123456:ABC...
    OPTIONMASTER_TELEGRAM_CHAT_ID=123456789
"""

import json
import logging
import urllib.error
import urllib.request
from threading import Thread

from optionmaster.paper.models import PaperTrade

logger = logging.getLogger(__name__)

_API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_SECONDS = 10


class TelegramNotifier:
    """Sends OptionMaster position alerts to a single Telegram chat."""

    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self._bot_token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    def send_text(self, text: str, *, wait: bool = False) -> bool:
        """Queue a message; with ``wait=True`` send synchronously and report success."""
        if not self.configured:
            return False
        if wait:
            return self._post(text)
        Thread(target=self._post, args=(text,), daemon=True).start()
        return True

    def notify_open(self, trade: PaperTrade) -> None:
        opened = trade.opened_at.astimezone().strftime("%d-%b %H:%M:%S")
        self.send_text(
            "🟢 <b>OptionMaster — position opened</b>\n"
            f"{trade.symbol} {trade.strike:g} {trade.side.value} ({trade.expiry})\n"
            f"Entry ₹{trade.entry_price:.2f} × {trade.quantity} qty\n"
            f"SL ₹{trade.stop_loss:.2f} · Target ₹{trade.target:.2f}\n"
            f"Premium ₹{trade.premium_paid:,.0f} · Max risk ₹{trade.maximum_risk:,.0f}\n"
            f"Mode: PAPER · {opened}"
        )

    def notify_close(self, trade: PaperTrade) -> None:
        net = trade.realized_pnl if trade.realized_pnl is not None else trade.unrealized_pnl
        icon = "✅" if (net or 0) >= 0 else "🔴"
        closed = (trade.closed_at or trade.opened_at).astimezone().strftime("%d-%b %H:%M:%S")
        exit_price = trade.exit_price if trade.exit_price is not None else trade.current_price
        self.send_text(
            f"{icon} <b>OptionMaster — position closed ({trade.status.value})</b>\n"
            f"{trade.symbol} {trade.strike:g} {trade.side.value} ({trade.expiry})\n"
            f"Entry ₹{trade.entry_price:.2f} → Exit ₹{exit_price:.2f} × {trade.quantity} qty\n"
            f"Net P&L (after charges): ₹{(net or 0):,.2f}\n"
            f"Mode: PAPER · {closed}"
        )

    def _post(self, text: str) -> bool:
        payload = json.dumps(
            {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        ).encode("utf-8")
        request = urllib.request.Request(
            _API_TEMPLATE.format(token=self._bot_token),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("Telegram alert failed: %s", exc)
            return False
