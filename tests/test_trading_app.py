"""Unit and Streamlit AppTest suite for Delta Exchange India Auto-Trader."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest

from config import (
    DELTA_INDIA_BASE_URL,
    DEFAULT_CONTRACTS,
    DEFAULT_DAILY_PROFIT_TARGET_INR,
    DEFAULT_LEVERAGE,
    DEFAULT_MARGIN_INR,
    DEFAULT_MAX_DAILY_LOSS_INR,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_TIMEFRAME,
    PAPER_STARTING_BALANCE_INR,
    TradingConfig,
)
from delta_client import DeltaIndiaClient
from strategy import generate_signal
from trade_manager import TradeManager


def _make_ohlcv_df(closes: List[float]) -> pd.DataFrame:
    start = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
    rows = []
    for idx, c in enumerate(closes):
        ts = start + timedelta(minutes=15 * idx)
        rows.append(
            {
                "timestamp": ts,
                "open": c,
                "high": c * 1.002,
                "low": c * 0.998,
                "close": c,
                "volume": 1000.0 + idx,
            }
        )
    return pd.DataFrame(rows)


def test_config_defaults() -> None:
    cfg = TradingConfig(api_key="", api_secret="")
    assert cfg.base_url == DELTA_INDIA_BASE_URL == "https://api.india.delta.exchange"
    assert cfg.contracts == ["BTCUSD", "ETHUSD", "SOLUSD"] == DEFAULT_CONTRACTS
    assert cfg.timeframe == DEFAULT_TIMEFRAME == "15m"
    assert cfg.margin_inr == DEFAULT_MARGIN_INR == 5000.0
    assert cfg.leverage == DEFAULT_LEVERAGE == 3
    assert cfg.stop_loss_pct == DEFAULT_STOP_LOSS_PCT == 1.0
    assert cfg.take_profit_pct == DEFAULT_TAKE_PROFIT_PCT == 2.0
    assert cfg.daily_profit_target_inr == DEFAULT_DAILY_PROFIT_TARGET_INR == 800.0
    assert cfg.max_daily_loss_inr == DEFAULT_MAX_DAILY_LOSS_INR == 500.0
    assert cfg.paper_starting_balance_inr == PAPER_STARTING_BALANCE_INR == 30000.0
    assert cfg.has_credentials is False


def test_delta_client_ccxt_india_urls_and_hmac_signature() -> None:
    cfg = TradingConfig(api_key="key123", api_secret="secret456")
    client = DeltaIndiaClient(cfg)
    ex = client._get_ccxt()
    assert ex.urls["api"]["public"] == "https://api.india.delta.exchange"
    assert ex.urls["api"]["private"] == "https://api.india.delta.exchange"

    headers = client.build_signature(
        method="POST",
        path="/v2/orders",
        query_string="",
        payload='{"size":1}',
        timestamp="1700000000",
    )
    expected_msg = 'POST1700000000/v2/orders{"size":1}'
    expected_sig = hmac.new(
        b"secret456", expected_msg.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert headers["api-key"] == "key123"
    assert headers["timestamp"] == "1700000000"
    assert headers["signature"] == expected_sig


def test_contract_sizing_and_bracket_order_payload() -> None:
    cfg = TradingConfig(api_key="k", api_secret="s", usd_to_inr_rate=85.0)
    client = DeltaIndiaClient(cfg)

    # Notional = (5000 / 85) * 3 = 176.47 USD; 1 BTCUSD contract = 60000 * 0.001 = 60 USD -> 2 contracts
    contracts = client.calculate_contract_size(
        symbol="BTCUSD",
        margin_inr=5000.0,
        leverage=3,
        mark_price=60000.0,
        contract_value=0.001,
    )
    assert contracts == 2

    client.fetch_ticker = MagicMock(
        return_value={"product_id": 27, "tick_size": "0.5", "contract_value": "0.001"}
    )
    client._request = MagicMock(return_value={"id": 999, "average_fill_price": "60000"})

    resp = client.place_bracket_order(
        symbol="BTCUSD",
        side="BUY",
        size=2,
        stop_loss_price=59400.12,
        take_profit_price=61200.49,
        leverage=3,
    )
    assert resp["id"] == 999
    assert client._request.call_count == 2
    lev_call, order_call = client._request.call_args_list
    assert lev_call.args == ("POST", "/v2/products/27/orders/leverage")
    assert lev_call.kwargs["body"] == {"leverage": "3"}
    assert order_call.args == ("POST", "/v2/orders")
    assert order_call.kwargs["body"]["order_type"] == "market_order"
    assert order_call.kwargs["body"]["bracket_stop_loss_price"] == "59400.0"
    assert order_call.kwargs["body"]["bracket_take_profit_price"] == "61200.5"


def test_strategy_generates_buy_sell_and_hold() -> None:
    # Construct BUY crossover: rising trend -> brief pullback (9 EMA < 21 EMA) -> sharp bounce above 200 EMA
    base = np.linspace(100.0, 140.0, 220).tolist()
    pullback = [139.0, 137.0, 135.0, 134.0, 133.0, 132.0]
    bounce = [136.0, 142.0, 148.0]
    buy_df = _make_ohlcv_df(base + pullback + bounce)
    buy_res = generate_signal(buy_df)
    assert buy_res["signal"] == "BUY"
    assert buy_res["close"] > buy_res["ema_200"]
    assert buy_res["ema_9"] > buy_res["ema_21"]
    assert buy_res["rsi_14"] > 55.0

    # Construct SELL crossover: falling trend -> brief bounce (9 EMA > 21 EMA) -> sharp drop below 200 EMA
    down_base = np.linspace(140.0, 100.0, 220).tolist()
    mini_bounce = [101.0, 103.0, 105.0, 106.0, 107.0, 108.0]
    drop = [104.0, 98.0, 92.0]
    sell_df = _make_ohlcv_df(down_base + mini_bounce + drop)
    sell_res = generate_signal(sell_df)
    assert sell_res["signal"] == "SELL"
    assert sell_res["close"] < sell_res["ema_200"]
    assert sell_res["ema_9"] < sell_res["ema_21"]
    assert sell_res["rsi_14"] < 45.0

    # Flat series -> HOLD
    hold_df = _make_ohlcv_df([100.0] * 225)
    hold_res = generate_signal(hold_df)
    assert hold_res["signal"] == "HOLD"


def test_paper_trade_lifecycle_tp_sl_duplicate_guard_and_limits(tmp_path: Path) -> None:
    log_file = tmp_path / "delta_trades_log.csv"
    cfg = TradingConfig(api_key="", api_secret="", trades_log_path=log_file)
    client = DeltaIndiaClient(cfg)
    client.calculate_contract_size = MagicMock(return_value=2)

    tm = TradeManager(client=client, config=cfg, mode="PAPER")
    assert tm.get_paper_balance_inr() == 30000.0

    # Tick 1: BUY signal opens BTCUSD at 60000
    tm.on_tick(
        signals={"BTCUSD": {"signal": "BUY"}},
        mark_prices={"BTCUSD": 60000.0},
        margin_inr=5000.0,
        leverage=3,
        sl_pct=1.0,
        tp_pct=2.0,
        daily_profit_target_inr=800.0,
        max_daily_loss_inr=500.0,
    )
    assert len(tm.paper_positions) == 1
    pos = tm.paper_positions["BTCUSD"]
    assert pos["sl_price"] == 59400.0
    assert pos["tp_price"] == 61200.0

    # Tick 2: Another BUY signal on BTCUSD while open should NOT create duplicate
    tm.on_tick(
        signals={"BTCUSD": {"signal": "BUY"}},
        mark_prices={"BTCUSD": 60300.0},
        margin_inr=5000.0,
        leverage=3,
        sl_pct=1.0,
        tp_pct=2.0,
        daily_profit_target_inr=800.0,
        max_daily_loss_inr=500.0,
    )
    assert len(tm.paper_positions) == 1

    # Tick 3: Price hits TP (61200) -> closes with +300 INR (5000 * 3 * 2%)
    tm.on_tick(
        signals={"BTCUSD": {"signal": "HOLD"}},
        mark_prices={"BTCUSD": 61200.0},
        margin_inr=5000.0,
        leverage=3,
        sl_pct=1.0,
        tp_pct=2.0,
        daily_profit_target_inr=800.0,
        max_daily_loss_inr=500.0,
    )
    assert len(tm.paper_positions) == 0
    assert tm.get_paper_balance_inr() == 30300.0

    csv_df = pd.read_csv(log_file)
    assert len(csv_df) == 1
    assert csv_df.iloc[0]["status"] == "CLOSED"
    assert csv_df.iloc[0]["exit_reason"] == "TAKE_PROFIT"
    assert float(csv_df.iloc[0]["pnl_inr"]) == 300.0

    # Daily profit target reached (set target = 300): new signal must be paused
    tm.on_tick(
        signals={"ETHUSD": {"signal": "SELL"}},
        mark_prices={"ETHUSD": 2000.0},
        margin_inr=5000.0,
        leverage=3,
        sl_pct=1.0,
        tp_pct=2.0,
        daily_profit_target_inr=300.0,
        max_daily_loss_inr=500.0,
    )
    assert len(tm.paper_positions) == 0


def test_emergency_kill_switch_paper_and_live(tmp_path: Path) -> None:
    log_file = tmp_path / "delta_trades_log.csv"
    cfg = TradingConfig(api_key="k", api_secret="s", trades_log_path=log_file)
    client = DeltaIndiaClient(cfg)
    client.calculate_contract_size = MagicMock(return_value=1)
    client.close_all_positions = MagicMock(return_value=[{"close_all": "ok"}])

    tm = TradeManager(client=client, config=cfg, mode="PAPER")
    tm.on_tick(
        signals={"SOLUSD": {"signal": "BUY"}},
        mark_prices={"SOLUSD": 100.0},
        margin_inr=5000.0,
        leverage=3,
        sl_pct=1.0,
        tp_pct=2.0,
        daily_profit_target_inr=800.0,
        max_daily_loss_inr=500.0,
    )
    assert len(tm.paper_positions) == 1

    tm.emergency_kill_switch(mark_prices={"SOLUSD": 101.0})
    assert tm.bot_halted is True
    assert len(tm.paper_positions) == 0
    assert tm.get_paper_balance_inr() == 30150.0


def test_streamlit_app_renders_and_kill_switch_works(tmp_path: Path) -> None:
    sample_df = _make_ohlcv_df(np.linspace(60000.0, 61000.0, 230).tolist())

    cfg = TradingConfig(
        api_key="",
        api_secret="",
        trades_log_path=tmp_path / "app_test_trades.csv",
    )
    mock_client = MagicMock(spec=DeltaIndiaClient)
    mock_client.config = cfg
    mock_client.get_public_ip.return_value = "203.0.113.10"
    mock_client.fetch_ticker.return_value = {
        "mark_price": 61000.0,
        "mark_change_24h": 1.5,
        "product_id": 27,
        "contract_value": 0.001,
        "tick_size": 0.5,
    }
    mock_client.fetch_perpetual_tickers.return_value = [
        {
            "symbol": "BTCUSD",
            "mark_price": 61000.0,
            "mark_change_24h": 1.5,
            "turnover_usd": 1480000000.0,
            "product_id": 27,
            "contract_value": 0.001,
            "tick_size": 0.5,
            "product_trading_status": "operational",
        },
        {
            "symbol": "XAUTUSD",
            "mark_price": 2650.0,
            "mark_change_24h": 0.8,
            "turnover_usd": 1320000000.0,
            "product_id": 1001,
            "contract_value": 0.01,
            "tick_size": 0.1,
            "product_trading_status": "operational",
        },
    ]
    mock_client.fetch_ohlcv_batch.return_value = (
        {"BTCUSD": sample_df, "XAUTUSD": sample_df},
        [],
    )
    mock_client.fetch_ohlcv.return_value = sample_df
    mock_client.calculate_contract_size.return_value = 2

    app_path = Path(__file__).resolve().parent.parent / "app.py"
    at = AppTest.from_file(str(app_path), default_timeout=30)
    at.session_state["config"] = cfg
    at.session_state["client"] = mock_client
    at.session_state["trade_manager"] = TradeManager(client=mock_client, config=cfg, mode="PAPER")

    at.run(timeout=30)
    assert not at.exception
    assert len(at.metric) >= 5
    assert at.metric[4].value == "203.0.113.10"

    # Click EMERGENCY KILL-SWITCH in sidebar
    at.button(key="kill_switch_btn").click().run(timeout=30)
    assert not at.exception
    assert at.session_state["trade_manager"].bot_halted is True


def test_fetch_perpetual_tickers_and_batch_sorting() -> None:
    cfg = TradingConfig(api_key="", api_secret="")
    client = DeltaIndiaClient(cfg)
    client._request = MagicMock(
        return_value=[
            {
                "symbol": "SOLUSD",
                "turnover_usd": "1000000",
                "product_trading_status": "operational",
            },
            {
                "symbol": "DEADUSD",
                "turnover_usd": "99999999",
                "product_trading_status": "halted",
            },
            {
                "symbol": "BTCUSD",
                "turnover_usd": "50000000",
                "product_trading_status": "operational",
            },
        ]
    )
    tickers = client.fetch_perpetual_tickers(operational_only=True)
    assert [t["symbol"] for t in tickers] == ["BTCUSD", "SOLUSD"]

    sample_df = _make_ohlcv_df(np.linspace(100.0, 110.0, 30).tolist())
    client.fetch_ohlcv = MagicMock(return_value=sample_df)
    frames, errs = client.fetch_ohlcv_batch(["BTCUSD", "SOLUSD"], timeframe="15m", limit=30)
    assert errs == []
    assert set(frames.keys()) == {"BTCUSD", "SOLUSD"}
