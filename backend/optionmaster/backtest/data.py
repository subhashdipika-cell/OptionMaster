"""Loader for locally stored Dhan option-chain snapshots.

The AlphaEdge strategy-lab collector polls Dhan's option chain roughly once a
minute during market hours and appends ATM±N strikes to daily CSVs:

    {UNDERLYING}_OPT_{YYYY-MM-DD}.csv
    time,underlying,under_ltp,expiry,strike,type,ltp,oi,prev_oi,iv,volume,
    delta,theta,vega,bid,ask

Timestamps in the files are UTC. Everything returned here is converted to IST
so strategy rules can be written in exchange time.
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

_FILE_PATTERN = re.compile(r"^(?P<symbol>[A-Z0-9]+)_OPT_(?P<date>\d{4}-\d{2}-\d{2})\.csv$")


@dataclass(slots=True)
class ChainQuote:
    strike: float
    side: str  # "CE" or "PE"
    ltp: float
    bid: float
    ask: float
    oi: float
    prev_oi: float
    iv: float
    volume: float

    @property
    def spread_pct(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        midpoint = (self.bid + self.ask) / 2
        return ((self.ask - self.bid) / midpoint) * 100 if midpoint else None


@dataclass(slots=True)
class ChainSnapshot:
    timestamp: datetime  # IST, timezone-aware
    spot: float
    expiry: str
    quotes: dict[tuple[float, str], ChainQuote] = field(default_factory=dict)

    def quote(self, strike: float, side: str) -> ChainQuote | None:
        return self.quotes.get((strike, side))

    def atm_strike(self, side: str) -> float | None:
        strikes = [key[0] for key in self.quotes if key[1] == side]
        return min(strikes, key=lambda strike: abs(strike - self.spot)) if strikes else None


@dataclass(slots=True)
class StoredDay:
    symbol: str
    day: date
    snapshots: list[ChainSnapshot]


class StoredDataRepository:
    """Reads the daily option-snapshot CSVs collected from Dhan."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    @property
    def available(self) -> bool:
        return self._directory.is_dir()

    def list_days(self) -> list[tuple[str, date]]:
        if not self.available:
            return []
        found: list[tuple[str, date]] = []
        for item in sorted(self._directory.glob("*_OPT_*.csv")):
            match = _FILE_PATTERN.match(item.name)
            if match:
                found.append((match.group("symbol"), date.fromisoformat(match.group("date"))))
        return found

    def load_day(self, symbol: str, day: date) -> StoredDay | None:
        path = self._directory / f"{symbol}_OPT_{day.isoformat()}.csv"
        if not path.is_file():
            return None
        grouped: dict[str, ChainSnapshot] = {}
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    timestamp = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                    spot = float(row["under_ltp"])
                    quote = ChainQuote(
                        strike=float(row["strike"]),
                        side=str(row["type"]).upper(),
                        ltp=float(row["ltp"] or 0),
                        bid=float(row["bid"] or 0),
                        ask=float(row["ask"] or 0),
                        oi=float(row["oi"] or 0),
                        prev_oi=float(row["prev_oi"] or 0),
                        iv=float(row["iv"] or 0),
                        volume=float(row["volume"] or 0),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if spot <= 0 or quote.side not in {"CE", "PE"}:
                    continue
                snapshot = grouped.get(row["time"])
                if snapshot is None:
                    snapshot = ChainSnapshot(
                        timestamp=timestamp.astimezone(IST),
                        spot=spot,
                        expiry=str(row.get("expiry") or ""),
                    )
                    grouped[row["time"]] = snapshot
                snapshot.quotes[(quote.strike, quote.side)] = quote
        snapshots = sorted(grouped.values(), key=lambda item: item.timestamp)
        # A day can briefly contain a second expiry around rollover; keep the nearest.
        expiries = {snapshot.expiry for snapshot in snapshots if snapshot.expiry}
        if len(expiries) > 1:
            nearest = min(expiries)
            snapshots = [snapshot for snapshot in snapshots if snapshot.expiry == nearest]
        return StoredDay(symbol=symbol, day=day, snapshots=snapshots)
