"""Trade lifecycle manager supporting PAPER and LIVE modes with CSV persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from config import TradingConfig
from delta_client import DeltaAPIError, DeltaIndiaClient

IST = ZoneInfo("Asia/Kolkata")

LOG_COLUMNS = [
    "trade_id",
    "timestamp",
    "mode",
    "symbol",
    "side",
    "contracts",
    "leverage",
    "margin_inr",
    "entry_price",
    "exit_price",
    "sl_price",
    "tp_price",
    "status",
    "exit_reason",
    "pnl_inr",
    "order_id",
]


class TradeManager:
    """Coordinates Paper and Live execution, SL/TP tracking, daily limits, and CSV logs."""

    def __init__(
        self,
        client: DeltaIndiaClient,
        config: Optional[TradingConfig] = None,
        mode: str = "PAPER",
    ) -> None:
        self.client = client
        self.config = config or client.config
        self.mode = mode.upper()
        self.log_path: Path = Path(self.config.trades_log_path)
        self.bot_halted: bool = False
        self.halt_reason: str = ""
        self.event_log: List[str] = []

        self._ensure_log_file()
        self.paper_positions: Dict[str, Dict[str, Any]] = self._load_open_paper_positions()

    def _ensure_log_file(self) -> None:
        """Create delta_trades_log.csv with headers if it does not exist yet."""
        if not self.log_path.exists():
            pd.DataFrame(columns=LOG_COLUMNS).to_csv(self.log_path, index=False)

    def load_trades_df(self) -> pd.DataFrame:
        """Read all logged trades from CSV with consistent column dtypes."""
        self._ensure_log_file()
        str_cols = (
            "trade_id",
            "timestamp",
            "mode",
            "symbol",
            "side",
            "status",
            "exit_reason",
            "order_id",
        )
        num_cols = (
            "contracts",
            "leverage",
            "margin_inr",
            "entry_price",
            "exit_price",
            "sl_price",
            "tp_price",
            "pnl_inr",
        )
        dtype_map = {col: "string" for col in str_cols}
        df = pd.read_csv(self.log_path, dtype=dtype_map)
        for col in LOG_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA
        for col in str_cols:
            df[col] = df[col].astype("string").fillna("")
        for col in num_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[LOG_COLUMNS]

    def _save_trades_df(self, df: pd.DataFrame) -> None:
        """Persist updated trades DataFrame to CSV."""
        df[LOG_COLUMNS].to_csv(self.log_path, index=False)

    def _load_open_paper_positions(self) -> Dict[str, Dict[str, Any]]:
        """Rehydrate any OPEN paper trades from CSV across restarts."""
        df = self.load_trades_df()
        if df.empty:
            return {}
        open_rows = df[(df["mode"] == "PAPER") & (df["status"] == "OPEN")]
        positions: Dict[str, Dict[str, Any]] = {}
        for _, row in open_rows.iterrows():
            symbol = str(row["symbol"])
            positions[symbol] = {
                "trade_id": str(row["trade_id"]),
                "timestamp": str(row["timestamp"]),
                "symbol": symbol,
                "side": str(row["side"]),
                "contracts": int(row["contracts"]),
                "leverage": int(row["leverage"]),
                "margin_inr": float(row["margin_inr"]),
                "entry_price": float(row["entry_price"]),
                "sl_price": float(row["sl_price"]),
                "tp_price": float(row["tp_price"]),
                "mark_price": float(row["entry_price"]),
                "unrealized_pnl_inr": 0.0,
            }
        return positions

    def _append_trade_row(self, row: Dict[str, Any]) -> None:
        df = self.load_trades_df()
        new_df = pd.DataFrame([row], columns=LOG_COLUMNS)
        combined = new_df if df.empty else pd.concat([df, new_df], ignore_index=True)
        self._save_trades_df(combined)

    def _update_trade_row(self, trade_id: str, updates: Dict[str, Any]) -> None:
        df = self.load_trades_df()
        mask = df["trade_id"] == trade_id
        if not mask.any():
            return
        for key, value in updates.items():
            df.loc[mask, key] = value
        self._save_trades_df(df)

    def _record_event(self, message: str) -> str:
        stamp = datetime.now(IST).strftime("%H:%M:%S")
        entry = f"[{stamp}] {message}"
        self.event_log.insert(0, entry)
        self.event_log = self.event_log[:50]
        return entry

    def set_mode(self, mode: str) -> None:
        """Switch between PAPER and LIVE execution modes."""
        normalized = mode.upper()
        if normalized not in ("PAPER", "LIVE"):
            raise ValueError(f"Unsupported mode: {mode}")
        if normalized != self.mode:
            self.mode = normalized
            self._record_event(f"Switched trading mode to {self.mode}")

    @staticmethod
    def calculate_pnl_inr(
        side: str,
        entry_price: float,
        current_price: float,
        margin_inr: float,
        leverage: int,
    ) -> float:
        """Compute leveraged INR PnL from entry and current/exit prices."""
        if entry_price <= 0:
            return 0.0
        direction = 1.0 if side.upper() == "BUY" else -1.0
        pct_move = ((current_price - entry_price) / entry_price) * direction
        return round(margin_inr * leverage * pct_move, 2)

    @staticmethod
    def compute_sl_tp(
        side: str,
        entry_price: float,
        sl_pct: float,
        tp_pct: float,
    ) -> Tuple[float, float]:
        """Return (stop_loss_price, take_profit_price) for BUY or SELL."""
        if side.upper() == "BUY":
            sl_price = entry_price * (1.0 - sl_pct / 100.0)
            tp_price = entry_price * (1.0 + tp_pct / 100.0)
        else:
            sl_price = entry_price * (1.0 + sl_pct / 100.0)
            tp_price = entry_price * (1.0 - tp_pct / 100.0)
        return round(sl_price, 4), round(tp_price, 4)

    def get_paper_balance_inr(self) -> float:
        """Return virtual INR cash + realized PnL (starting ₹30,000)."""
        df = self.load_trades_df()
        realized = 0.0
        if not df.empty:
            closed_paper = df[(df["mode"] == "PAPER") & (df["status"] == "CLOSED")]
            realized = float(pd.to_numeric(closed_paper["pnl_inr"], errors="coerce").fillna(0.0).sum())
        return round(self.config.paper_starting_balance_inr + realized, 2)

    def get_open_positions(self, mark_prices: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
        """Return active positions for the active mode with live unrealized PnL."""
        mark_prices = mark_prices or {}
        if self.mode == "PAPER":
            positions: List[Dict[str, Any]] = []
            for sym, pos in self.paper_positions.items():
                mark = float(mark_prices.get(sym, pos.get("mark_price", pos["entry_price"])))
                unrealized = self.calculate_pnl_inr(
                    pos["side"],
                    pos["entry_price"],
                    mark,
                    pos["margin_inr"],
                    pos["leverage"],
                )
                pos["mark_price"] = mark
                pos["unrealized_pnl_inr"] = unrealized
                positions.append(dict(pos))
            return positions

        if not self.config.has_credentials:
            return []
        return self.client.fetch_open_positions()

    def _reconcile_live_closed_trades(
        self, live_positions: List[Dict[str, Any]], mark_prices: Dict[str, float]
    ) -> None:
        """Mark OPEN LIVE CSV rows as CLOSED if the exchange position was closed by SL/TP."""
        df = self.load_trades_df()
        if df.empty:
            return
        open_live_rows = df[(df["mode"] == "LIVE") & (df["status"] == "OPEN")]
        if open_live_rows.empty:
            return

        active_symbols = {p["symbol"].upper() for p in live_positions}
        for _, row in open_live_rows.iterrows():
            sym = str(row["symbol"]).upper()
            if sym not in active_symbols:
                exit_px = float(mark_prices.get(sym, row["entry_price"]))
                pnl = self.calculate_pnl_inr(
                    str(row["side"]),
                    float(row["entry_price"]),
                    exit_px,
                    float(row["margin_inr"]),
                    int(row["leverage"]),
                )
                self._update_trade_row(
                    str(row["trade_id"]),
                    {
                        "exit_price": exit_px,
                        "status": "CLOSED",
                        "exit_reason": "EXCHANGE_CLOSED",
                        "pnl_inr": pnl,
                    },
                )
                self._record_event(f"LIVE {sym} position closed on exchange (est. PnL ₹{pnl:+,.2f})")

    def get_today_metrics(self, mark_prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Calculate today's realized + unrealized PnL, win rate, and intraday PnL series."""
        df = self.load_trades_df()
        today_str = datetime.now(IST).strftime("%Y-%m-%d")

        closed_today = pd.DataFrame(columns=LOG_COLUMNS)
        if not df.empty:
            mode_mask = df["mode"] == self.mode
            status_mask = df["status"] == "CLOSED"
            date_mask = df["timestamp"].astype(str).str.startswith(today_str)
            closed_today = df[mode_mask & status_mask & date_mask].copy()

        realized_today = 0.0
        win_rate = 0.0
        intraday_series = pd.DataFrame(columns=["timestamp", "cumulative_pnl_inr"])

        if not closed_today.empty:
            closed_today["pnl_inr"] = pd.to_numeric(
                closed_today["pnl_inr"], errors="coerce"
            ).fillna(0.0)
            realized_today = float(closed_today["pnl_inr"].sum())
            wins = int((closed_today["pnl_inr"] > 0).sum())
            win_rate = round((wins / len(closed_today)) * 100.0, 1)

            closed_today = closed_today.sort_values("timestamp")
            closed_today["cumulative_pnl_inr"] = closed_today["pnl_inr"].cumsum()
            intraday_series = closed_today[["timestamp", "cumulative_pnl_inr"]].reset_index(
                drop=True
            )

        open_positions = self.get_open_positions(mark_prices)
        unrealized_total = sum(float(p.get("unrealized_pnl_inr", 0.0)) for p in open_positions)
        today_net_pnl = round(realized_today + unrealized_total, 2)

        return {
            "today_net_pnl_inr": today_net_pnl,
            "realized_today_inr": round(realized_today, 2),
            "unrealized_inr": round(unrealized_total, 2),
            "win_rate_pct": win_rate,
            "closed_trades_today": len(closed_today),
            "open_positions_count": len(open_positions),
            "intraday_series": intraday_series,
        }

    def check_daily_limits(
        self,
        daily_profit_target_inr: float,
        max_daily_loss_inr: float,
        mark_prices: Optional[Dict[str, float]] = None,
    ) -> Tuple[bool, str]:
        """Return (is_paused, reason) when today's profit target or max loss is reached."""
        metrics = self.get_today_metrics(mark_prices)
        net_pnl = metrics["today_net_pnl_inr"]
        if daily_profit_target_inr > 0 and net_pnl >= daily_profit_target_inr:
            return (
                True,
                f"Daily Profit Target hit (₹{net_pnl:+,.2f} ≥ ₹{daily_profit_target_inr:,.0f}). New entries paused.",
            )
        if max_daily_loss_inr > 0 and net_pnl <= -abs(max_daily_loss_inr):
            return (
                True,
                f"Max Daily Loss hit (₹{net_pnl:+,.2f} ≤ -₹{max_daily_loss_inr:,.0f}). New entries paused.",
            )
        return False, ""

    def close_paper_position(
        self, symbol: str, exit_price: float, exit_reason: str = "MANUAL"
    ) -> Optional[Dict[str, Any]]:
        """Close a simulated PAPER position and update CSV log."""
        sym = symbol.upper()
        pos = self.paper_positions.pop(sym, None)
        if not pos:
            return None

        pnl = self.calculate_pnl_inr(
            pos["side"],
            pos["entry_price"],
            exit_price,
            pos["margin_inr"],
            pos["leverage"],
        )
        self._update_trade_row(
            pos["trade_id"],
            {
                "exit_price": round(exit_price, 4),
                "status": "CLOSED",
                "exit_reason": exit_reason,
                "pnl_inr": pnl,
            },
        )
        self._record_event(
            f"PAPER CLOSED {pos['side']} {sym} @ {exit_price:,.2f} ({exit_reason}) | PnL: ₹{pnl:+,.2f}"
        )
        return {**pos, "exit_price": exit_price, "exit_reason": exit_reason, "pnl_inr": pnl}

    def close_single_position(self, symbol: str, mark_price: Optional[float] = None) -> None:
        """Close one open position in the active mode."""
        sym = symbol.upper()
        if self.mode == "PAPER":
            px = mark_price or self.client.fetch_mark_price(sym)
            self.close_paper_position(sym, px, exit_reason="MANUAL_CLOSE")
            return

        self.client.close_position(sym)
        df = self.load_trades_df()
        open_rows = df[(df["mode"] == "LIVE") & (df["symbol"] == sym) & (df["status"] == "OPEN")]
        px = mark_price or self.client.fetch_mark_price(sym)
        for _, row in open_rows.iterrows():
            pnl = self.calculate_pnl_inr(
                str(row["side"]),
                float(row["entry_price"]),
                px,
                float(row["margin_inr"]),
                int(row["leverage"]),
            )
            self._update_trade_row(
                str(row["trade_id"]),
                {
                    "exit_price": round(px, 4),
                    "status": "CLOSED",
                    "exit_reason": "MANUAL_CLOSE",
                    "pnl_inr": pnl,
                },
            )
        self._record_event(f"LIVE CLOSED {sym} manually @ ~{px:,.2f}")

    def _open_trade(
        self,
        symbol: str,
        side: str,
        mark_price: float,
        margin_inr: float,
        leverage: int,
        sl_pct: float,
        tp_pct: float,
    ) -> None:
        """Open a new PAPER or LIVE trade with attached SL and TP."""
        sym = symbol.upper()
        sl_price, tp_price = self.compute_sl_tp(side, mark_price, sl_pct, tp_pct)
        trade_id = uuid.uuid4().hex[:10]
        now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

        try:
            contracts = self.client.calculate_contract_size(sym, margin_inr, leverage, mark_price)
        except Exception:
            contracts = 1

        if self.mode == "PAPER":
            available_paper = self.get_paper_balance_inr() - sum(
                p["margin_inr"] for p in self.paper_positions.values()
            )
            if available_paper < margin_inr:
                self._record_event(
                    f"PAPER skipped {side} {sym}: available virtual margin ₹{available_paper:,.0f} < ₹{margin_inr:,.0f}"
                )
                return

            pos = {
                "trade_id": trade_id,
                "timestamp": now_str,
                "symbol": sym,
                "side": side,
                "contracts": contracts,
                "leverage": leverage,
                "margin_inr": margin_inr,
                "entry_price": round(mark_price, 4),
                "sl_price": sl_price,
                "tp_price": tp_price,
                "mark_price": round(mark_price, 4),
                "unrealized_pnl_inr": 0.0,
            }
            self.paper_positions[sym] = pos
            self._append_trade_row(
                {
                    "trade_id": trade_id,
                    "timestamp": now_str,
                    "mode": "PAPER",
                    "symbol": sym,
                    "side": side,
                    "contracts": contracts,
                    "leverage": leverage,
                    "margin_inr": margin_inr,
                    "entry_price": round(mark_price, 4),
                    "exit_price": None,
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "status": "OPEN",
                    "exit_reason": "",
                    "pnl_inr": 0.0,
                    "order_id": f"PAPER-{trade_id}",
                }
            )
            self._record_event(
                f"PAPER OPENED {side} {sym} ({contracts} cts @ {mark_price:,.2f}) | SL {sl_price:,.2f} / TP {tp_price:,.2f}"
            )
            return

        # LIVE mode
        order_resp = self.client.place_bracket_order(
            symbol=sym,
            side=side,
            size=contracts,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            leverage=leverage,
        )
        order_id = str(order_resp.get("id", "")) if isinstance(order_resp, dict) else ""
        fill_price = float(order_resp.get("average_fill_price") or mark_price) if isinstance(
            order_resp, dict
        ) else mark_price

        self._append_trade_row(
            {
                "trade_id": trade_id,
                "timestamp": now_str,
                "mode": "LIVE",
                "symbol": sym,
                "side": side,
                "contracts": contracts,
                "leverage": leverage,
                "margin_inr": margin_inr,
                "entry_price": round(fill_price, 4),
                "exit_price": None,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "status": "OPEN",
                "exit_reason": "",
                "pnl_inr": 0.0,
                "order_id": order_id,
            }
        )
        self._record_event(
            f"LIVE OPENED {side} {sym} ({contracts} cts @ {fill_price:,.2f}, Order #{order_id})"
        )

    def on_tick(
        self,
        signals: Dict[str, Dict[str, Any]],
        mark_prices: Dict[str, float],
        margin_inr: float,
        leverage: int,
        sl_pct: float,
        tp_pct: float,
        daily_profit_target_inr: float,
        max_daily_loss_inr: float,
    ) -> List[str]:
        """Run one scan/execution cycle: monitor SL/TP, enforce limits, and enter new signals."""
        events: List[str] = []

        # 1. Monitor open PAPER SL/TP hits on every tick
        if self.mode == "PAPER":
            for sym in list(self.paper_positions.keys()):
                pos = self.paper_positions[sym]
                mark = mark_prices.get(sym)
                if mark is None:
                    continue
                if pos["side"] == "BUY":
                    if mark <= pos["sl_price"]:
                        closed = self.close_paper_position(sym, mark, exit_reason="STOP_LOSS")
                        if closed:
                            events.append(f"SL hit on {sym}")
                    elif mark >= pos["tp_price"]:
                        closed = self.close_paper_position(sym, mark, exit_reason="TAKE_PROFIT")
                        if closed:
                            events.append(f"TP hit on {sym}")
                else:
                    if mark >= pos["sl_price"]:
                        closed = self.close_paper_position(sym, mark, exit_reason="STOP_LOSS")
                        if closed:
                            events.append(f"SL hit on {sym}")
                    elif mark <= pos["tp_price"]:
                        closed = self.close_paper_position(sym, mark, exit_reason="TAKE_PROFIT")
                        if closed:
                            events.append(f"TP hit on {sym}")
        else:
            try:
                live_pos = self.get_open_positions(mark_prices)
                self._reconcile_live_closed_trades(live_pos, mark_prices)
            except DeltaAPIError as exc:
                events.append(self._record_event(f"Live position sync warning: {exc}"))
                return events

        # 2. Check if bot is halted or daily limit reached
        if self.bot_halted:
            return events

        paused, pause_reason = self.check_daily_limits(
            daily_profit_target_inr, max_daily_loss_inr, mark_prices
        )
        if paused:
            if pause_reason != self.halt_reason:
                self.halt_reason = pause_reason
                events.append(self._record_event(pause_reason))
            return events

        # 3. Prevent duplicate open positions and act on fresh BUY/SELL signals
        active_symbols = {p["symbol"].upper() for p in self.get_open_positions(mark_prices)}

        for sym, sig_info in signals.items():
            signal = sig_info.get("signal", "HOLD")
            if signal not in ("BUY", "SELL"):
                continue
            if sym.upper() in active_symbols:
                continue
            mark = mark_prices.get(sym)
            if not mark:
                continue

            try:
                self._open_trade(
                    symbol=sym,
                    side=signal,
                    mark_price=mark,
                    margin_inr=margin_inr,
                    leverage=leverage,
                    sl_pct=sl_pct,
                    tp_pct=tp_pct,
                )
                active_symbols.add(sym.upper())
                events.append(f"Opened {signal} {sym}")
            except DeltaAPIError as exc:
                events.append(self._record_event(f"Order error on {sym}: {exc}"))

        return events

    def emergency_kill_switch(self, mark_prices: Optional[Dict[str, float]] = None) -> List[str]:
        """Immediately close all open positions and halt the bot."""
        mark_prices = mark_prices or {}
        actions: List[str] = []
        self.bot_halted = True
        self.halt_reason = "Emergency Kill-Switch activated by user."

        if self.mode == "PAPER":
            for sym in list(self.paper_positions.keys()):
                pos = self.paper_positions[sym]
                exit_px = mark_prices.get(sym)
                if exit_px is None:
                    try:
                        exit_px = self.client.fetch_mark_price(sym)
                    except Exception:
                        exit_px = pos["entry_price"]
                self.close_paper_position(sym, exit_px, exit_reason="KILL_SWITCH")
                actions.append(f"Closed PAPER {sym} @ {exit_px:,.2f}")
        else:
            try:
                self.client.close_all_positions()
                df = self.load_trades_df()
                open_live = df[(df["mode"] == "LIVE") & (df["status"] == "OPEN")]
                for _, row in open_live.iterrows():
                    sym = str(row["symbol"]).upper()
                    exit_px = mark_prices.get(sym, float(row["entry_price"]))
                    pnl = self.calculate_pnl_inr(
                        str(row["side"]),
                        float(row["entry_price"]),
                        exit_px,
                        float(row["margin_inr"]),
                        int(row["leverage"]),
                    )
                    self._update_trade_row(
                        str(row["trade_id"]),
                        {
                            "exit_price": round(exit_px, 4),
                            "status": "CLOSED",
                            "exit_reason": "KILL_SWITCH",
                            "pnl_inr": pnl,
                        },
                    )
                actions.append("Closed all LIVE positions on Delta India")
            except DeltaAPIError as exc:
                actions.append(f"Kill-switch API warning: {exc}")

        self._record_event("EMERGENCY KILL-SWITCH TRIGGERED — All positions closed, bot halted.")
        return actions

    def resume_bot(self) -> None:
        """Clear kill-switch halt flag so auto-trading can resume."""
        self.bot_halted = False
        self.halt_reason = ""
        self._record_event("Bot resumed by user.")
