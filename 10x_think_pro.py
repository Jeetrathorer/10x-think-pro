"""
10X THINK PRO
---------------
Conservative Telegram market-analysis and paper-trading bot.

This file intentionally does NOT place broker orders. It uses public Yahoo
Finance data through yfinance, creates paper scenarios only, and fails closed
when data is missing, stale, conflicting, or risk limits are reached.

Quick start:
    cp .env.example .env
    python 10x_think_pro.py --self-test
    python 10x_think_pro.py
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
LOG = logging.getLogger("10x-think-pro")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def csv_values(name: str, default: str) -> list[str]:
    return [
        item.strip().upper()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    ]


SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "INFY": "INFY.NS",
    "TCS": "TCS.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "RELIANCE": "RELIANCE.NS",
}


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str = "10x_think_pro.sqlite3"
    watchlist: tuple[str, ...] = (
        "NIFTY",
        "BANKNIFTY",
        "INFY",
        "TCS",
        "HDFCBANK",
        "RELIANCE",
    )
    paper_capital: float = 10000.0
    risk_per_trade: float = 0.005
    max_daily_loss: float = 0.02
    max_consecutive_losses: int = 2
    max_paper_trades: int = 6
    min_score: int = 80
    scan_interval_seconds: int = 900
    max_candle_age_minutes: int = 35
    market_open: time = time(9, 20)
    market_close: time = time(15, 15)
    timezone_name: str = "Asia/Kolkata"
    notification_mode: str = "strong_only"
    allowed_chat_ids: tuple[int, ...] = ()
    allow_outside_hours_analysis: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        watchlist = tuple(csv_values("WATCHLIST", ",".join(cls.watchlist)))
        chat_ids: list[int] = []
        for raw in csv_values("ALLOWED_CHAT_IDS", ""):
            try:
                chat_ids.append(int(raw))
            except ValueError:
                LOG.warning("Ignoring invalid ALLOWED_CHAT_IDS value")
        return cls(
            bot_token=token,
            db_path=os.getenv("DB_PATH", "10x_think_pro.sqlite3"),
            watchlist=watchlist or cls.watchlist,
            paper_capital=env_float("PAPER_CAPITAL", 10000.0),
            risk_per_trade=min(max(env_float("RISK_PER_TRADE", 0.005), 0.001), 0.01),
            max_daily_loss=min(max(env_float("MAX_DAILY_LOSS", 0.02), 0.005), 0.05),
            max_consecutive_losses=max(env_int("MAX_CONSECUTIVE_LOSSES", 2), 1),
            max_paper_trades=max(env_int("MAX_PAPER_TRADES", 6), 1),
            min_score=min(max(env_int("MIN_SCORE", 80), 70), 95),
            scan_interval_seconds=max(env_int("SCAN_INTERVAL_SECONDS", 900), 300),
            max_candle_age_minutes=max(env_int("MAX_CANDLE_AGE_MINUTES", 35), 10),
            notification_mode=os.getenv("NOTIFICATION_MODE", "strong_only"),
            allowed_chat_ids=tuple(chat_ids),
            allow_outside_hours_analysis=env_bool("ALLOW_OUTSIDE_HOURS_ANALYSIS", True),
        )


@dataclass
class Setup:
    symbol: str
    decision: str
    direction: str
    strategy: str
    score: int
    entry_low: float
    entry_high: float
    invalidation: float
    target_1: float
    target_2: float
    quantity: int
    risk_rupees: float
    regime: str
    reasons: list[str]
    warnings: list[str]
    data_status: str = "OK"
    fingerprint: str = ""

    @property
    def entry(self) -> float:
        return (self.entry_low + self.entry_high) / 2

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.invalidation)

    @property
    def reward_risk(self) -> float:
        if self.risk_per_unit <= 0:
            return 0.0
        return abs(self.target_2 - self.entry) / self.risk_per_unit


def now_ist() -> datetime:
    return datetime.now(IST)


def fmt_price(value: float) -> str:
    if not math.isfinite(value):
        return "n/a"
    return f"{value:,.2f}"


def fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def as_float(value: Any) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def round_down_quantity(risk_rupees: float, entry: float, stop: float) -> int:
    distance = abs(entry - stop)
    if distance <= 0 or not all(math.isfinite(x) for x in (risk_rupees, entry, stop)):
        return 0
    return max(0, int(math.floor(risk_rupees / distance)))


def is_market_open(config: Config, when: Optional[datetime] = None) -> bool:
    current = (when or now_ist()).astimezone(IST)
    return current.weekday() < 5 and config.market_open <= current.time() <= config.market_close


def clean_yfinance_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    if isinstance(result.columns, pd.MultiIndex):
        result.columns = [str(column[0]).lower() for column in result.columns]
    else:
        result.columns = [str(column).lower().replace(" ", "_") for column in result.columns]
    required = ["open", "high", "low", "close", "volume"]
    if not all(column in result.columns for column in required):
        return pd.DataFrame()
    result = result[required].copy()
    result = result.apply(pd.to_numeric, errors="coerce").dropna(subset=["open", "high", "low", "close"])
    result = result[~result.index.duplicated(keep="last")].sort_index()
    if result.index.tz is None:
        result.index = result.index.tz_localize(UTC)
    result.index = result.index.tz_convert(IST)
    return result


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    close = data["close"]
    high = data["high"]
    low = data["low"]
    volume = data["volume"].replace(0, np.nan)
    data["ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    data["ema50"] = close.ewm(span=50, adjust=False, min_periods=50).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    data["rsi"] = 100 - (100 / (1 + rs))
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    data["macd"] = ema12 - ema26
    data["macd_signal"] = data["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    tr = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    data["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    typical = (high + low + close) / 3
    day_key = pd.Series(data.index.date, index=data.index)
    data["vwap"] = (typical * data["volume"]).groupby(day_key).cumsum() / data["volume"].groupby(day_key).cumsum()
    data["volume_avg20"] = volume.rolling(20, min_periods=10).mean()
    data["volume_ratio"] = data["volume"] / data["volume_avg20"]
    data["prior_high20"] = high.shift(1).rolling(20, min_periods=10).max()
    data["prior_low20"] = low.shift(1).rolling(20, min_periods=10).min()
    data["range"] = high - low
    data["body"] = (close - data["open"]).abs()
    return data


def latest(frame: pd.DataFrame) -> Optional[pd.Series]:
    if frame.empty:
        return None
    row = frame.iloc[-1]
    if row.isna().any():
        return None
    return row


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_setups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    regime TEXT NOT NULL,
                    entry_low REAL NOT NULL,
                    entry_high REAL NOT NULL,
                    invalidation REAL NOT NULL,
                    target_1 REAL NOT NULL,
                    target_2 REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    risk_rupees REAL NOT NULL,
                    reasons TEXT NOT NULL,
                    warnings TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    result_r REAL,
                    fingerprint TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    data_status TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.commit()

    def insert_setup(self, setup: Setup) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO paper_setups
                (created_at, symbol, decision, direction, strategy, score, regime,
                 entry_low, entry_high, invalidation, target_1, target_2, quantity,
                 risk_rupees, reasons, warnings, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ist().isoformat(),
                    setup.symbol,
                    setup.decision,
                    setup.direction,
                    setup.strategy,
                    setup.score,
                    setup.regime,
                    setup.entry_low,
                    setup.entry_high,
                    setup.invalidation,
                    setup.target_1,
                    setup.target_2,
                    setup.quantity,
                    setup.risk_rupees,
                    json.dumps(setup.reasons),
                    json.dumps(setup.warnings),
                    setup.fingerprint,
                ),
            )
            db.commit()
            return int(cursor.lastrowid)

    def log_scan(self, symbol: str, decision: str, score: int, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO scan_log (created_at, symbol, decision, score, data_status) VALUES (?, ?, ?, ?, ?)",
                (now_ist().isoformat(), symbol, decision, score, status),
            )
            db.commit()

    def recent_setups(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM paper_setups ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            )

    def latest_alert_fingerprint(self, fingerprint: str, hours: int = 8) -> bool:
        cutoff = (now_ist() - timedelta(hours=hours)).isoformat()
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM paper_setups WHERE fingerprint = ? AND created_at >= ? LIMIT 1",
                (fingerprint, cutoff),
            ).fetchone()
            return row is not None

    def close_setup(self, setup_id: int, r_multiple: float) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE paper_setups SET status = 'CLOSED', result_r = ? WHERE id = ? AND status = 'OPEN'",
                (r_multiple, setup_id),
            )
            db.commit()
            return cursor.rowcount > 0

    def risk_snapshot(self, config: Config) -> dict[str, Any]:
        today = now_ist().date().isoformat()
        with self.connect() as db:
            rows = list(
                db.execute(
                    "SELECT * FROM paper_setups WHERE substr(created_at, 1, 10) = ? ORDER BY id ASC",
                    (today,),
                ).fetchall()
            )
            closed = [
                row for row in rows
                if row["status"] == "CLOSED" and row["result_r"] is not None
            ]
            pnl = sum(float(row["risk_rupees"]) * float(row["result_r"]) for row in closed)
            losses = [row for row in closed if float(row["result_r"]) < 0]
            consecutive = 0
            for row in reversed(closed):
                if float(row["result_r"]) < 0:
                    consecutive += 1
                else:
                    break
            loss_limit = config.paper_capital * config.max_daily_loss
            locked = (
                pnl <= -loss_limit
                or consecutive >= config.max_consecutive_losses
                or len(rows) >= config.max_paper_trades
            )
            return {
                "trades": len(rows),
                "closed": len(closed),
                "pnl": pnl,
                "losses": len(losses),
                "consecutive_losses": consecutive,
                "loss_limit": loss_limit,
                "locked": locked,
                "lock_reason": (
                    "daily loss limit"
                    if pnl <= -loss_limit
                    else "consecutive loss limit"
                    if consecutive >= config.max_consecutive_losses
                    else "maximum paper trades"
                    if len(rows) >= config.max_paper_trades
                    else ""
                ),
            }

    def stats(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = list(
                db.execute(
                    "SELECT * FROM paper_setups WHERE status = 'CLOSED' AND result_r IS NOT NULL"
                ).fetchall()
            )
        if not rows:
            return {"trades": 0}
        results = [float(row["result_r"]) for row in rows]
        wins = [r for r in results if r > 0]
        losses = [r for r in results if r < 0]
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        streak = 0
        max_streak = 0
        for result in results:
            equity += result
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
            streak = streak + 1 if result < 0 else 0
            max_streak = max(max_streak, streak)
        return {
            "trades": len(results),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(results),
            "avg_r": sum(results) / len(results),
            "expectancy_r": sum(results) / len(results),
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else float("inf"),
            "max_drawdown_r": max_dd,
            "max_losing_streak": max_streak,
        }

    def subscribe(self, chat_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO subscribers (chat_id, created_at) VALUES (?, ?)",
                (chat_id, now_ist().isoformat()),
            )
            db.commit()

    def subscribers(self) -> list[int]:
        with self.connect() as db:
            rows = db.execute("SELECT chat_id FROM subscribers").fetchall()
        return [int(row["chat_id"]) for row in rows]


class YahooMarketData:
    """Read-only Yahoo chart API adapter. No fabricated fallback values."""

    PERIOD_SECONDS = {"5m": 30 * 86400, "15m": 60 * 86400, "1h": 730 * 86400}
    YAHOO_INTERVALS = {"5m": "5m", "15m": "15m", "1h": "60m"}

    @classmethod
    def fetch_sync(cls, symbol: str, interval: str) -> pd.DataFrame:
        ticker = SYMBOLS.get(symbol, symbol if "." in symbol else f"{symbol}.NS")
        end = int(datetime.now(UTC).timestamp())
        start = end - cls.PERIOD_SECONDS[interval]
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {
            "period1": start,
            "period2": end,
            "interval": cls.YAHOO_INTERVALS[interval],
            "includePrePost": "false",
            "events": "div,splits",
        }
        with httpx.Client(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 10X-THINK-PRO/1.0"},
        ) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result or not result[0].get("timestamp"):
            return pd.DataFrame()
        quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
        frame = pd.DataFrame(
            {
                "open": quote.get("open", []),
                "high": quote.get("high", []),
                "low": quote.get("low", []),
                "close": quote.get("close", []),
                "volume": quote.get("volume", []),
            },
            index=pd.to_datetime(result[0]["timestamp"], unit="s", utc=True),
        )
        return clean_yfinance_frame(frame)

    async def fetch(self, symbol: str, interval: str) -> pd.DataFrame:
        try:
            return await asyncio.to_thread(self.fetch_sync, symbol, interval)
        except Exception:
            LOG.exception("Market-data fetch failed for %s %s", symbol, interval)
            return pd.DataFrame()

    @staticmethod
    def stale(frame: pd.DataFrame, max_age_minutes: int) -> bool:
        if frame.empty:
            return True
        last = frame.index[-1].to_pydatetime().astimezone(IST)
        return (now_ist() - last).total_seconds() > max_age_minutes * 60


def direction_for(hour: pd.Series, fifteen: pd.Series, five: pd.Series) -> str:
    bullish = (
        hour["close"] > hour["ema20"] > hour["ema50"]
        and fifteen["close"] > fifteen["ema20"] > fifteen["ema50"]
        and five["close"] > five["vwap"]
    )
    bearish = (
        hour["close"] < hour["ema20"] < hour["ema50"]
        and fifteen["close"] < fifteen["ema20"] < fifteen["ema50"]
        and five["close"] < five["vwap"]
    )
    if bullish:
        return "LONG"
    if bearish:
        return "SHORT"
    return "NONE"


def regime_for(hour: pd.Series, fifteen: pd.Series, five: pd.Series) -> str:
    atr_pct = as_float(five["atr"]) / as_float(five["close"])
    if atr_pct > 0.025:
        return "HIGH_VOLATILITY"
    if (
        hour["close"] > hour["ema20"] > hour["ema50"]
        and fifteen["close"] > fifteen["ema20"] > fifteen["ema50"]
    ):
        return "TRENDING_UP"
    if (
        hour["close"] < hour["ema20"] < hour["ema50"]
        and fifteen["close"] < fifteen["ema20"] < fifteen["ema50"]
    ):
        return "TRENDING_DOWN"
    return "SIDEWAYS"


def evidence_score(
    direction: str, hour: pd.Series, fifteen: pd.Series, five: pd.Series, regime: str
) -> tuple[int, list[str], list[str]]:
    if direction == "NONE":
        return 0, [], ["Higher timeframes conflict or are not aligned"]
    long_side = direction == "LONG"
    points = 0
    reasons: list[str] = []
    warnings: list[str] = []

    if (regime == "TRENDING_UP" and long_side) or (regime == "TRENDING_DOWN" and not long_side):
        points += 15
        reasons.append("1H and 15M trend context aligned")
    else:
        warnings.append("Trend regime is not fully supportive")

    structure_ok = (
        five["close"] > five["prior_high20"] * 0.998
        if long_side
        else five["close"] < five["prior_low20"] * 1.002
    )
    if structure_ok:
        points += 15
        reasons.append("Price is holding near the active structure edge")

    distance_to_level = (
        abs(five["close"] - five["prior_low20"]) / five["close"]
        if long_side
        else abs(five["close"] - five["prior_high20"]) / five["close"]
    )
    if math.isfinite(distance_to_level) and distance_to_level < 0.012:
        points += 10
        reasons.append("Support/resistance context is usable")
    else:
        warnings.append("Nearest structure level is not clean")

    vwap_ok = five["close"] > five["vwap"] if long_side else five["close"] < five["vwap"]
    if vwap_ok:
        points += 10
        reasons.append("VWAP agrees with direction")
    else:
        warnings.append("VWAP conflict")

    ema_ok = five["close"] > five["ema20"] if long_side else five["close"] < five["ema20"]
    if ema_ok:
        points += 10
        reasons.append("EMA20 agrees with direction")
    else:
        warnings.append("EMA20 conflict")

    volume_ratio = as_float(five["volume_ratio"])
    if math.isfinite(volume_ratio) and volume_ratio >= 1.10:
        points += 10
        reasons.append(f"Volume confirmation ({volume_ratio:.2f}x average)")
    else:
        warnings.append("Volume is not convincingly above average")

    candle_ok = (
        five["close"] >= five["open"] if long_side else five["close"] <= five["open"]
    )
    if candle_ok:
        points += 10
        reasons.append("Latest price action supports the direction")

    breakout_ok = (
        five["close"] > five["prior_high20"] if long_side else five["close"] < five["prior_low20"]
    )
    if breakout_ok:
        points += 10
        reasons.append("Breakout/breakdown confirmation present")
    else:
        warnings.append("Breakout/retest confirmation is incomplete")

    # Risk/reward is awarded later after a stop and targets are calculated.
    atr_pct = as_float(five["atr"]) / as_float(five["close"])
    if atr_pct > 0.018:
        warnings.append("Volatility is elevated; risk veto may block the setup")
    return points, reasons, warnings


def build_setup(
    symbol: str,
    direction: str,
    regime: str,
    hour: pd.Series,
    fifteen: pd.Series,
    five: pd.Series,
    score: int,
    reasons: list[str],
    warnings: list[str],
    config: Config,
    data_status: str = "OK",
) -> Setup:
    close = as_float(five["close"])
    atr = as_float(five["atr"])
    if direction == "LONG":
        entry_low, entry_high = close - atr * 0.20, close + atr * 0.10
        stop = min(as_float(five["prior_low20"]), close - atr * 1.20)
        if not math.isfinite(stop) or stop >= entry_low:
            stop = close - atr * 1.20
        target_1 = close + abs(close - stop) * 1.25
        target_2 = close + abs(close - stop) * 2.0
    else:
        entry_low, entry_high = close - atr * 0.10, close + atr * 0.20
        stop = max(as_float(five["prior_high20"]), close + atr * 1.20)
        if not math.isfinite(stop) or stop <= entry_high:
            stop = close + atr * 1.20
        target_1 = close - abs(close - stop) * 1.25
        target_2 = close - abs(close - stop) * 2.0

    risk_rupees = config.paper_capital * config.risk_per_trade
    quantity = round_down_quantity(risk_rupees, close, stop)
    rr = abs(target_2 - close) / abs(close - stop) if abs(close - stop) else 0
    if rr >= 1.5:
        score = min(100, score + 10)
        reasons.append(f"Risk/reward is {rr:.2f}R")
    else:
        warnings.append(f"Risk/reward is only {rr:.2f}R")

    fingerprint_text = "|".join(
        [symbol, direction, str(score), f"{close:.2f}", f"{stop:.2f}", regime]
    )
    fingerprint = hashlib.sha256(fingerprint_text.encode()).hexdigest()[:16]
    decision = "PAPER LONG" if direction == "LONG" else "PAPER SHORT"
    return Setup(
        symbol=symbol,
        decision=decision,
        direction=direction,
        strategy="Multi-timeframe trend + VWAP/structure confirmation",
        score=int(score),
        entry_low=float(entry_low),
        entry_high=float(entry_high),
        invalidation=float(stop),
        target_1=float(target_1),
        target_2=float(target_2),
        quantity=quantity,
        risk_rupees=float(risk_rupees),
        regime=regime,
        reasons=reasons,
        warnings=warnings,
        data_status=data_status,
        fingerprint=fingerprint,
    )


class AnalysisEngine:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.market = YahooMarketData()

    async def analyze(self, symbol: str) -> Setup:
        symbol = symbol.upper().strip()
        if not symbol:
            return self.no_trade(symbol or "UNKNOWN", "Symbol is empty")
        if not is_market_open(self.config) and not self.config.allow_outside_hours_analysis:
            return self.no_trade(symbol, "Market is closed")
        frames = await asyncio.gather(
            self.market.fetch(symbol, "1h"),
            self.market.fetch(symbol, "15m"),
            self.market.fetch(symbol, "5m"),
        )
        if any(frame.empty for frame in frames):
            return self.no_trade(symbol, "DATA UNAVAILABLE: one or more timeframes returned no candles", "DATA UNAVAILABLE")
        if is_market_open(self.config) and any(
            self.market.stale(frame, self.config.max_candle_age_minutes) for frame in frames
        ):
            return self.no_trade(symbol, "DATA UNAVAILABLE: latest candle is stale", "DATA UNAVAILABLE")
        enriched = [add_indicators(frame) for frame in frames]
        rows = [latest(frame) for frame in enriched]
        if any(row is None for row in rows):
            return self.no_trade(symbol, "DATA UNAVAILABLE: insufficient clean indicator history", "DATA UNAVAILABLE")
        hour, fifteen, five = rows  # type: ignore[misc]
        direction = direction_for(hour, fifteen, five)
        regime = regime_for(hour, fifteen, five)
        score, reasons, warnings = evidence_score(direction, hour, fifteen, five, regime)
        setup = build_setup(
            symbol, direction, regime, hour, fifteen, five, score, reasons, warnings, self.config
        ) if direction != "NONE" else self.no_trade(
            symbol, "Higher timeframes conflict; waiting is safer"
        )
        if direction == "NONE":
            return setup
        risk = self.db.risk_snapshot(self.config)
        veto_reasons: list[str] = []
        if risk["locked"]:
            veto_reasons.append(f"Trading locked: {risk['lock_reason']}")
        if setup.quantity <= 0:
            veto_reasons.append("Position size is zero at the configured risk limit")
        if setup.reward_risk < 1.5:
            veto_reasons.append("Risk/reward is below 1.5R")
        atr_pct = as_float(five["atr"]) / as_float(five["close"])
        if not math.isfinite(atr_pct) or atr_pct <= 0 or atr_pct > 0.04:
            veto_reasons.append("Abnormal or unavailable volatility")
        if setup.score < self.config.min_score:
            veto_reasons.append(f"Evidence score {setup.score} is below {self.config.min_score}")
        if veto_reasons:
            setup.decision = "NO TRADE"
            setup.warnings.extend(veto_reasons)
            setup.data_status = "RISK VETO"
        return setup

    @staticmethod
    def no_trade(symbol: str, reason: str, status: str = "OK") -> Setup:
        return Setup(
            symbol=symbol,
            decision="NO TRADE",
            direction="NONE",
            strategy="None",
            score=0,
            entry_low=0,
            entry_high=0,
            invalidation=0,
            target_1=0,
            target_2=0,
            quantity=0,
            risk_rupees=0,
            regime="UNKNOWN",
            reasons=[],
            warnings=[reason],
            data_status=status,
        )


def setup_message(setup: Setup, config: Config, setup_id: Optional[int] = None) -> str:
    header = f"10X THINK PRO | {setup.symbol}\n"
    if setup.decision == "NO TRADE":
        warning_text = "\n".join(f"- {item}" for item in setup.warnings[:8])
        return (
            header
            + "NO TRADE / WAIT\n"
            + f"Evidence score: {setup.score}/100\n"
            + f"Data: {setup.data_status}\n"
            + f"Regime: {setup.regime}\n"
            + f"Reasons:\n{warning_text or '- No valid setup'}\n\n"
            + "Paper trading only. Score is not a profit probability."
        )
    reasons = "\n".join(f"- {item}" for item in setup.reasons[:8]) or "- Confirmation incomplete"
    warnings = "\n".join(f"- {item}" for item in setup.warnings[:8]) or "- None"
    id_line = f"\nJournal ID: {setup_id}" if setup_id else ""
    return (
        header
        + f"{setup.decision}\n"
        + f"Evidence score: {setup.score}/100\n"
        + f"Regime: {setup.regime}\n"
        + f"Strategy: {setup.strategy}\n\n"
        + f"Entry zone: {fmt_price(setup.entry_low)} - {fmt_price(setup.entry_high)}\n"
        + f"Invalidation/SL: {fmt_price(setup.invalidation)}\n"
        + f"Target 1: {fmt_price(setup.target_1)}\n"
        + f"Target 2: {fmt_price(setup.target_2)}\n"
        + f"Risk/reward: {setup.reward_risk:.2f}R\n"
        + f"Paper quantity: {setup.quantity}\n"
        + f"Maximum paper risk: Rs {setup.risk_rupees:,.2f}\n\n"
        + f"Evidence:\n{reasons}\n\n"
        + f"Warnings:\n{warnings}{id_line}\n\n"
        + "PAPER TRADING ONLY. No guaranteed result. Do not use this as financial advice."
    )


def row_message(row: sqlite3.Row) -> str:
    return (
        f"#{row['id']} {row['symbol']} {row['decision']} | "
        f"score {row['score']} | {row['status']} | "
        f"{row['created_at'][11:16]} IST"
    )


class BotController:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.engine = AnalysisEngine(config, self.db)
        self.last_alert: dict[str, str] = {}
        for chat_id in config.allowed_chat_ids:
            self.db.subscribe(chat_id)

    def authorized(self, update: Update) -> bool:
        if not self.config.allowed_chat_ids:
            return True
        chat = update.effective_chat
        return bool(chat and chat.id in self.config.allowed_chat_ids)

    async def guard(self, update: Update) -> bool:
        if self.authorized(update):
            if update.effective_chat:
                self.db.subscribe(update.effective_chat.id)
            return True
        if update.effective_message:
            await update.effective_message.reply_text("This bot is private.")
        return False

    async def reply(self, update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        await self.reply(
            update,
            "10X THINK PRO is ready.\n\n"
            "This bot creates paper-trading scenarios only. It does not place broker orders.\n"
            "Use /help for commands. Strong alerts are sent only when all hard risk filters pass.",
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        await self.reply(
            update,
            "Commands:\n"
            "/analysis SYMBOL - multi-timeframe analysis\n"
            "/nifty /banknifty - quick index analysis\n"
            "/watchlist - configured symbols\n"
            "/setups - recent paper setups\n"
            "/journal - recent journal rows\n"
            "/close ID R - close a paper setup, e.g. /close 4 1.5\n"
            "/stats - closed-trade performance in R\n"
            "/risk - daily risk lock status\n"
            "/today - today's scans/setups\n"
            "/backtest SYMBOL - simple non-lookahead 15M test\n"
            "/settings - active safety settings\n"
            "/status - data source and bot status\n\n"
            "Not investment advice. Past performance is not a guarantee.",
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        snap = self.db.risk_snapshot(self.config)
        await self.reply(
            update,
            f"Status: running\n"
            f"Data: Yahoo Finance (read-only; not guaranteed real-time)\n"
            f"Timezone: Asia/Kolkata\n"
            f"Market: {'OPEN' if is_market_open(self.config) else 'CLOSED'}\n"
            f"Paper capital: Rs {self.config.paper_capital:,.2f}\n"
            f"Risk/trade: {fmt_pct(self.config.risk_per_trade)}\n"
            f"Daily lock: {fmt_pct(self.config.max_daily_loss)}\n"
            f"Today's paper setups: {snap['trades']}/{self.config.max_paper_trades}\n"
            f"Lock: {'YES - ' + snap['lock_reason'] if snap['locked'] else 'NO'}",
        )

    async def analyze_symbol(self, update: Update, symbol: str) -> None:
        if not await self.guard(update):
            return
        symbol = symbol.upper().strip() or "NIFTY"
        await self.reply(update, f"Fetching {symbol} data. Please wait...")
        try:
            setup = await self.engine.analyze(symbol)
            self.db.log_scan(setup.symbol, setup.decision, setup.score, setup.data_status)
            await self.reply(update, setup_message(setup, self.config))
        except Exception:
            LOG.exception("Analysis command failed")
            await self.reply(update, "Analysis failed safely. Check logs and try again.")

    async def analysis(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        symbol = context.args[0] if context.args else "NIFTY"
        await self.analyze_symbol(update, symbol)

    async def quick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.effective_message.text if update.effective_message else ""
        symbol = text.split()[0].lstrip("/") if text else "NIFTY"
        await self.analyze_symbol(update, symbol)

    async def watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.guard(update):
            await self.reply(update, "Watchlist:\n" + "\n".join(f"- {s}" for s in self.config.watchlist))

    async def setups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        rows = self.db.recent_setups(10)
        await self.reply(update, "Recent paper setups:\n" + ("\n".join(row_message(row) for row in rows) or "No setups yet."))

    async def journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.setups(update, context)

    async def close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        if len(context.args) != 2:
            await self.reply(update, "Use: /close ID R\nExample: /close 4 1.5")
            return
        try:
            setup_id = int(context.args[0])
            r_multiple = float(context.args[1])
            if not -10 <= r_multiple <= 10:
                raise ValueError
        except ValueError:
            await self.reply(update, "ID must be an integer and R must be between -10 and 10.")
            return
        updated = self.db.close_setup(setup_id, r_multiple)
        await self.reply(update, "Journal updated." if updated else "Open setup ID not found.")

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        stats = self.db.stats()
        if not stats.get("trades"):
            await self.reply(update, "No closed paper trades yet.")
            return
        profit_factor = stats["profit_factor"]
        await self.reply(
            update,
            f"Closed paper-trade stats\n"
            f"Trades: {stats['trades']}\n"
            f"Wins/losses: {stats['wins']}/{stats['losses']}\n"
            f"Win rate: {fmt_pct(stats['win_rate'])}\n"
            f"Average R: {stats['avg_r']:.2f}\n"
            f"Expectancy: {stats['expectancy_r']:.2f}R\n"
            f"Profit factor: {'inf' if math.isinf(profit_factor) else f'{profit_factor:.2f}'}\n"
            f"Max drawdown: {stats['max_drawdown_r']:.2f}R\n"
            f"Max losing streak: {stats['max_losing_streak']}",
        )

    async def risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        snap = self.db.risk_snapshot(self.config)
        await self.reply(
            update,
            f"Risk control\n"
            f"Today's paper P/L: Rs {snap['pnl']:,.2f}\n"
            f"Daily loss limit: Rs {snap['loss_limit']:,.2f}\n"
            f"Consecutive losses: {snap['consecutive_losses']}/{self.config.max_consecutive_losses}\n"
            f"Trades today: {snap['trades']}/{self.config.max_paper_trades}\n"
            f"Trading lock: {'ACTIVE - ' + snap['lock_reason'] if snap['locked'] else 'not active'}",
        )

    async def today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.risk(update, context)

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        await self.reply(
            update,
            f"Settings\n"
            f"Watchlist: {', '.join(self.config.watchlist)}\n"
            f"Min evidence score: {self.config.min_score}/100\n"
            f"Scan interval: {self.config.scan_interval_seconds // 60} minutes\n"
            f"Alerts: {self.config.notification_mode}\n"
            f"Risk/trade: {fmt_pct(self.config.risk_per_trade)}\n"
            f"Max daily loss: {fmt_pct(self.config.max_daily_loss)}\n"
            f"Paper mode: ON (hard-coded; no order execution)",
        )

    async def backtest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        symbol = context.args[0].upper() if context.args else "NIFTY"
        await self.reply(update, f"Running conservative 15M backtest for {symbol}...")
        try:
            frame = add_indicators(await self.engine.market.fetch(symbol, "15m"))
            result = self.simple_backtest(frame)
            await self.reply(update, result)
        except Exception:
            LOG.exception("Backtest failed")
            await self.reply(update, "Backtest failed safely. No result was fabricated.")

    @staticmethod
    def simple_backtest(frame: pd.DataFrame) -> str:
        if frame.empty or len(frame) < 80:
            return "DATA UNAVAILABLE: not enough 15M history for backtest."
        results: list[float] = []
        for i in range(55, len(frame) - 1):
            previous = frame.iloc[i - 1]
            signal = frame.iloc[i]
            following = frame.iloc[i + 1]
            long = signal["close"] > signal["ema20"] > signal["ema50"] and signal["close"] > signal["vwap"]
            short = signal["close"] < signal["ema20"] < signal["ema50"] and signal["close"] < signal["vwap"]
            if not (long or short):
                continue
            entry = float(following["open"])
            atr = float(signal["atr"])
            if not math.isfinite(atr) or atr <= 0:
                continue
            stop_distance = atr * 1.2
            target_distance = stop_distance * 2
            future = frame.iloc[i + 1 : min(i + 9, len(frame))]
            r_result = None
            for _, candle in future.iterrows():
                if long and candle["low"] <= entry - stop_distance:
                    r_result = -1.0
                    break
                if short and candle["high"] >= entry + stop_distance:
                    r_result = -1.0
                    break
                if long and candle["high"] >= entry + target_distance:
                    r_result = 2.0
                    break
                if short and candle["low"] <= entry - target_distance:
                    r_result = 2.0
                    break
            results.append(0.0 if r_result is None else r_result)
        if not results:
            return "No qualifying historical signals in the available sample."
        wins = sum(1 for r in results if r > 0)
        losses = sum(1 for r in results if r < 0)
        return (
            "Educational backtest (not a prediction)\n"
            f"Signals: {len(results)}\n"
            f"Wins/losses: {wins}/{losses}\n"
            f"Win rate: {wins / len(results) * 100:.1f}%\n"
            f"Average R: {sum(results) / len(results):.2f}\n"
            "Yahoo Finance intraday history and slippage/charges can materially change results."
        )

    async def scan_once(self, app: Application) -> None:
        if not is_market_open(self.config):
            return
        for symbol in self.config.watchlist:
            try:
                setup = await self.engine.analyze(symbol)
                self.db.log_scan(setup.symbol, setup.decision, setup.score, setup.data_status)
                if (
                    setup.decision in {"PAPER LONG", "PAPER SHORT"}
                    and setup.score >= self.config.min_score
                    and not self.db.latest_alert_fingerprint(setup.fingerprint)
                ):
                    setup_id = self.db.insert_setup(setup)
                    self.last_alert[symbol] = setup.fingerprint
                    for chat_id in self.db.subscribers():
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=setup_message(setup, self.config, setup_id),
                        )
            except Exception:
                LOG.exception("Scheduled scan failed for %s", symbol)

    async def scanner_loop(self, app: Application) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await self.scan_once(app)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Scanner loop failed; continuing safely")
            await asyncio.sleep(self.config.scan_interval_seconds)


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def self_test() -> None:
    assert round_down_quantity(50, 100, 95) == 10
    assert round_down_quantity(50, 100, 100) == 0
    assert is_market_open(Config(bot_token="x"), datetime(2026, 8, 17, 10, 0, tzinfo=IST))
    assert not is_market_open(Config(bot_token="x"), datetime(2026, 8, 16, 10, 0, tzinfo=IST))
    index = pd.date_range("2026-01-01", periods=100, freq="5min", tz=IST)
    close = pd.Series(np.linspace(100, 120, 100), index=index)
    frame = pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000,
        },
        index=index,
    )
    indicators = add_indicators(frame)
    assert indicators.iloc[-1]["ema20"] > 0
    assert indicators.iloc[-1]["atr"] > 0
    print("Self-test passed: risk sizing, market hours, indicators.")


async def main() -> None:
    load_dotenv()
    configure_logging()
    if "--self-test" in sys.argv:
        self_test()
        return
    config = Config.from_env()
    if not config.bot_token:
        raise SystemExit("BOT_TOKEN is missing. Put it in .env; never paste it into chat.")
    controller = BotController(config)
    app = Application.builder().token(config.bot_token).build()
    app.add_handler(CommandHandler("start", controller.start))
    app.add_handler(CommandHandler("help", controller.help))
    app.add_handler(CommandHandler("status", controller.status))
    app.add_handler(CommandHandler("analysis", controller.analysis))
    app.add_handler(CommandHandler("nifty", controller.quick))
    app.add_handler(CommandHandler("banknifty", controller.quick))
    app.add_handler(CommandHandler("watchlist", controller.watchlist))
    app.add_handler(CommandHandler("setups", controller.setups))
    app.add_handler(CommandHandler("journal", controller.journal))
    app.add_handler(CommandHandler("close", controller.close))
    app.add_handler(CommandHandler("stats", controller.stats))
    app.add_handler(CommandHandler("risk", controller.risk))
    app.add_handler(CommandHandler("today", controller.today))
    app.add_handler(CommandHandler("settings", controller.settings))
    app.add_handler(CommandHandler("backtest", controller.backtest))

    scanner: Optional[asyncio.Task[None]] = None
    try:
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            scanner = asyncio.create_task(controller.scanner_loop(app))
            LOG.info("10X THINK PRO started in PAPER mode")
            await asyncio.Event().wait()
    finally:
        if scanner:
            scanner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scanner
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOG.info("Stopped by user")