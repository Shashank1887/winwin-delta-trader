"""Streamlit dashboard for Delta Exchange India Crypto Futures Auto-Trader & Screener."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from config import (
    AUTO_REFRESH_SECONDS,
    DEFAULT_CONTRACTS,
    DEFAULT_DAILY_PROFIT_TARGET_INR,
    DEFAULT_LEVERAGE,
    DEFAULT_MARGIN_INR,
    DEFAULT_MAX_DAILY_LOSS_INR,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    MAX_LEVERAGE,
    MIN_LEVERAGE,
    TradingConfig,
)
from delta_client import DeltaIndiaClient
from strategy import generate_signal
from trade_manager import TradeManager

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(
    page_title="Delta India Futures Auto-Trader",
    page_icon=":material/candlestick_chart:",
    layout="wide",
)


def _init_state() -> None:
    """Initialize shared configuration, client, and trade manager in session state."""
    if "config" not in st.session_state:
        st.session_state.config = TradingConfig()
    if "client" not in st.session_state:
        st.session_state.client = DeltaIndiaClient(st.session_state.config)
    if "trade_manager" not in st.session_state:
        st.session_state.trade_manager = TradeManager(
            client=st.session_state.client,
            config=st.session_state.config,
            mode="PAPER",
        )
    if "latest_mark_prices" not in st.session_state:
        st.session_state.latest_mark_prices = {}
    if "last_scan_at" not in st.session_state:
        st.session_state.last_scan_at = "Not scanned yet"


@st.cache_data(ttl=300)
def cached_public_ip(_client: DeltaIndiaClient) -> str:
    """Fetch and cache the machine's public IP for Delta whitelist verification."""
    return _client.get_public_ip()


@st.cache_data(ttl=60)
def cached_perpetual_universe(_client: DeltaIndiaClient) -> List[Dict[str, Any]]:
    """Fetch all operational perpetual futures from Delta India sorted by 24h volume."""
    try:
        tickers = _client.fetch_perpetual_tickers(operational_only=True)
        if tickers:
            return tickers
    except Exception:
        pass
    return [{"symbol": sym, "turnover_usd": 0.0} for sym in DEFAULT_CONTRACTS]


@st.cache_data(ttl=10)
def cached_market_snapshot(
    _client: DeltaIndiaClient,
    symbols: tuple[str, ...],
    timeframe: str,
) -> Dict[str, Any]:
    """Fetch 15m OHLCV candles, live mark prices, and strategy signals for selected contracts."""
    signals: Dict[str, Dict[str, Any]] = {}
    mark_prices: Dict[str, float] = {}
    errors: List[str] = []

    ticker_map: Dict[str, Dict[str, Any]] = {}
    try:
        for item in _client.fetch_perpetual_tickers(operational_only=True):
            ticker_map[str(item.get("symbol", "")).upper()] = item
    except Exception:
        ticker_map = {}

    if hasattr(_client, "fetch_ohlcv_batch"):
        ohlcv_map, batch_errors = _client.fetch_ohlcv_batch(
            list(symbols), timeframe=timeframe, limit=250
        )
        errors.extend(batch_errors)
    else:
        ohlcv_map = {}
        for sym in symbols:
            try:
                ohlcv_map[sym] = _client.fetch_ohlcv(sym, timeframe=timeframe, limit=250)
            except Exception as exc:
                errors.append(f"{sym}: {exc}")

    for sym in symbols:
        df = ohlcv_map.get(sym)
        if df is None:
            continue
        try:
            ticker = ticker_map.get(sym.upper())
            if ticker is None:
                ticker = _client.fetch_ticker(sym)
            mark = float(ticker.get("mark_price") or ticker.get("close") or 0.0)
            if mark <= 0 and not df.empty:
                mark = float(df["close"].iloc[-1])
            mark_prices[sym] = mark
            sig = generate_signal(df)
            sig["mark_price"] = mark
            sig["change_24h"] = float(ticker.get("mark_change_24h") or 0.0)
            sig["turnover_m_usd"] = round(float(ticker.get("turnover_usd") or 0.0) / 1_000_000.0, 2)
            signals[sym] = sig
        except Exception as exc:
            errors.append(f"{sym}: {exc}")

    return {"signals": signals, "mark_prices": mark_prices, "errors": errors}


@st.cache_data(ttl=15)
def cached_live_balance(_client: DeltaIndiaClient) -> Dict[str, Any]:
    """Fetch authenticated INR/USD wallet balance from Delta Exchange India."""
    return _client.fetch_inr_wallet_balance()


_init_state()
config: TradingConfig = st.session_state.config
client: DeltaIndiaClient = st.session_state.client
manager: TradeManager = st.session_state.trade_manager

# ---------------------------------------------------------------------------
# Sidebar: Trading Controls & Emergency Kill-Switch
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Trading controls", anchor=False)

    mode_choice = st.segmented_control(
        "Execution mode",
        options=["Paper Trading", "Live Trading"],
        default="Paper Trading",
        key="mode_choice",
    )

    live_ready = False
    if mode_choice == "Live Trading":
        if not config.has_credentials:
            st.warning(
                "Add `DELTA_API_KEY` and `DELTA_API_SECRET` in `.env` before enabling live orders.",
                icon=":material/key:",
            )
        live_confirmed = st.checkbox(
            "I confirm placing real orders on Delta India",
            value=False,
            key="live_confirm",
        )
        if live_confirmed and config.has_credentials:
            live_ready = True
            manager.set_mode("LIVE")
        else:
            manager.set_mode("PAPER")
            if not live_confirmed:
                st.info("Running in PAPER mode until live confirmation is checked.")
    else:
        manager.set_mode("PAPER")

    st.divider()

    universe = cached_perpetual_universe(client)
    all_symbols = [str(item["symbol"]) for item in universe]
    total_available = len(all_symbols)

    scan_scope = st.selectbox(
        "Market universe",
        options=[
            "Top 20 by Volume",
            "Top 50 by Volume",
            f"All Perpetuals ({total_available})",
            "Custom Selection",
        ],
        index=0,
        key="scan_scope",
    )

    if scan_scope == "Top 20 by Volume":
        selected_contracts = all_symbols[:20]
    elif scan_scope == "Top 50 by Volume":
        selected_contracts = all_symbols[:50]
    elif scan_scope.startswith("All Perpetuals"):
        selected_contracts = all_symbols
    else:
        default_custom = [s for s in DEFAULT_CONTRACTS if s in all_symbols] or all_symbols[:3]
        selected_contracts = st.multiselect(
            "Contracts",
            options=all_symbols,
            default=default_custom,
            key="selected_contracts",
        )
        if not selected_contracts:
            st.warning("Select at least one contract to scan.")

    st.caption(
        f"Scanning **{len(selected_contracts)}** of {total_available} live Delta India perpetuals "
        "(crypto, gold/silver & tokenized stocks)."
    )

    leverage = st.slider(
        "Leverage",
        min_value=MIN_LEVERAGE,
        max_value=MAX_LEVERAGE,
        value=DEFAULT_LEVERAGE,
        format="%dx",
        key="leverage_slider",
    )

    margin_inr = st.number_input(
        "Margin per trade (INR)",
        min_value=500.0,
        max_value=500000.0,
        value=DEFAULT_MARGIN_INR,
        step=500.0,
        key="margin_inr_input",
    )

    col_sl, col_tp = st.columns(2)
    with col_sl:
        sl_pct = st.number_input(
            "Stop-loss %",
            min_value=0.1,
            max_value=25.0,
            value=DEFAULT_STOP_LOSS_PCT,
            step=0.1,
            key="sl_pct_input",
        )
    with col_tp:
        tp_pct = st.number_input(
            "Take-profit %",
            min_value=0.1,
            max_value=50.0,
            value=DEFAULT_TAKE_PROFIT_PCT,
            step=0.1,
            key="tp_pct_input",
        )

    col_pt, col_ml = st.columns(2)
    with col_pt:
        daily_profit_target = st.number_input(
            "Daily target (INR)",
            min_value=100.0,
            max_value=100000.0,
            value=DEFAULT_DAILY_PROFIT_TARGET_INR,
            step=100.0,
            key="daily_target_input",
        )
    with col_ml:
        max_daily_loss = st.number_input(
            "Max daily loss (INR)",
            min_value=100.0,
            max_value=100000.0,
            value=DEFAULT_MAX_DAILY_LOSS_INR,
            step=100.0,
            key="max_loss_input",
        )

    auto_scan = st.toggle(
        f"Auto-scan every {AUTO_REFRESH_SECONDS}s",
        value=True,
        key="auto_scan_toggle",
    )

    st.divider()
    st.caption("Immediately closes all open positions and halts new entries.")
    if st.button(
        "EMERGENCY KILL-SWITCH",
        type="primary",
        icon=":material/emergency_home:",
        width="stretch",
        key="kill_switch_btn",
    ):
        kill_actions = manager.emergency_kill_switch(st.session_state.latest_mark_prices)
        for action in kill_actions:
            st.toast(action, icon=":material/warning:")
        st.rerun()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
    st.title("Delta India Futures Auto-Trader", anchor=False)
    with st.container(horizontal=True, vertical_alignment="center"):
        if manager.bot_halted:
            st.badge("HALTED", color="red", icon=":material/block:")
            if st.button("Resume bot", icon=":material/play_arrow:", key="resume_btn"):
                manager.resume_bot()
                st.rerun()
        else:
            st.badge(
                "LIVE ACTIVE" if live_ready else "PAPER ACTIVE",
                color="red" if live_ready else "green",
                icon=":material/bolt:",
            )

st.caption(
    f"Endpoint: `{config.base_url}` | Timeframe: `{config.timeframe}` | "
    "Strategy: Close vs 200 EMA + 9/21 EMA Cross + RSI(14)"
)

# ---------------------------------------------------------------------------
# Live Auto-Refresh Dashboard Fragment (scans every 15 seconds)
# ---------------------------------------------------------------------------
@st.fragment(run_every=AUTO_REFRESH_SECONDS)
def render_live_dashboard() -> None:
    symbols = tuple(selected_contracts)
    public_ip = cached_public_ip(client)

    manual_scan = False
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        st.caption(f"Last scan: {st.session_state.last_scan_at}")
        manual_scan = st.button("Scan now", icon=":material/refresh:", key="scan_now_btn")

    signals: Dict[str, Dict[str, Any]] = {}
    mark_prices: Dict[str, float] = {}
    fetch_errors: List[str] = []

    if symbols:
        if manual_scan:
            cached_market_snapshot.clear()
        snapshot = cached_market_snapshot(client, symbols, config.timeframe)
        signals = snapshot["signals"]
        mark_prices = snapshot["mark_prices"]
        fetch_errors = snapshot["errors"]
        st.session_state.latest_mark_prices = mark_prices
        st.session_state.last_scan_at = datetime.now(IST).strftime("%H:%M:%S IST")

        if auto_scan or manual_scan:
            tick_events = manager.on_tick(
                signals=signals,
                mark_prices=mark_prices,
                margin_inr=margin_inr,
                leverage=leverage,
                sl_pct=sl_pct,
                tp_pct=tp_pct,
                daily_profit_target_inr=daily_profit_target,
                max_daily_loss_inr=max_daily_loss,
            )
            for evt in tick_events:
                st.toast(evt, icon=":material/notifications:")

    for err in fetch_errors:
        st.warning(f"Market feed issue ({err})")

    # Pause / Halt banners
    if manager.bot_halted:
        st.error(
            f"Bot halted: {manager.halt_reason} Click **Resume bot** above to restart scanning.",
            icon=":material/emergency_home:",
        )
    else:
        paused, pause_reason = manager.check_daily_limits(
            daily_profit_target, max_daily_loss, mark_prices
        )
        if paused:
            st.warning(pause_reason, icon=":material/pause_circle:")

    # Balance & metrics
    metrics = manager.get_today_metrics(mark_prices)
    open_positions = manager.get_open_positions(mark_prices)

    if manager.mode == "PAPER":
        wallet_label = f"₹{manager.get_paper_balance_inr():,.0f}"
        mode_delta = "Virtual INR"
    else:
        try:
            bal = cached_live_balance(client)
            wallet_label = f"₹{bal['inr_available']:,.0f}"
            mode_delta = f"Delta {bal.get('currency', 'INR')}"
        except Exception:
            wallet_label = "API Error"
            mode_delta = "Check IP / Keys"

    # Top KPI Cards
    with st.container(horizontal=True):
        st.metric(
            label=f"Active mode: {manager.mode}",
            value=wallet_label,
            delta=mode_delta,
            delta_color="off",
            border=True,
        )
        pnl_val = metrics["today_net_pnl_inr"]
        st.metric(
            label="Today's net PnL",
            value=f"₹{pnl_val:+,.2f}",
            delta=f"{metrics['closed_trades_today']} closed",
            delta_color="normal" if pnl_val != 0 else "off",
            border=True,
        )
        st.metric(
            label="Win rate",
            value=f"{metrics['win_rate_pct']:.1f}%",
            delta=f"Target ₹{daily_profit_target:,.0f}",
            delta_color="off",
            border=True,
        )
        st.metric(
            label="Active positions",
            value=str(metrics["open_positions_count"]),
            delta=f"Max loss -₹{max_daily_loss:,.0f}",
            delta_color="off",
            border=True,
        )
        st.metric(
            label="Current public IP",
            value=public_ip,
            delta="For Delta whitelist",
            delta_color="off",
            border=True,
        )

    # 15m Market Screener
    with st.container(border=True):
        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            st.subheader("15m market screener", anchor=False)
            only_actionable = st.toggle(
                "Only BUY / SELL signals",
                value=False,
                key="only_actionable_toggle",
            )

        if signals:
            screener_rows = []
            for sym in symbols:
                info = signals.get(sym)
                if not info:
                    continue
                if only_actionable and info["signal"] == "HOLD":
                    continue
                screener_rows.append(
                    {
                        "Contract": sym,
                        "Mark price": info["mark_price"],
                        "24h %": info.get("change_24h", 0.0),
                        "24h Vol ($M)": info.get("turnover_m_usd", 0.0),
                        "9 EMA": info["ema_9"],
                        "21 EMA": info["ema_21"],
                        "200 EMA": info["ema_200"],
                        "RSI (14)": info["rsi_14"],
                        "Trend": info["trend"],
                        "Signal": info["signal"],
                        "Summary": info["reason"],
                    }
                )
            active_sig_count = sum(
                1 for info in signals.values() if info["signal"] in ("BUY", "SELL")
            )
            st.caption(
                f"Scanned **{len(signals)}** contracts | **{active_sig_count}** active BUY/SELL signal(s) | "
                "Actionable signals are sorted to the top."
            )
            if screener_rows:
                screener_df = pd.DataFrame(screener_rows)
                signal_rank = {"BUY": 0, "SELL": 0, "HOLD": 1}
                screener_df["_rank"] = screener_df["Signal"].map(signal_rank).fillna(2)
                screener_df = (
                    screener_df.sort_values(["_rank", "24h Vol ($M)"], ascending=[True, False])
                    .drop(columns=["_rank"])
                    .reset_index(drop=True)
                )
                st.dataframe(
                    screener_df,
                    hide_index=True,
                    column_config={
                        "Mark price": st.column_config.NumberColumn("Mark price ($)", format="$%.4f"),
                        "24h %": st.column_config.NumberColumn("24h %", format="%.2f%%"),
                        "24h Vol ($M)": st.column_config.NumberColumn("24h Vol ($M)", format="$%.2fM"),
                        "9 EMA": st.column_config.NumberColumn("9 EMA", format="$%.4f"),
                        "21 EMA": st.column_config.NumberColumn("21 EMA", format="$%.4f"),
                        "200 EMA": st.column_config.NumberColumn("200 EMA", format="$%.4f"),
                        "RSI (14)": st.column_config.NumberColumn("RSI (14)", format="%.1f"),
                    },
                    alt="15m crypto futures screener showing mark prices, EMAs, RSI, and trade signals",
                )
            else:
                st.info("No BUY or SELL crossover signals on this 15m tick. Turn off the filter to see all scanned contracts.")
        else:
            st.info("Select one or more contracts in the sidebar to view live signals.")

    # Active Positions & Intraday PnL Chart
    col_pos, col_chart = st.columns([1.25, 1.0])

    with col_pos:
        with st.container(border=True):
            st.subheader("Active positions", anchor=False)
            if open_positions:
                pos_df = pd.DataFrame(open_positions)
                display_cols = [
                    c
                    for c in (
                        "symbol",
                        "side",
                        "contracts",
                        "leverage",
                        "entry_price",
                        "mark_price",
                        "sl_price",
                        "tp_price",
                        "unrealized_pnl_inr",
                    )
                    if c in pos_df.columns
                ]
                st.dataframe(
                    pos_df[display_cols],
                    hide_index=True,
                    column_config={
                        "symbol": "Contract",
                        "side": "Side",
                        "contracts": "Qty",
                        "leverage": "Lev",
                        "entry_price": st.column_config.NumberColumn("Entry ($)", format="$%.2f"),
                        "mark_price": st.column_config.NumberColumn("Mark ($)", format="$%.2f"),
                        "sl_price": st.column_config.NumberColumn("SL ($)", format="$%.2f"),
                        "tp_price": st.column_config.NumberColumn("TP ($)", format="$%.2f"),
                        "unrealized_pnl_inr": st.column_config.NumberColumn(
                            "Unrealized PnL (INR)", format="₹%.2f"
                        ),
                    },
                    alt="Currently open futures positions with stop-loss, take-profit, and unrealized PnL",
                )

                with st.container(horizontal=True):
                    for pos in open_positions:
                        sym = pos["symbol"]
                        if st.button(
                            f"Close {sym}",
                            icon=":material/close:",
                            key=f"close_{sym}",
                        ):
                            manager.close_single_position(sym, mark_prices.get(sym))
                            st.toast(f"Closed {sym}", icon=":material/check_circle:")
                            st.rerun()
            else:
                st.info("No active positions right now. Waiting for a 15m BUY or SELL signal.")

    with col_chart:
        with st.container(border=True):
            st.subheader("Intraday cumulative PnL", anchor=False)
            pnl_df = metrics["intraday_series"]
            if not pnl_df.empty:
                chart = (
                    alt.Chart(pnl_df)
                    .mark_area(
                        line={"color": "#0F766E"},
                        color=alt.Gradient(
                            gradient="linear",
                            stops=[
                                alt.GradientStop(color="rgba(15, 118, 110, 0.35)", offset=0),
                                alt.GradientStop(color="rgba(15, 118, 110, 0.02)", offset=1),
                            ],
                            x1=1,
                            x2=1,
                            y1=1,
                            y2=0,
                        ),
                    )
                    .encode(
                        x=alt.X("timestamp:N", title="Trade time"),
                        y=alt.Y("cumulative_pnl_inr:Q", title="Cumulative PnL (INR)"),
                        tooltip=["timestamp", "cumulative_pnl_inr"],
                    )
                    .properties(height=240)
                )
                st.altair_chart(
                    chart,
                    alt="Intraday cumulative PnL progression in INR",
                )
            else:
                st.info("Intraday PnL chart will appear after your first closed trade today.")

    # Optional Contract Price + EMA Chart
    if signals:
        with st.expander("15m price & EMA chart", expanded=False):
            chart_symbol = st.selectbox(
                "Inspect contract",
                options=list(signals.keys()),
                key="chart_symbol_select",
            )
            sig_df = signals[chart_symbol]["df"].tail(80).copy()
            if not sig_df.empty:
                melted = sig_df.melt(
                    id_vars=["timestamp"],
                    value_vars=["close", "ema_9", "ema_21", "ema_200"],
                    var_name="Series",
                    value_name="Price",
                )
                price_chart = (
                    alt.Chart(melted)
                    .mark_line()
                    .encode(
                        x=alt.X("timestamp:T", title="Time (IST)"),
                        y=alt.Y("Price:Q", title="Price ($)", scale=alt.Scale(zero=False)),
                        color=alt.Color("Series:N", title="Indicator"),
                    )
                    .properties(height=280)
                )
                st.altair_chart(
                    price_chart,
                    alt=f"15m close price and 9, 21, 200 EMAs for {chart_symbol}",
                )

    # Trade History & Activity Log
    tab_trades, tab_events = st.tabs(["Trade history", "Bot activity log"])

    with tab_trades:
        trades_df = manager.load_trades_df()
        if not trades_df.empty:
            recent = trades_df.iloc[::-1].reset_index(drop=True)
            st.dataframe(
                recent,
                hide_index=True,
                column_config={
                    "margin_inr": st.column_config.NumberColumn("Margin (INR)", format="₹%.0f"),
                    "entry_price": st.column_config.NumberColumn("Entry ($)", format="$%.4f"),
                    "exit_price": st.column_config.NumberColumn("Exit ($)", format="$%.4f"),
                    "sl_price": st.column_config.NumberColumn("SL ($)", format="$%.4f"),
                    "tp_price": st.column_config.NumberColumn("TP ($)", format="$%.4f"),
                    "pnl_inr": st.column_config.NumberColumn("PnL (INR)", format="₹%.2f"),
                },
                alt="Complete trade log from delta_trades_log.csv",
            )
            st.download_button(
                "Download trades CSV",
                data=trades_df.to_csv(index=False).encode("utf-8"),
                file_name="delta_trades_log.csv",
                mime="text/csv",
                icon=":material/download:",
                key="download_trades_csv",
            )
        else:
            st.info("No trades logged yet. All paper and live trades save to `delta_trades_log.csv`.")

    with tab_events:
        if manager.event_log:
            st.code("\n".join(manager.event_log), language="text")
        else:
            st.info("Scanner events, SL/TP triggers, and limit alerts will appear here.")


render_live_dashboard()
