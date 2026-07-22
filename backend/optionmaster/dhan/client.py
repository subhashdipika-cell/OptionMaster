import json
from pathlib import Path
from typing import Any

from optionmaster.config import Settings


def _fresh_token(settings: Settings) -> tuple[str | None, str | None]:
    """(client_id, access_token) — prefer the fleet's auto-refreshed token.

    Dhan tokens rotate daily. The static .env copy silently expired on
    2026-07-17 and every live-data call returned empty results for five days
    (expiry lists came back [] instead of erroring). AlphaEdge's strategy-lab
    refreshes its token every morning at 06:30 via TOTP; reading it at
    client-creation time keeps OptionMaster permanently fresh. Only the two
    credential fields are read — the file also holds secrets that must never
    leave it. Falls back to the .env values if the file is unavailable.
    """
    path = Path(settings.dhan_shared_token_file)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        tok = cfg.get("access_token")
        cid = cfg.get("client_id") or settings.dhan_client_id
        if tok and cid:
            return str(cid), str(tok)
    except Exception:
        pass
    return settings.dhan_client_id, settings.dhan_access_token


class DhanClientFactory:
    """Create the official Dhan client only when credentials are configured."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create(self) -> Any:
        client_id, access_token = _fresh_token(self.settings)
        if not (client_id and access_token):
            raise RuntimeError("Dhan credentials are not configured.")

        from dhanhq import DhanContext, dhanhq

        context = DhanContext(client_id, access_token)
        return dhanhq(context)
