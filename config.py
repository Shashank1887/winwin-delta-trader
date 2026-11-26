"""Application configuration and trading defaults for Delta Exchange India."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

DELTA_INDIA_BASE_URL = "https://api.india.delta.exchange"

DEFAULT_CONTRACTS: List[str] = ["BTCUSD", "ETHUSD", "SOLUSD"]
DEFAULT_TIMEFRAME = "15m"

DEFAULT_MARGIN_INR = 5000.0
DEFAULT_LEVERAGE = 3
MIN_LEVERAGE = 1
MAX_LEVERAGE = 5

DEFAULT_STOP_LOSS_PCT = 1.0
DEFAULT_TAKE_PROFIT_PCT = 2.0

DEFAULT_DAILY_PROFIT_TARGET_INR = 800.0
DEFAULT_MAX_DAILY_LOSS_INR = 500.0

PAPER_STARTING_BALANCE_INR = 30000.0
AUTO_REFRESH_SECONDS = 15

TRADES_LOG_PATH = BASE_DIR / "delta_trades_log.csv"


def _get_setting(key: str, default: str = "") -> str:
    """Read setting from environment (.env) first, falling back to Streamlit Cloud secrets."""
    val = os.getenv(key, "").strip()
    if val:
        return val
    try:
        import streamlit as st

        if st.runtime.exists() and key in st.secrets:
            return str(st.secrets[key]).strip()
    except Exception:
        pass
    return default


@dataclass
class TradingConfig:
    """Runtime configuration shared across the client, strategy, and trade manager."""

    api_key: str = field(default_factory=lambda: _get_setting("DELTA_API_KEY"))
    api_secret: str = field(default_factory=lambda: _get_setting("DELTA_API_SECRET"))
    base_url: str = DELTA_INDIA_BASE_URL
    contracts: List[str] = field(default_factory=lambda: list(DEFAULT_CONTRACTS))
    timeframe: str = DEFAULT_TIMEFRAME
    margin_inr: float = DEFAULT_MARGIN_INR
    leverage: int = DEFAULT_LEVERAGE
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT
    daily_profit_target_inr: float = DEFAULT_DAILY_PROFIT_TARGET_INR
    max_daily_loss_inr: float = DEFAULT_MAX_DAILY_LOSS_INR
    paper_starting_balance_inr: float = PAPER_STARTING_BALANCE_INR
    usd_to_inr_rate: float = field(
        default_factory=lambda: float(_get_setting("USD_TO_INR_RATE", "85.0"))
    )
    auto_refresh_seconds: int = AUTO_REFRESH_SECONDS
    trades_log_path: Path = TRADES_LOG_PATH

    @property
    def has_credentials(self) -> bool:
        """Return True when both API key and secret look configured."""
        return (
            bool(self.api_key)
            and bool(self.api_secret)
            and "your_delta_india" not in self.api_key
            and "your_delta_india" not in self.api_secret
        )
