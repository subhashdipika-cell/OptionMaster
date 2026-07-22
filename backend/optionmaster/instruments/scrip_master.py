import csv
import hashlib
import io
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

DETAILED_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"


class ScripMasterError(RuntimeError):
    """Raised when the Dhan scrip master is unavailable or incomplete."""


@dataclass(frozen=True, slots=True)
class ScripMasterSummary:
    fetched_at: str
    source_url: str
    sha256: str
    nse_instruments: int
    instruments_with_lot_size: int

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


def _column(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


class NseScripMaster:
    """Local, refreshable index of Dhan's current NSE detailed scrip master."""

    def __init__(self, data_dir: str | Path = "data/scrip-master") -> None:
        self._data_dir = Path(data_dir)
        self._csv_path = self._data_dir / "dhan-nse-scrip-master.csv"
        self._metadata_path = self._data_dir / "dhan-nse-scrip-master.json"
        self._lot_by_security_id: dict[int, int] | None = None
        self._summary: ScripMasterSummary | None = None

    def refresh(self, *, timeout_seconds: float = 60) -> ScripMasterSummary:
        try:
            response = requests.get(DETAILED_SCRIP_MASTER_URL, timeout=timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ScripMasterError(f"Unable to download Dhan detailed scrip master: {exc}") from exc
        return self._install(response.content, DETAILED_SCRIP_MASTER_URL)

    def load_cached(self) -> ScripMasterSummary:
        if not self._csv_path.exists() or not self._metadata_path.exists():
            raise ScripMasterError("NSE scrip master has not been refreshed yet.")
        try:
            metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
            self._summary = ScripMasterSummary(**metadata)
            self._lot_by_security_id = self._build_lot_index(self._csv_path.read_bytes())
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ScripMasterError(f"Unable to load cached NSE scrip master: {exc}") from exc
        return self._summary

    def status(self) -> ScripMasterSummary | None:
        if self._summary is not None:
            return self._summary
        if self._metadata_path.exists():
            try:
                metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
                self._summary = ScripMasterSummary(**metadata)
                return self._summary
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
        return None

    def lot_size_for_security(self, security_id: int) -> int:
        if self._lot_by_security_id is None:
            self.load_cached()
        assert self._lot_by_security_id is not None
        lot_size = self._lot_by_security_id.get(int(security_id))
        if not lot_size:
            raise ScripMasterError(
                f"No NSE SEM_LOT_UNITS value found for Dhan security ID {security_id}."
            )
        return lot_size

    def _install(self, payload: bytes, source_url: str) -> ScripMasterSummary:
        lot_index, nse_instruments = self._build_lot_index(payload, include_count=True)
        if not lot_index:
            raise ScripMasterError("Downloaded Dhan master has no NSE rows with SEM_LOT_UNITS values.")
        summary = ScripMasterSummary(
            fetched_at=datetime.now(timezone.utc).isoformat(),
            source_url=source_url,
            sha256=hashlib.sha256(payload).hexdigest(),
            nse_instruments=nse_instruments,
            instruments_with_lot_size=len(lot_index),
        )
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._csv_path.write_bytes(payload)
        self._metadata_path.write_text(json.dumps(summary.as_dict(), indent=2), encoding="utf-8")
        self._lot_by_security_id = lot_index
        self._summary = summary
        return summary

    @staticmethod
    def _build_lot_index(
        payload: bytes, *, include_count: bool = False
    ) -> dict[int, int] | tuple[dict[int, int], int]:
        text = payload.decode("utf-8-sig")
        lots: dict[int, int] = {}
        nse_instruments = 0
        for raw_row in csv.DictReader(io.StringIO(text)):
            row = {str(key).strip(): str(value).strip() for key, value in raw_row.items() if key}
            exchange = _column(row, "SEM_EXM_EXCH_ID", "EXCH_ID").upper()
            if exchange != "NSE":
                continue
            nse_instruments += 1
            security = _column(
                row,
                "SEM_SMST_SECURITY_ID",
                "SEM_SECURITY_ID",
                "SECURITY_ID",
            )
            lot = _column(row, "SEM_LOT_UNITS", "LOT_SIZE")
            try:
                security_id = int(float(security))
                lot_size = int(float(lot))
            except (TypeError, ValueError):
                continue
            if security_id > 0 and lot_size > 0:
                lots[security_id] = lot_size
        if include_count:
            return lots, nse_instruments
        return lots
