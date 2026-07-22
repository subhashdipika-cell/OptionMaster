"""Loader for locally stored 1-minute index candles.

The AlphaEdge strategy-lab collector writes minute candles for the cash indices
alongside the option chain:

    {UNDERLYING}_M1_{YYYY-MM-DD}.csv
    time,open,high,low,close,tick_volume,spread,real_volume

Unlike the option snapshots, these files are ROLLING windows — a file dated
2026-07-20 also contains bars going back several days — so the same minute
appears in many files. Everything is deduplicated by timestamp on load.

Timestamps in the files are UTC; everything returned here is IST so strategy
rules can be written in exchange time.

Why this exists: the option-chain archive is a sequence of point-in-time
snapshots (~71s apart, no OHLC), which is enough for momentum but cannot
express a reversal — a reversal is defined by the intrabar extreme (the sweep
low, the wick) that a point sample never sees. These candles supply the
signal side; the chain snapshots still supply the fills.
"""

import csv
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

# Cash-session bounds (NSE and BSE both 09:15–15:30 IST).
SESSION_START = (9, 15)
SESSION_END = (15, 30)


@dataclass(slots=True)
class SpotCandle:
    timestamp: datetime  # IST, timezone-aware; bar OPEN time
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def close_time(self) -> datetime:
        """When this bar finished — the earliest moment its signal is tradable."""
        return self.timestamp + timedelta(minutes=1)

    @property
    def range(self) -> float:
        return self.high - self.low


class SpotCandleRepository:
    """Reads and deduplicates the rolling M1 index-candle CSVs."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    @property
    def available(self) -> bool:
        return self._directory.is_dir()

    def symbols(self) -> list[str]:
        if not self.available:
            return []
        found = {item.name.split("_M1_")[0] for item in self._directory.glob("*_M1_*.csv")}
        return sorted(found)

    def load_day(self, symbol: str, day: date) -> list[SpotCandle]:
        """Session candles for one symbol-day, ascending, deduplicated."""
        return self._by_day(symbol).get(day.isoformat(), [])

    def days(self, symbol: str) -> list[date]:
        return [date.fromisoformat(key) for key in sorted(self._by_day(symbol))]

    def _by_day(self, symbol: str) -> dict[str, list[SpotCandle]]:
        # Reading every rolling file for a symbol is ~25 files / ~30k rows, so
        # parse once per symbol and keep it for the life of the process.
        return _load_symbol(str(self._directory), symbol.upper())


@lru_cache(maxsize=16)
def _load_symbol(directory: str, symbol: str) -> dict[str, list[SpotCandle]]:
    root = Path(directory)
    seen: dict[datetime, SpotCandle] = {}
    for path in sorted(root.glob(f"{symbol}_M1_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    stamp = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    ).astimezone(IST)
                    candle = SpotCandle(
                        timestamp=stamp,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("tick_volume") or 0),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if candle.high < candle.low or candle.high <= 0:
                    continue
                if not SESSION_START <= (stamp.hour, stamp.minute) <= SESSION_END:
                    continue
                # Rolling files overlap; last writer wins (identical either way).
                seen[stamp] = candle
    grouped: dict[str, list[SpotCandle]] = {}
    for stamp in sorted(seen):
        grouped.setdefault(stamp.date().isoformat(), []).append(seen[stamp])
    return grouped


def snapshot_at_or_after(snapshots: list, moment: datetime, *, tolerance_seconds: float):
    """First chain snapshot at/after ``moment``, or None if the next one is too far.

    The chain archive samples roughly every 71 seconds, so a signal that fires on
    a minute close is filled at the next available snapshot — typically ~1 minute
    later. ``tolerance_seconds`` rejects fills that would land after a data hole
    (NIFTY50 afternoons can go 7+ minutes between snapshots), where pretending we
    could have traded would flatter the result.
    """
    stamps = [snapshot.timestamp for snapshot in snapshots]
    index = bisect_left(stamps, moment)
    if index >= len(snapshots):
        return None
    candidate = snapshots[index]
    if (candidate.timestamp - moment).total_seconds() > tolerance_seconds:
        return None
    return candidate
