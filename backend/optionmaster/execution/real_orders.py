"""Protected Dhan execution for explicitly armed real mode."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import requests

from optionmaster.config import Settings
from optionmaster.dhan.client import _fresh_token
from optionmaster.market.models import OptionQuote
from optionmaster.paper.models import CreatePaperTradeRequest


class RealOrderRejected(RuntimeError):
    """Raised when a real Super Order cannot safely be submitted."""


@dataclass(frozen=True, slots=True)
class RealOrderReceipt:
    order_id: str
    order_status: str
    correlation_id: str
    order_type: str
    entry_price: float
    target_price: float
    stop_loss_price: float
    trailing_jump: float


class DhanRealOrderClient:
    """Submit a BUY-only NSE F&O Super Order with target and stop protection."""

    endpoint = "https://api.dhan.co/v2/super/orders"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def place_option_buy(
        self,
        *,
        security_id: int,
        quantity: int,
        quote: OptionQuote,
        request: CreatePaperTradeRequest,
    ) -> RealOrderReceipt:
        if quantity <= 0 or security_id <= 0:
            raise RealOrderRejected("A valid Dhan contract and positive quantity are required.")
        if quote.ask <= 0 or quote.bid <= 0 or quote.ltp <= 0:
            raise RealOrderRejected("Selected option has no usable bid/ask quote for real execution.")
        order_type = "MARKET" if quote.spread_fraction <= 0.0025 else "LIMIT"
        entry = quote.ask
        correlation_id = f"OM-{uuid4().hex[:20]}"
        client_id, token = _fresh_token(self._settings)
        if not client_id or not token:
            raise RealOrderRejected("Dhan credentials are not configured.")
        payload = {
            "dhanClientId": str(client_id),
            "correlationId": correlation_id,
            "transactionType": "BUY",
            "exchangeSegment": "NSE_FNO",
            "productType": "INTRADAY",
            "orderType": order_type,
            "securityId": str(security_id),
            "quantity": int(quantity),
            "price": 0.0 if order_type == "MARKET" else round(entry, 2),
            "targetPrice": round(entry * (1 + request.target_fraction), 2),
            "stopLossPrice": round(entry * (1 - request.stop_loss_fraction), 2),
            "trailingJump": round(max(0.05, entry * request.trailing_fraction), 2),
        }
        try:
            response = requests.post(
                self.endpoint,
                headers={"access-token": token, "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            data = response.json() if response.content else {}
        except (requests.RequestException, ValueError) as exc:
            raise RealOrderRejected(f"Dhan Super Order request failed: {exc}") from exc
        if isinstance(data, dict) and isinstance(data.get("data"), dict) and not data.get("orderId"):
            data = data["data"]
        if response.status_code >= 400 or (isinstance(data, dict) and data.get("status") == "failure"):
            message = data.get("remarks") if isinstance(data, dict) else None
            message = message or (data.get("errorMessage") if isinstance(data, dict) else None)
            raise RealOrderRejected(f"Dhan rejected the Super Order: {message or response.status_code}")
        if not isinstance(data, dict) or not data.get("orderId"):
            raise RealOrderRejected("Dhan returned no order ID for the Super Order.")
        return RealOrderReceipt(
            order_id=str(data["orderId"]),
            order_status=str(data.get("orderStatus") or "SUBMITTED"),
            correlation_id=correlation_id,
            order_type=order_type,
            entry_price=round(entry, 2),
            target_price=float(payload["targetPrice"]),
            stop_loss_price=float(payload["stopLossPrice"]),
            trailing_jump=float(payload["trailingJump"]),
        )
