"""Technical indicator computation and 15m EMA/RSI signal generation."""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

try:
    import pandas_ta as ta  # type: ignore
except ImportError:
    ta = None


def _compute_ema(series: pd.Series, length: int) -> pd.Series:
    """Compute EMA using pandas_ta when available, otherwise exact pandas ewm."""
    if ta is not None:
        result = ta.ema(series, length=length)
        if result is not None:
            return result
    return series.ewm(span=length, adjust=False).mean()


def _compute_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """Compute Wilder's RSI(14) using pandas_ta when available, otherwise exact RMA."""
    if ta is not None:
        result = ta.rsi(series, length=length)
        if result is not None:
            return result

    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return rsi


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Append EMA(9), EMA(21), EMA(200), and RSI(14) columns to an OHLCV DataFrame."""
    if df.empty:
        out = df.copy()
        for col in ("ema_9", "ema_21", "ema_200", "rsi_14"):
            out[col] = pd.Series(dtype=float)
        return out

    out = df.copy()
    if "timestamp" in out.columns:
        out = out.sort_values("timestamp").reset_index(drop=True)

    close = out["close"].astype(float)
    out["ema_9"] = _compute_ema(close, 9)
    out["ema_21"] = _compute_ema(close, 21)
    out["ema_200"] = _compute_ema(close, 200)
    out["rsi_14"] = _compute_rsi(close, 14)
    return out


def generate_signal(df: pd.DataFrame) -> Dict[str, Any]:
    """Evaluate BUY / SELL / HOLD rules on the latest 15m candle.

    Rules:
    - BUY (Long): Close > 200 EMA, 9 EMA crosses above 21 EMA, and RSI(14) > 55.
    - SELL (Short): Close < 200 EMA, 9 EMA crosses below 21 EMA, and RSI(14) < 45.
    """
    enriched = compute_indicators(df)
    if len(enriched) < 22:
        return {
            "signal": "HOLD",
            "close": float(enriched["close"].iloc[-1]) if not enriched.empty else 0.0,
            "ema_9": None,
            "ema_21": None,
            "ema_200": None,
            "rsi_14": None,
            "trend": "UNKNOWN",
            "crossover": "NONE",
            "reason": "Waiting for sufficient candle history",
            "df": enriched,
        }

    curr = enriched.iloc[-1]
    prev = enriched.iloc[-2]

    close = float(curr["close"])
    ema_9 = float(curr["ema_9"])
    ema_21 = float(curr["ema_21"])
    ema_200 = float(curr["ema_200"])
    rsi_14 = float(curr["rsi_14"]) if pd.notna(curr["rsi_14"]) else 50.0

    prev_ema_9 = float(prev["ema_9"])
    prev_ema_21 = float(prev["ema_21"])

    bullish_cross = prev_ema_9 <= prev_ema_21 and ema_9 > ema_21
    bearish_cross = prev_ema_9 >= prev_ema_21 and ema_9 < ema_21

    trend = "BULLISH" if close > ema_200 else "BEARISH"
    if bullish_cross:
        crossover = "BULLISH_CROSS"
    elif bearish_cross:
        crossover = "BEARISH_CROSS"
    else:
        crossover = "NONE"

    signal = "HOLD"
    if close > ema_200 and bullish_cross and rsi_14 > 55.0:
        signal = "BUY"
        reason = f"Close > 200 EMA, 9/21 EMA bullish cross, RSI {rsi_14:.1f} > 55"
    elif close < ema_200 and bearish_cross and rsi_14 < 45.0:
        signal = "SELL"
        reason = f"Close < 200 EMA, 9/21 EMA bearish cross, RSI {rsi_14:.1f} < 45"
    else:
        checks = []
        checks.append("Above 200 EMA" if close > ema_200 else "Below 200 EMA")
        if bullish_cross:
            checks.append("9/21 Bullish Cross")
        elif bearish_cross:
            checks.append("9/21 Bearish Cross")
        else:
            checks.append("No 9/21 cross")
        checks.append(f"RSI {rsi_14:.1f}")
        reason = " | ".join(checks)

    return {
        "signal": signal,
        "close": close,
        "ema_9": ema_9,
        "ema_21": ema_21,
        "ema_200": ema_200,
        "rsi_14": rsi_14,
        "trend": trend,
        "crossover": crossover,
        "reason": reason,
        "df": enriched,
    }
