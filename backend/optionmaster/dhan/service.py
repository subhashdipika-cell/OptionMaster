from datetime import datetime, timedelta, timezone
from typing import Any

from optionmaster.context.models import PriceBar
from optionmaster.dhan.client import DhanClientFactory
from optionmaster.market.models import MarketSnapshot, OptionQuote, OptionSide


class DhanMarketService:
    """Read-only Dhan market-data operations for paper analysis."""

    def __init__(self, client_factory: DhanClientFactory) -> None:
        self._client_factory = client_factory

    def expiries(self, security_id: int, segment: str) -> list[str]:
        client = self._client_factory.create()
        response = client.expiry_list(
            under_security_id=security_id,
            under_exchange_segment=segment,
        )
        data = response.get("data", []) if isinstance(response, dict) else []
        # Some SDK/API versions wrap the payload one additional time
        # ({"data": {"data": [...]}}) — same unwrap option_chain_snapshot does.
        if isinstance(data, dict):
            data = data.get("data", [])
        return [str(item) for item in data] if isinstance(data, list) else []

    def option_chain(self, security_id: int, segment: str, expiry: str) -> dict[str, Any]:
        client = self._client_factory.create()
        response = client.option_chain(
            under_security_id=security_id,
            under_exchange_segment=segment,
            expiry=expiry,
        )
        return response if isinstance(response, dict) else {}

    def option_chain_snapshot(
        self, *, symbol: str, security_id: int, segment: str, expiry: str
    ) -> MarketSnapshot:
        raw = self.option_chain(security_id, segment, expiry)
        if raw.get("status") == "failure":
            remarks = raw.get("remarks") or "Unknown Dhan API failure."
            raise RuntimeError(f"Dhan API failure: {remarks}")
        data = raw.get("data", raw)
        # Some SDK/API versions wrap the payload one additional time.
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            raise RuntimeError("Dhan returned an invalid option-chain response.")

        underlying = float(data.get("last_price") or 0)
        if underlying <= 0:
            raise RuntimeError("Dhan option-chain response has no underlying price.")

        quotes: list[OptionQuote] = []
        for strike_text, node in (data.get("oc") or {}).items():
            if not isinstance(node, dict):
                continue
            try:
                strike = float(strike_text)
            except (TypeError, ValueError):
                continue
            for key, side in (("ce", OptionSide.CE), ("pe", OptionSide.PE)):
                leg = node.get(key) or {}
                if not isinstance(leg, dict):
                    continue
                greeks = leg.get("greeks") or {}
                quotes.append(
                    OptionQuote(
                        security_id=int(float(leg["security_id"])) if leg.get("security_id") else None,
                        strike=strike,
                        side=side,
                        ltp=float(leg.get("last_price") or 0),
                        bid=float(leg.get("top_bid_price") or 0),
                        ask=float(leg.get("top_ask_price") or 0),
                        delta=float(greeks.get("delta") or 0),
                        gamma=float(greeks.get("gamma") or 0),
                        theta=float(greeks.get("theta") or 0),
                        vega=float(greeks.get("vega") or 0),
                        iv=float(leg.get("implied_volatility") or 0) / 100,
                        oi=float(leg.get("oi") or 0),
                        previous_oi=float(leg.get("previous_oi") or 0),
                        volume=float(leg.get("volume") or 0),
                    )
                )

        return MarketSnapshot(
            symbol=symbol.upper(),
            timestamp=datetime.now(timezone.utc),
            underlying=underlying,
            option_quotes=quotes,
        )

    def intraday_context(
        self,
        *,
        security_id: int,
        segment: str,
        instrument_type: str = "INDEX",
        momentum_bars: int = 5,
    ) -> tuple[float, float]:
        """Return (session_change_pct, recent_momentum_pct) from 1-minute bars."""
        client = self._client_factory.create()
        now = datetime.now(timezone.utc)
        response = client.intraday_minute_data(
            security_id=str(security_id),
            exchange_segment=segment,
            instrument_type=instrument_type,
            from_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
            to_date=now.strftime("%Y-%m-%d"),
            interval=1,
        )
        if isinstance(response, dict) and response.get("status") == "failure":
            raise RuntimeError(response.get("remarks") or "Dhan intraday data request failed.")
        data = response.get("data", response) if isinstance(response, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        closes = data.get("close") or [] if isinstance(data, dict) else []
        if len(closes) < momentum_bars + 1:
            return 0.0, 0.0
        first = float(closes[0])
        last = float(closes[-1])
        reference = float(closes[-(momentum_bars + 1)])
        if first <= 0 or reference <= 0:
            return 0.0, 0.0
        return ((last / first) - 1) * 100, ((last / reference) - 1) * 100

    def intraday_bars(
        self,
        *,
        security_id: int,
        segment: str,
        instrument_type: str = "INDEX",
        interval: int = 5,
        lookback_days: int = 4,
        maximum_bars: int = 120,
    ) -> list[PriceBar]:
        """Fetch a bounded OHLCV series for the shadow context engine."""
        client = self._client_factory.create()
        now = datetime.now(timezone.utc)
        response = client.intraday_minute_data(
            security_id=str(security_id),
            exchange_segment=segment,
            instrument_type=instrument_type,
            from_date=(now - timedelta(days=lookback_days)).strftime("%Y-%m-%d"),
            to_date=now.strftime("%Y-%m-%d"),
            interval=interval,
        )
        if isinstance(response, dict) and response.get("status") == "failure":
            raise RuntimeError(response.get("remarks") or "Dhan intraday data request failed.")
        data = response.get("data", response) if isinstance(response, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            return []
        timestamps = data.get("timestamp") or []
        opens = data.get("open") or []
        highs = data.get("high") or []
        lows = data.get("low") or []
        closes = data.get("close") or []
        volumes = data.get("volume") or []
        size = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
        bars: list[PriceBar] = []
        for index in range(size):
            try:
                bars.append(
                    PriceBar(
                        timestamp=datetime.fromtimestamp(float(timestamps[index]), tz=timezone.utc),
                        open=float(opens[index]),
                        high=float(highs[index]),
                        low=float(lows[index]),
                        close=float(closes[index]),
                        volume=float(volumes[index]) if index < len(volumes) else 0,
                    )
                )
            except (TypeError, ValueError, OSError):
                continue
        return bars[-maximum_bars:]

    def live_snapshot(
        self,
        *,
        symbol: str,
        security_id: int,
        segment: str,
        expiry: str,
        instrument_type: str = "INDEX",
        vix_security_id: int = 21,
        vix_segment: str = "IDX_I",
        vix_instrument_type: str = "INDEX",
    ) -> MarketSnapshot:
        """Build an option-chain snapshot enriched with trend and India VIX."""
        snapshot = self.option_chain_snapshot(
            symbol=symbol,
            security_id=security_id,
            segment=segment,
            expiry=expiry,
        )
        try:
            change, momentum = self.intraday_context(
                security_id=security_id,
                segment=segment,
                instrument_type=instrument_type,
            )
            snapshot.underlying_change_pct = round(change, 4)
            snapshot.underlying_momentum = round(momentum, 4)
        except Exception:
            # Chain quality is still useful if supplemental candles are unavailable.
            pass

        try:
            client = self._client_factory.create()
            now = datetime.now(timezone.utc)
            response = client.intraday_minute_data(
                security_id=str(vix_security_id),
                exchange_segment=vix_segment,
                instrument_type=vix_instrument_type,
                from_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
                to_date=now.strftime("%Y-%m-%d"),
                interval=1,
            )
            data = response.get("data", response) if isinstance(response, dict) else {}
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data = data["data"]
            closes = data.get("close") or [] if isinstance(data, dict) else []
            snapshot.india_vix = float(closes[-1]) if closes else 0.0
        except Exception:
            pass
        return snapshot
