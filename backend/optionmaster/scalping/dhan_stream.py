from datetime import datetime, timezone
from typing import Callable

from optionmaster.config import Settings
from optionmaster.scalping.models import MarketTick, ScalpingSessionRequest, TickKind


class DhanScalpingFeed:
    """Runs Dhan's two-instrument Full-feed subscription in a background thread."""

    def __init__(
        self,
        *,
        settings: Settings,
        configuration: ScalpingSessionRequest,
        on_tick: Callable[[MarketTick], None],
        on_error: Callable[[str], None],
    ) -> None:
        self._settings = settings
        self._configuration = configuration
        self._on_tick = on_tick
        self._on_error = on_error
        self._feed = None

    def start(self) -> None:
        if not self._settings.dhan_configured:
            raise RuntimeError("Dhan credentials are not configured.")
        from dhanhq import DhanContext, MarketFeed

        context = DhanContext(self._settings.dhan_client_id, self._settings.dhan_access_token)
        instruments = [
            (self._segment_code(MarketFeed, self._configuration.spot_segment), str(self._configuration.spot_security_id), MarketFeed.Full),
            (self._segment_code(MarketFeed, self._configuration.option_segment), str(self._configuration.option_security_id), MarketFeed.Full),
        ]
        self._feed = MarketFeed(
            context,
            instruments,
            version="v2",
            on_ticks=self._handle_message,
            on_error=lambda _feed, exc: self._on_error(str(exc)),
        )
        self._feed.start()

    def stop(self) -> None:
        if self._feed is not None:
            self._feed.close_connection()

    def _handle_message(self, _feed, data: object) -> None:
        if not isinstance(data, dict) or data.get("type") != "Full Data":
            return
        security_id = int(data.get("security_id") or 0)
        if security_id not in {self._configuration.spot_security_id, self._configuration.option_security_id}:
            return
        depth = data.get("depth") or []
        top = depth[0] if depth else {}
        tick = MarketTick(
            security_id=security_id,
            kind=TickKind.SPOT if security_id == self._configuration.spot_security_id else TickKind.OPTION,
            timestamp=datetime.now(timezone.utc),
            ltp=float(data.get("LTP") or 0),
            bid=float(top.get("bid_price") or 0),
            ask=float(top.get("ask_price") or 0),
            last_quantity=int(data.get("LTQ") or 0),
            cumulative_volume=int(data.get("volume") or 0),
            bid_quantity=int(top.get("bid_quantity") or 0),
            ask_quantity=int(top.get("ask_quantity") or 0),
        )
        self._on_tick(tick)

    @staticmethod
    def _segment_code(market_feed, segment: str) -> int:
        mapping = {"IDX_I": market_feed.IDX, "NSE_EQ": market_feed.NSE, "NSE_FNO": market_feed.NSE_FNO}
        try:
            return mapping[segment.upper()]
        except KeyError as exc:
            raise ValueError(f"Unsupported Dhan scalping feed segment: {segment}") from exc
