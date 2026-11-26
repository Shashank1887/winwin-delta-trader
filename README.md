# Delta Exchange India — Crypto Futures Auto-Trader & Screener

A modular Python + Streamlit auto-trading dashboard and 15m screener configured specifically for **Delta Exchange India** (`https://api.india.delta.exchange`).

---

## Features

- **Delta Exchange India Native (`https://api.india.delta.exchange`)**: Overrides `ccxt.delta()` endpoints for India futures and dynamically loads all **260+ operational perpetual futures** (crypto, gold/silver, and tokenized stocks) with Top 20 / Top 50 / All / Custom market scanning, plus HMAC-SHA256 authenticated order signing.
- **15m Trend + Momentum Strategy (`strategy.py`)**:
  - **BUY (Long):** `Close > 200 EMA`, `9 EMA` crosses above `21 EMA`, and `RSI(14) > 55`.
  - **SELL (Short):** `Close < 200 EMA`, `9 EMA` crosses below `21 EMA`, and `RSI(14) < 45`.
- **Seamless PAPER & LIVE Modes (`trade_manager.py`)**:
  - **PAPER Mode:** Simulates trades against live Delta India mark prices with a virtual **₹30,000** balance, monitors SL/TP on every 15s tick, and logs everything to `delta_trades_log.csv`.
  - **LIVE Mode:** Requires explicit UI confirmation, prevents duplicate open positions on the same symbol, places market entries with attached bracket SL/TP orders, and logs fills to `delta_trades_log.csv`.
- **Risk Guardrails & Emergency Kill-Switch (`app.py`)**:
  - Default **₹5,000** margin per trade at **3x** leverage (`1x–5x` slider), **1.0%** SL, **2.0%** TP.
  - Auto-pauses new entries when **Daily Profit Target (₹800)** or **Max Daily Loss (₹500)** is hit.
  - Red **EMERGENCY KILL-SWITCH** button immediately closes all open positions and halts the bot.

---

## Project Structure

```text
WinWin_Crypto/
├── .env.example          # Template for Delta India API credentials
├── .streamlit/
│   └── config.toml       # Clean dashboard theme (high-visibility red primary button)
├── config.py             # Defaults, endpoints, leverage, SL/TP, and daily INR limits
├── delta_client.py       # CCXT India override + HMAC-SHA256 REST client + public IP helper
├── strategy.py           # 15m EMA(9/21/200) & RSI(14) signal engine
├── trade_manager.py      # PAPER / LIVE trade execution, SL/TP monitor, CSV logger
├── app.py                # Streamlit dashboard with 15s auto-refresh engine
├── requirements.txt      # Python dependencies
└── tests/
    └── test_trading_app.py
```

---

## Step-by-Step Setup

### 1. Create a Virtual Environment & Install Dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **Python 3.14 note:** `pandas_ta` depends on `numba`, which currently supports Python `< 3.14`. On Python 3.14+, `strategy.py` automatically uses its built-in exact pandas/numpy EMA and Wilder RSI(14) calculations with identical math.

### 2. Configure Your Delta India API Keys

1. Log in to **Delta Exchange India**: `https://www.india.delta.exchange`
   *(Important: Global `delta.exchange` keys will NOT work on `api.india.delta.exchange`.)*
2. Go to **Account -> API Management -> Create API Key**.
3. Copy `.env.example` to `.env`:
   ```powershell
   Copy-Item .env.example .env
   ```
4. Paste your `DELTA_API_KEY` and `DELTA_API_SECRET` into `.env`.

### 3. Delta India IP Whitelisting Notes (Important for LIVE Mode)

Delta Exchange India requires API keys with trading permissions to be bound to a **whitelisted IPv4 address**:

1. Launch the dashboard first (even in **PAPER** mode — no API keys needed for market data).
2. Check the **Current public IP** KPI card at the top of the dashboard.
3. In Delta India **API Management**, add that exact IPv4 address to the API key's **IP Whitelist**.
4. Enable **Read** and **Trading** permissions on the key.
5. If your home/office ISP changes your public IP later, check the dashboard KPI card again and update your whitelist on Delta India.

### 4. Run the Application

```powershell
streamlit run app.py
```

Open the URL printed in your terminal (usually `http://localhost:8501`).

### 5. Run Tests

```powershell
pytest
```

---

## Free Hosting & Mobile Access

### Option 1: Streamlit Community Cloud (Free, Easiest for Mobile Browser)
1. Create a **Private GitHub repository** and upload this project folder (`.gitignore` already excludes `.env` and `delta_trades_log.csv`).
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. Click **Create app** -> select your repo, branch (`main`), and main file `app.py`.
4. Under **Advanced settings -> Secrets**, paste:
   ```toml
   DELTA_API_KEY = "your_delta_india_api_key"
   DELTA_API_SECRET = "your_delta_india_api_secret"
   USD_TO_INR_RATE = "85.0"
   ```
5. Click **Deploy** and open the generated `https://*.streamlit.app` link on your phone (you can **Add to Home Screen** on Android/iOS).
6. **Important security & IP notes for free cloud hosting:**
   - Set the app to **Private** in Streamlit Cloud sharing settings so only your account can open it.
   - Free cloud servers have **dynamic outbound IPs** that can change after sleep/restarts. **PAPER mode** works anytime, but for **LIVE mode** you would need to check the dashboard's **Current public IP** card and update Delta India's IP whitelist whenever the cloud IP changes.

### Option 2: Run on Your PC + Private Mobile Access via Tailscale (Free + Static IP)
If you want **24/7 LIVE trading with a consistent IP** and persistent `delta_trades_log.csv`:
1. Install [Tailscale](https://tailscale.com) (free personal plan) on your Windows PC and your mobile phone.
2. Run `streamlit run app.py --server.address 0.0.0.0 --server.port 8501` on your PC.
3. On your mobile phone (with Tailscale connected), open `http://<your-pc-tailscale-ip>:8501` from anywhere securely.

---

## Disclaimer

Automated futures trading carries substantial financial risk. Always validate behavior in **PAPER** mode first before checking the **Live Trading** confirmation box.
