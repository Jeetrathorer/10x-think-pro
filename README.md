# 10X THINK PRO

Single-file Telegram market-analysis bot for **paper trading only**.
It does not connect to a broker and cannot place real-money orders.

## What is included

- NIFTY, BANKNIFTY, INFY, TCS, HDFCBANK and RELIANCE watchlist
- Yahoo Finance read-only data adapter
- 1H + 15M + 5M confirmation
- EMA20/EMA50, VWAP, RSI, MACD, ATR, volume ratio and structure checks
- Evidence score out of 100; score is not a probability of profit
- Hard no-trade veto for stale/missing data, weak score, poor risk/reward,
  abnormal volatility, zero position size and risk locks
- 0.5% default paper risk per scenario, 2% daily loss lock, consecutive-loss lock
- SQLite journal, R-based stats, manual `/close ID R` result recording
- Strong-setup-only scheduled Telegram alerts
- A simple non-lookahead educational backtest

Yahoo Finance is convenient and does not require a market-data key, but its
intraday history, freshness, rate limits and exchange coverage are not
guaranteed. For live execution or exchange-grade data, use an authorized
broker/data provider later; this file intentionally does not implement order
execution.

## Termux install

```bash
pkg update -y
pkg install python git -y
mkdir -p ~/10x-think-pro
cd ~/10x-think-pro
# Copy 10x_think_pro.py, requirements.txt and .env.example here
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python 10x_think_pro.py --self-test
python 10x_think_pro.py
```

Put the BotFather token only in `.env` as `BOT_TOKEN=...`. Do not send it in
chat or commit `.env`. Set `ALLOWED_CHAT_IDS` to your own Telegram chat ID(s)
for a private bot.

## Keep it running on Termux

Install Termux:API only if you want notifications from the phone itself.
For a simple persistent process:

```bash
pkg install tmux -y
tmux new -s 10x
cd ~/10x-think-pro
source .venv/bin/activate
python 10x_think_pro.py
```

Detach with `Ctrl+b`, then `d`. Reattach with:

```bash
tmux attach -t 10x
```

Prevent Android from killing Termux:

```bash
termux-wake-lock
```

For reboot persistence, install the Termux:Boot add-on, create
`~/.termux/boot/start-10x-think.sh`, and make it executable:

```bash
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$HOME/10x-think-pro"
source .venv/bin/activate
python 10x_think_pro.py >> bot.log 2>&1
```

```bash
chmod +x ~/.termux/boot/start-10x-think.sh
```

## Commands

`/start`, `/help`, `/status`, `/analysis SYMBOL`, `/nifty`, `/banknifty`,
`/watchlist`, `/setups`, `/journal`, `/close ID R`, `/stats`, `/risk`,
`/today`, `/backtest SYMBOL`, `/settings`

No trade is a valid result. The bot must not be treated as a guarantee,
signal-selling system or financial advice.