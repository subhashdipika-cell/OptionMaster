from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    dhan_client_id: str | None = None
    dhan_access_token: str | None = None
    # Fleet-shared token file, auto-refreshed daily at 06:30 by AlphaEdge's
    # strategy-lab (TOTP). Read at client-creation time so OptionMaster never
    # runs on a stale token; .env values are only a fallback.
    dhan_shared_token_file: str = r"D:\alphaedge\strategy-lab\dhan_config.json"
    execution_mode: Literal["PAPER", "LIVE"] = "PAPER"
    host: str = "127.0.0.1"
    port: int = 8300
    frontend_port: int = 5275
    underlyings: str = "NIFTY,BANKNIFTY"
    india_vix_security_id: int = 21
    india_vix_segment: str = "IDX_I"
    india_vix_instrument_type: str = "INDEX"
    scrip_master_dir: str = "data/scrip-master"
    nse_option_brokerage_per_order: float = 20.0
    nse_option_exchange_transaction_rate: float = 0.0003503
    bse_option_exchange_transaction_rate: float = 0.000325  # SENSEX / BANKEX
    nse_option_ipft_rate: float = 0.0000050
    nse_option_sebi_turnover_rate: float = 0.0000010
    nse_option_gst_rate: float = 0.18
    nse_option_stt_sell_rate: float = 0.0010  # 0.10% since 2024-10-01
    nse_option_stamp_duty_buy_rate: float = 0.00003
    nse_option_clearing_charge_per_order: float = 0.0
    journal_path: str = "data/optionmaster.db"
    stored_data_dir: str = "D:/alphaedge/strategy-lab/data/options"
    # M1 index candles live one level up from the option snapshots; the sweep
    # reversal strategy reads its signal from these.
    spot_data_dir: str = "D:/alphaedge/strategy-lab/data"
    backtest_lot_sizes: str = "NIFTY50:75,BANKNIFTY:35,SENSEX:20,FINNIFTY:65"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # ── Autonomous paper trader (workers/auto_trader.py) ──────────────────
    # Paper-only by design: the worker reuses the same analyze->gate->paper
    # pipeline as the dashboard; it never touches the Super Order builder.
    auto_trader_enabled: bool = True          # default; runtime toggle persists in data/auto_trader.json
    auto_symbol: str = "NIFTY"
    auto_security_id: int = 13                # Dhan NIFTY 50 (IDX_I)
    auto_segment: str = "IDX_I"
    auto_instrument_type: str = "INDEX"
    auto_lots: int = 1
    auto_capital: float = 100_000.0
    auto_max_risk_fraction: float = 0.02
    auto_daily_loss_fraction: float = 0.05
    auto_max_premium_fraction: float = 0.30
    auto_max_trades_per_day: int = 3

    # Local Ollama reviewer: research-only; never an execution authority.
    ollama_enabled: bool = True
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:latest"
    ollama_timeout_seconds: float = 60.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OPTIONMASTER_",
        extra="ignore",
    )

    @property
    def underlying_symbols(self) -> list[str]:
        return [item.strip().upper() for item in self.underlyings.split(",") if item.strip()]

    @property
    def dhan_configured(self) -> bool:
        return bool(self.dhan_client_id and self.dhan_access_token)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def lot_size_map(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for item in self.backtest_lot_sizes.split(","):
            if ":" in item:
                symbol, _, value = item.strip().partition(":")
                try:
                    sizes[symbol.strip().upper()] = int(value)
                except ValueError:
                    continue
        return sizes


@lru_cache
def get_settings() -> Settings:
    return Settings()
