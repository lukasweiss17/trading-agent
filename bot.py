"""
bot.py - Alpaca Paper Trading Options Bot  v2.0
================================================
Improvements over v1:
  1. IV Rank filter       — only buy when options are historically cheap (IVR < 35)
  2. Multi-timeframe RSI  — 1-min + 5-min + 15-min must all agree before entry
  3. Trade-window filter  — only trade 10:00-11:30 and 14:00-15:30 ET
  4. VIX regime engine    — 4-state volatility regime with dynamic thresholds
  5. Vertical debit spreads — reduces IV exposure and capital at risk
  6. Trailing take-profit — tiered high-water-mark stops; lets winners run

Usage:
    python bot.py
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz
import schedule
import ta
from loguru import logger

import config

# ---------------------------------------------------------------------------
# Alpaca imports
# ---------------------------------------------------------------------------
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import (
        StockBarsRequest,
        OptionLatestQuoteRequest,
        OptionSnapshotRequest,
    )
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
except ImportError as _exc:
    sys.exit(
        "[FATAL] Missing dependency: {}\nRun: pip install -r requirements.txt".format(_exc)
    )

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger.remove()
logger.add(
    "trading-bot.log",
    rotation="50 MB",
    retention="7 days",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    level="DEBUG",
)
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    level="INFO",
    colorize=True,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TRADE_LOG_FILE = Path("trade_log.json")
ROI_FILE       = Path("roi_tracker.json")
IV_HISTORY_FILE = Path("iv_history.json")
ET = pytz.timezone("America/New_York")

TF_5MIN  = TimeFrame(amount=5,  unit=TimeFrameUnit.Minute)
TF_15MIN = TimeFrame(amount=15, unit=TimeFrameUnit.Minute)


# ===========================================================================
class AlpacaOptionsBot:
    """
    Paper-trading options bot with full signal pipeline:
      Regime → Trade-Window → IV-Rank → Multi-TF RSI → Spread Entry → Trailing SL/TP
    """

    def __init__(self) -> None:
        logger.info("Alpaca Options Bot v2.0 starting up...")

        # Alpaca clients
        self.trading_client = TradingClient(
            api_key=config.API_KEY, secret_key=config.SECRET_KEY, paper=True
        )
        self.stock_data = StockHistoricalDataClient(
            api_key=config.API_KEY, secret_key=config.SECRET_KEY
        )
        self.option_data = OptionHistoricalDataClient(
            api_key=config.API_KEY, secret_key=config.SECRET_KEY
        )

        # Persistent state
        self.trade_log: list[dict] = self._load_json(TRADE_LOG_FILE, [])
        self.roi: dict             = self._load_json(ROI_FILE, self._default_roi())
        self.iv_history: dict      = self._load_json(IV_HISTORY_FILE, {})

        # In-memory state
        # key = long_contract_symbol; value includes all position metadata
        self.open_positions: dict[str, dict] = {}
        self._active_tickers: set[str] = set()
        self._blocked_tickers: set[str] = set()

        # Correlation groups — only one position allowed per group
        self._corr_groups: list[set[str]] = [
            {"SPY", "QQQ", "TQQQ", "SPXL"},
            {"IWM"},
            {"SOXL", "SMCI", "AMD", "NVDA"},
            {"TECL", "AAPL", "MSFT", "META", "GOOGL", "AMZN"},
            {"LABU"},
            {"FNGU"},
            {"TSLA"},
            {"MSTR", "COIN"},
            {"PLTR"},
            {"RKLB"},
        ]

        logger.info(
            "Initialised | Capital: ${:,.2f} | Tickers: {}",
            self.roi["current_capital"],
            ", ".join(config.TICKERS),
        )
        self._sync_positions_from_alpaca()

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    @staticmethod
    def _default_roi() -> dict:
        return {
            "starting_capital": config.STARTING_CAPITAL,
            "current_capital":  config.STARTING_CAPITAL,
            "total_trades":   0,
            "winning_trades": 0,
            "losing_trades":  0,
            "total_pnl":      0.0,
            "roi_pct":        0.0,
            "last_updated":   "",
        }

    @staticmethod
    def _load_json(path: Path, default):
        if path.exists():
            try:
                with open(path) as fh:
                    return json.load(fh)
            except Exception as exc:
                logger.warning("Could not load {}: {}", path, exc)
        return default

    def _save_json(self, path: Path, data) -> None:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2, default=str)

    # -----------------------------------------------------------------------
    # Startup sync
    # -----------------------------------------------------------------------

    def _sync_positions_from_alpaca(self) -> None:
        """Rebuild in-memory state from Alpaca on startup to survive restarts."""
        try:
            positions = self.trading_client.get_all_positions()
        except Exception as exc:
            logger.error("Could not sync positions from Alpaca: {}", exc)
            return

        log_entries: dict[str, dict] = {
            r["symbol"]: r for r in self.trade_log if r.get("action") == "OPEN"
        }

        synced = 0
        for pos in positions:
            symbol = pos.symbol
            if len(symbol) <= 6:
                continue
            ticker = next((t for t in config.TICKERS if symbol.startswith(t)), None)
            if ticker is None:
                continue

            direction = "put" if "P" in symbol[len(ticker):] else "call"
            log = log_entries.get(symbol, {})
            entry_price = log.get("entry_price") or float(pos.avg_entry_price or 0)

            self.open_positions[symbol] = {
                "ticker":             ticker,
                "direction":          direction,
                "qty":                int(pos.qty),
                "entry_price":        entry_price,
                "short_symbol":       log.get("short_symbol"),
                "short_entry_price":  log.get("short_entry_price", 0.0),
                "is_spread":          bool(log.get("short_symbol")),
                "open_ts":            log.get("timestamp", ""),
                "peak_pnl_pct":       0.0,
                "trailing_floor_pct": -config.STOP_LOSS_PCT,
            }
            self._active_tickers.add(ticker)
            synced += 1
            logger.info("[SYNC] Restored {} | {} x{} | entry={:.2f}",
                        symbol, direction.upper(), int(pos.qty), entry_price)

        logger.info("[SYNC] {} open position(s) restored from Alpaca",
                    synced if synced else "No")

    # -----------------------------------------------------------------------
    # Improvement #3 — Trade-window filter
    # -----------------------------------------------------------------------

    def _is_trade_window(self) -> bool:
        """
        Only trade during two high-quality ET windows:
          10:00–11:30  post-open momentum
          14:00–15:30  afternoon trend continuation
        """
        now_et = datetime.now(ET)
        h, m = now_et.hour, now_et.minute
        t = h * 60 + m
        for wh, wm, eh, em in config.TRADE_WINDOWS_ET:
            if wh * 60 + wm <= t <= eh * 60 + em:
                return True
        logger.debug("Outside trade window (ET {:02d}:{:02d}) — scan skipped", h, m)
        return False

    # -----------------------------------------------------------------------
    # Market data helpers
    # -----------------------------------------------------------------------

    def _get_bars(self, ticker: str, timeframe: TimeFrame, limit: int) -> Optional[pd.DataFrame]:
        """Fetch OHLCV bars for ticker at given timeframe."""
        try:
            req = StockBarsRequest(
                symbol_or_symbols=ticker, timeframe=timeframe, limit=limit
            )
            raw = self.stock_data.get_stock_bars(req)
            df = raw.df
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(ticker, level=0)
            return df.sort_index()
        except Exception as exc:
            logger.error("Bars fetch failed {} {}: {}", ticker, timeframe, exc)
            return None

    def calculate_rsi(self, prices: pd.Series) -> float:
        try:
            return float(
                ta.momentum.RSIIndicator(close=prices, window=config.RSI_PERIOD).rsi().iloc[-1]
            )
        except Exception:
            return 50.0

    def calculate_volume_momentum(self, volumes: pd.Series) -> float:
        try:
            avg = volumes.iloc[-(config.VOLUME_LOOKBACK + 1):-1].mean()
            return float(volumes.iloc[-1] / avg) if avg else 0.0
        except Exception:
            return 0.0

    # -----------------------------------------------------------------------
    # Improvement #4 — VIX regime engine
    # -----------------------------------------------------------------------

    def _get_vix_equivalent(self) -> float:
        """
        Estimate VIX from SPY's ATM option IV via Black-Scholes ATM approximation.
        Returns annualised IV as a percentage (e.g. 22.5 ≈ VIX 22.5).
        Falls back to 20.0 (neutral) on failure.
        """
        try:
            contracts = self.get_option_chain("SPY", "put")
            if not contracts:
                return 20.0

            bars = self._get_bars("SPY", TimeFrame.Minute, 5)
            if bars is None or bars.empty:
                return 20.0
            spot = float(bars["close"].iloc[-1])

            # Pick ATM put
            atm = min(contracts, key=lambda c: abs(float(c.strike_price) - spot))
            dte = max((atm.expiration_date - date.today()).days, 1)

            # Try to get IV directly from snapshot greeks
            try:
                snap_req = OptionSnapshotRequest(symbol_or_symbols=atm.symbol)
                snaps = self.option_data.get_option_snapshot(snap_req)
                snap = snaps.get(atm.symbol)
                if snap and snap.greeks and snap.greeks.implied_volatility:
                    return float(snap.greeks.implied_volatility) * 100
            except Exception:
                pass

            # Fall back: BS ATM approximation  σ ≈ P / (S · √(T/252) · 0.3989)
            mid = self.get_option_mid_price(atm.symbol)
            if mid and mid > 0:
                T = dte / 252.0
                iv = mid / (spot * math.sqrt(T) * 0.3989)
                return iv * 100
        except Exception as exc:
            logger.debug("VIX equivalent failed: {}", exc)
        return 20.0

    def _market_regime(self) -> dict:
        """
        Returns a dict with:
          signal_direction : 'call' | 'put' | 'neutral'
          rsi_oversold     : adjusted RSI oversold threshold
          rsi_overbought   : adjusted RSI overbought threshold
          use_spreads      : bool — whether to use debit spreads
          vix              : float
          label            : human-readable regime label
        """
        # SPY directional bias
        spy_bars = self._get_bars("SPY", TimeFrame.Minute, config.RSI_PERIOD + 10)
        if spy_bars is None or spy_bars.empty:
            return {"signal_direction": None, "label": "UNAVAILABLE"}
        spy_rsi = self.calculate_rsi(spy_bars["close"])

        vix = self._get_vix_equivalent()

        # VIX regime thresholds
        if vix > config.VIX_EXTREME:
            # Capitulation — mean-reversion calls regardless of SPY RSI
            regime = {
                "signal_direction": "call",
                "rsi_oversold":     60,
                "rsi_overbought":   80,
                "use_spreads":      True,
                "label":            f"PANIC (VIX={vix:.0f})",
            }
        elif vix > config.VIX_HIGH:
            # Fear — only puts, tight thresholds
            regime = {
                "signal_direction": "put" if spy_rsi < 50 else "neutral",
                "rsi_oversold":     35,
                "rsi_overbought":   65,
                "use_spreads":      True,
                "label":            f"FEAR (VIX={vix:.0f})",
            }
        elif vix > config.VIX_LOW:
            # Elevated — tighten thresholds slightly
            regime = {
                "signal_direction": "put" if spy_rsi < 50 else "call",
                "rsi_oversold":     37,
                "rsi_overbought":   63,
                "use_spreads":      True,
                "label":            f"ELEVATED (VIX={vix:.0f})",
            }
        else:
            # Normal — standard momentum thresholds
            regime = {
                "signal_direction": "put" if spy_rsi < 50 else "call",
                "rsi_oversold":     config.RSI_OVERSOLD,
                "rsi_overbought":   config.RSI_OVERBOUGHT,
                "use_spreads":      False,
                "label":            f"NORMAL (VIX={vix:.0f})",
            }

        regime["vix"] = vix
        logger.info("[REGIME] {} | SPY RSI={:.1f} → {} only",
                    regime["label"], spy_rsi, regime["signal_direction"])
        return regime

    # -----------------------------------------------------------------------
    # Improvement #1 — IV Rank filter
    # -----------------------------------------------------------------------

    def _get_current_iv(self, ticker: str, direction: str, price: float) -> Optional[float]:
        """
        Return current annualised IV (0–1 float) for ticker's ATM option.
        Tries snapshot greeks first, falls back to BS approximation.
        """
        try:
            contracts = self.get_option_chain(ticker, direction)
            if not contracts:
                return None
            atm = min(contracts, key=lambda c: abs(float(c.strike_price) - price))
            dte = max((atm.expiration_date - date.today()).days, 1)

            # Snapshot greeks
            try:
                snap_req = OptionSnapshotRequest(symbol_or_symbols=atm.symbol)
                snaps = self.option_data.get_option_snapshot(snap_req)
                snap = snaps.get(atm.symbol)
                if snap and snap.greeks and snap.greeks.implied_volatility:
                    return float(snap.greeks.implied_volatility)
            except Exception:
                pass

            # BS approximation
            mid = self.get_option_mid_price(atm.symbol)
            if mid and mid > 0 and price > 0:
                T = dte / 252.0
                return mid / (price * math.sqrt(T) * 0.3989)
        except Exception as exc:
            logger.debug("IV fetch failed for {}: {}", ticker, exc)
        return None

    def _update_iv_history(self, ticker: str, iv: float) -> None:
        """Append today's IV to rolling history, prune to IV_HISTORY_DAYS."""
        today_str = date.today().isoformat()
        if ticker not in self.iv_history:
            self.iv_history[ticker] = []
        # Avoid duplicate entries for the same day
        self.iv_history[ticker] = [
            e for e in self.iv_history[ticker] if e["date"] != today_str
        ]
        self.iv_history[ticker].append({"date": today_str, "iv": iv})
        # Keep only the last N days
        cutoff = (date.today() - timedelta(days=config.IV_HISTORY_DAYS)).isoformat()
        self.iv_history[ticker] = [
            e for e in self.iv_history[ticker] if e["date"] >= cutoff
        ]
        self._save_json(IV_HISTORY_FILE, self.iv_history)

    def _iv_rank(self, ticker: str, current_iv: float) -> Optional[float]:
        """
        Return IV Rank (0–100): where current IV sits in the rolling history.
        Returns None if insufficient history (< MIN_IV_HISTORY_POINTS).
        """
        history = self.iv_history.get(ticker, [])
        if len(history) < config.MIN_IV_HISTORY_POINTS:
            return None  # Not enough data yet — skip filter
        ivs = [e["iv"] for e in history]
        iv_min, iv_max = min(ivs), max(ivs)
        if iv_max == iv_min:
            return 50.0
        return (current_iv - iv_min) / (iv_max - iv_min) * 100

    # -----------------------------------------------------------------------
    # Improvement #2 — Multi-timeframe RSI signal
    # -----------------------------------------------------------------------

    def calculate_signal_multi_tf(
        self, ticker: str, regime: dict
    ) -> tuple[Optional[str], float, float, float]:
        """
        Compute RSI across 1-min, 5-min, 15-min timeframes.
        All three must agree directionally before a signal fires.
        Returns (signal, rsi_1m, vol_mom, price).
        """
        min_bars_1m  = config.RSI_PERIOD + config.VOLUME_LOOKBACK + 5
        min_bars_5m  = config.RSI_PERIOD + 5
        min_bars_15m = config.RSI_PERIOD + 3

        bars_1m  = self._get_bars(ticker, TimeFrame.Minute, min_bars_1m + 10)
        bars_5m  = self._get_bars(ticker, TF_5MIN,  min_bars_5m  + 5)
        bars_15m = self._get_bars(ticker, TF_15MIN, min_bars_15m + 3)

        if (bars_1m is None  or len(bars_1m)  < min_bars_1m  or
                bars_5m is None  or len(bars_5m)  < min_bars_5m  or
                bars_15m is None or len(bars_15m) < min_bars_15m):
            logger.debug("{}: insufficient multi-TF bars", ticker)
            return None, 0.0, 0.0, 0.0

        rsi_1m  = self.calculate_rsi(bars_1m["close"])
        rsi_5m  = self.calculate_rsi(bars_5m["close"])
        rsi_15m = self.calculate_rsi(bars_15m["close"])
        vol_mom = self.calculate_volume_momentum(bars_1m["volume"])
        price   = float(bars_1m["close"].iloc[-1])

        oversold  = regime["rsi_oversold"]
        overbought = regime["rsi_overbought"]

        logger.debug(
            "{} | ${:.2f} | RSI 1m={:.1f} 5m={:.1f} 15m={:.1f} | VolMom={:.2f}x",
            ticker, price, rsi_1m, rsi_5m, rsi_15m, vol_mom,
        )

        signal: Optional[str] = None

        # PUT: all three timeframes show downtrend
        if (rsi_1m < oversold and rsi_5m < oversold + 5 and rsi_15m < oversold + 10
                and vol_mom > config.VOLUME_THRESHOLD):
            signal = "put"
            logger.info(
                "[SIGNAL] PUT  {} | RSI 1m={:.0f} 5m={:.0f} 15m={:.0f} Vol={:.1f}x",
                ticker, rsi_1m, rsi_5m, rsi_15m, vol_mom,
            )

        # CALL: all three timeframes show uptrend
        elif (rsi_1m > overbought and rsi_5m > overbought - 5 and rsi_15m > overbought - 10
              and vol_mom > config.VOLUME_THRESHOLD):
            signal = "call"
            logger.info(
                "[SIGNAL] CALL {} | RSI 1m={:.0f} 5m={:.0f} 15m={:.0f} Vol={:.1f}x",
                ticker, rsi_1m, rsi_5m, rsi_15m, vol_mom,
            )

        return signal, rsi_1m, vol_mom, price

    # -----------------------------------------------------------------------
    # Options chain helpers
    # -----------------------------------------------------------------------

    def get_option_chain(self, ticker: str, direction: str) -> list:
        try:
            today   = date.today()
            exp_min = today + timedelta(days=config.DTE_MIN)
            exp_max = today + timedelta(days=config.DTE_MAX)
            ctype   = ContractType.CALL if direction == "call" else ContractType.PUT
            req = GetOptionContractsRequest(
                underlying_symbols=[ticker],
                expiration_date_gte=exp_min,
                expiration_date_lte=exp_max,
                type=ctype,
                limit=200,
            )
            resp = self.trading_client.get_option_contracts(req)
            contracts = getattr(resp, "option_contracts", []) or []
            logger.debug("{} {} chain: {} contracts", ticker, direction, len(contracts))
            return contracts
        except Exception as exc:
            logger.error("Chain fetch failed {} {}: {}", ticker, direction, exc)
            return []

    def select_contract(
        self, ticker: str, direction: str, current_price: float
    ) -> Optional[str]:
        """ATM contract — strike closest to current price."""
        contracts = self.get_option_chain(ticker, direction)
        if not contracts:
            logger.warning("No contracts for {} {}", ticker, direction)
            return None
        try:
            best = min(contracts, key=lambda c: abs(float(c.strike_price) - current_price))
            logger.info("Contract: {} | strike={} | exp={}",
                        best.symbol, best.strike_price, best.expiration_date)
            return best.symbol
        except Exception as exc:
            logger.error("Contract selection error: {}", exc)
            return None

    def get_option_mid_price(self, symbol: str) -> Optional[float]:
        """Return (bid + ask) / 2."""
        try:
            req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.option_data.get_option_latest_quote(req)
            q = quotes.get(symbol)
            if q is None:
                return None
            ask = float(q.ask_price)
            bid = float(q.bid_price)
            return (bid + ask) / 2.0 if ask > 0 else None
        except Exception as exc:
            logger.error("Quote fetch failed {}: {}", symbol, exc)
            return None

    # -----------------------------------------------------------------------
    # Improvement #5 — Vertical debit spreads
    # -----------------------------------------------------------------------

    def _select_spread_short_leg(
        self, ticker: str, direction: str, long_strike: float
    ) -> Optional[str]:
        """
        Find the OTM short leg for a debit spread.
        PUT spread  : short strike = long_strike * (1 - SPREAD_WIDTH_PCT)
        CALL spread : short strike = long_strike * (1 + SPREAD_WIDTH_PCT)
        """
        contracts = self.get_option_chain(ticker, direction)
        if not contracts:
            return None

        if direction == "put":
            target_strike = long_strike * (1 - config.SPREAD_WIDTH_PCT)
        else:
            target_strike = long_strike * (1 + config.SPREAD_WIDTH_PCT)

        # Must be further OTM than long leg
        if direction == "put":
            candidates = [c for c in contracts if float(c.strike_price) < long_strike]
        else:
            candidates = [c for c in contracts if float(c.strike_price) > long_strike]

        if not candidates:
            return None

        short_leg = min(candidates, key=lambda c: abs(float(c.strike_price) - target_strike))
        logger.debug("Short leg: {} | strike={}", short_leg.symbol, short_leg.strike_price)
        return short_leg.symbol

    def get_spread_net_price(
        self, long_symbol: str, short_symbol: Optional[str]
    ) -> Optional[float]:
        """Net spread value = long_mid - short_mid."""
        long_mid = self.get_option_mid_price(long_symbol)
        if long_mid is None:
            return None
        if not short_symbol:
            return long_mid
        short_mid = self.get_option_mid_price(short_symbol)
        if short_mid is None:
            return long_mid
        return long_mid - short_mid

    # -----------------------------------------------------------------------
    # Order management
    # -----------------------------------------------------------------------

    def _calc_qty(self, net_premium: float) -> int:
        budget = self.roi["current_capital"] * config.MAX_POSITION_SIZE_PCT
        if net_premium <= 0:
            return 0
        return max(1, int(budget / (net_premium * 100)))

    def place_option_order(self, symbol: str, qty: int, side: str) -> Optional[object]:
        if qty < 1:
            return None
        try:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                asset_class=AssetClass.US_OPTION,
            )
            order = self.trading_client.submit_order(req)
            logger.info("[ORDER] {} {} x{} | id={}", side.upper(), symbol, qty, order.id)
            return order
        except Exception as exc:
            err = str(exc)
            logger.error("Order failed {}: {}", symbol, err)
            for ticker in config.TICKERS:
                if not symbol.startswith(ticker):
                    continue
                if "pattern day trading" in err.lower():
                    self._blocked_tickers.add(ticker)
                    self._active_tickers.discard(ticker)
                    logger.warning("PDT block: {} skipped for session", ticker)
                elif "insufficient" in err.lower():
                    self._blocked_tickers.add(ticker)
                    logger.warning("Insufficient funds: {} blocked for session", ticker)
                break
            return None

    def log_trade(self, record: dict) -> None:
        self.trade_log.append(record)
        self._save_json(TRADE_LOG_FILE, self.trade_log)

    # -----------------------------------------------------------------------
    # Improvement #6 — Position monitoring with trailing stops
    # -----------------------------------------------------------------------

    def monitor_positions(self) -> None:
        """
        Evaluate every open position against trailing stop / take-profit rules.
        Trailing floors are updated progressively as the position gains.
        """
        if not self.open_positions:
            return

        for symbol, pos in list(self.open_positions.items()):
            try:
                short_sym = pos.get("short_symbol")
                current = self.get_spread_net_price(symbol, short_sym)
                if current is None:
                    logger.warning("No quote for {} - skipping", symbol)
                    continue

                entry    = pos["entry_price"]
                pnl_pct  = (current - entry) / entry if entry else 0.0

                # Update high-water mark
                if pnl_pct > pos.get("peak_pnl_pct", 0.0):
                    pos["peak_pnl_pct"] = pnl_pct

                # Update trailing floor from high-water mark
                floor = pos.get("trailing_floor_pct", -config.STOP_LOSS_PCT)
                for trigger, new_floor in config.TRAILING_STOPS:
                    if pos.get("peak_pnl_pct", 0.0) >= trigger:
                        floor = max(floor, new_floor)
                pos["trailing_floor_pct"] = floor

                logger.debug(
                    "{} | entry={:.2f} now={:.2f} PnL={:+.1f}% peak={:+.1f}% floor={:+.1f}%",
                    symbol, entry, current,
                    pnl_pct * 100,
                    pos.get("peak_pnl_pct", 0) * 100,
                    floor * 100,
                )

                close_reason: Optional[str] = None
                if pnl_pct <= floor:
                    close_reason = "TRAILING_STOP" if floor > -config.STOP_LOSS_PCT else "STOP_LOSS"

                if close_reason:
                    self._close_position(pos, symbol, current, close_reason)

            except Exception as exc:
                logger.error("Monitor error for {}: {}", symbol, exc)

    def _close_position(
        self, pos: dict, symbol: str, exit_net_price: float, reason: str
    ) -> None:
        """Close all legs of a position and record P&L."""
        qty = pos["qty"]

        # Close long leg
        order = self.place_option_order(symbol, qty, "sell")
        if order is None:
            return

        # Close short leg if spread
        short_sym = pos.get("short_symbol")
        if short_sym:
            self.place_option_order(short_sym, qty, "buy")

        entry    = pos["entry_price"]
        pnl_dollar = (exit_net_price - entry) * qty * 100
        pnl_pct    = round((exit_net_price - entry) / entry * 100, 2) if entry else 0.0
        label      = "WIN" if pnl_dollar >= 0 else "LOSS"

        logger.info("[{}] CLOSED {} | reason={} | PnL=${:+.2f} ({:+.1f}%)",
                    label, symbol, reason, pnl_dollar, pnl_pct)

        self.log_trade({
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "action":           "CLOSE",
            "ticker":           pos["ticker"],
            "symbol":           symbol,
            "short_symbol":     short_sym,
            "direction":        pos["direction"],
            "qty":              qty,
            "entry_price":      round(entry, 4),
            "exit_price":       round(exit_net_price, 4),
            "pnl_dollar":       round(pnl_dollar, 2),
            "pnl_pct":          pnl_pct,
            "reason":           reason,
            "peak_pnl_pct":     round(pos.get("peak_pnl_pct", 0) * 100, 1),
        })

        r = self.roi
        r["total_trades"] += 1
        r["total_pnl"]        = round(r["total_pnl"] + pnl_dollar, 2)
        r["current_capital"]  = round(r["current_capital"] + pnl_dollar, 2)
        if pnl_dollar >= 0:
            r["winning_trades"] += 1
        else:
            r["losing_trades"] += 1
        r["roi_pct"] = round(
            (r["current_capital"] - r["starting_capital"]) / r["starting_capital"] * 100, 2
        )
        r["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_json(ROI_FILE, r)

        del self.open_positions[symbol]
        self._active_tickers.discard(pos["ticker"])

    # -----------------------------------------------------------------------
    # Correlation helper
    # -----------------------------------------------------------------------

    def _corr_group_active(self, ticker: str) -> bool:
        for group in self._corr_groups:
            if ticker in group and group & self._active_tickers:
                return True
        return False

    # -----------------------------------------------------------------------
    # Main scan loop
    # -----------------------------------------------------------------------

    def run_scan(self) -> None:
        logger.info("--- Scan @ {} ---", datetime.now(timezone.utc).strftime("%H:%M:%S"))

        if not self.is_market_open():
            logger.info("Market closed — skipping scan")
            return

        # Filter #3: trade window
        if not self._is_trade_window():
            return

        # Filter #4: VIX regime
        regime = self._market_regime()
        allowed = regime.get("signal_direction")
        if allowed is None or allowed == "neutral":
            logger.info("[REGIME] Neutral — no trades this scan")
            return

        use_spreads = regime.get("use_spreads", False)

        # Collect and score candidates
        candidates: list[dict] = []
        for ticker in config.TICKERS:
            if ticker in self._blocked_tickers:
                continue
            if ticker in self._active_tickers:
                continue
            if self._corr_group_active(ticker):
                logger.debug("{}: correlated position open — skip", ticker)
                continue

            # Filter #2: multi-timeframe signal
            signal, rsi_1m, vol_mom, price = self.calculate_signal_multi_tf(ticker, regime)
            if signal is None or signal != allowed:
                continue

            # Filter #1: IV rank
            current_iv = self._get_current_iv(ticker, signal, price)
            if current_iv is not None:
                self._update_iv_history(ticker, current_iv)
                iv_rank = self._iv_rank(ticker, current_iv)
                if iv_rank is not None and iv_rank > config.IV_RANK_MAX:
                    logger.info(
                        "{}: IV rank {:.0f} > {} — options too expensive, skip",
                        ticker, iv_rank, config.IV_RANK_MAX,
                    )
                    continue
                logger.debug("{}: IV={:.2f} IVR={}", ticker, current_iv,
                             f"{iv_rank:.0f}" if iv_rank is not None else "building")

            rsi_strength = abs(rsi_1m - 50)
            score = rsi_strength * vol_mom
            candidates.append({
                "ticker": ticker, "signal": signal, "rsi": rsi_1m,
                "vol_mom": vol_mom, "price": price, "score": score,
            })

        if not candidates:
            logger.info("No qualifying signals this scan")
            return

        # Take top-scored signals up to position cap
        candidates.sort(key=lambda x: x["score"], reverse=True)
        max_new = config.MAX_CONCURRENT_POSITIONS - len(self.open_positions)
        candidates = candidates[:max(0, max_new)]

        for c in candidates:
            ticker  = c["ticker"]
            signal  = c["signal"]
            price   = c["price"]

            # Select long leg (ATM)
            long_symbol = self.select_contract(ticker, signal, price)
            if long_symbol is None:
                continue

            long_premium = self.get_option_mid_price(long_symbol)
            if long_premium is None or long_premium < config.MIN_PREMIUM:
                logger.warning("{}: premium {:.2f} < min {:.2f} — skip",
                               long_symbol, long_premium or 0, config.MIN_PREMIUM)
                continue

            # Filter #5: select spread short leg if regime calls for spreads
            short_symbol       = None
            short_entry_price  = 0.0
            net_premium        = long_premium

            if use_spreads:
                long_strike = float(long_symbol.split(ticker)[-1][7:])  # parse strike from OCC symbol
                short_symbol = self._select_spread_short_leg(ticker, signal, long_strike if long_strike > 0 else price)
                if short_symbol:
                    short_mid = self.get_option_mid_price(short_symbol)
                    if short_mid and short_mid > 0:
                        short_entry_price = short_mid
                        net_premium = max(long_premium - short_mid, 0.01)
                    else:
                        short_symbol = None  # fall back to naked

            if net_premium < config.MIN_PREMIUM:
                logger.warning("{}: net spread premium {:.2f} too low — skip", ticker, net_premium)
                continue

            qty  = self._calc_qty(net_premium)
            cost = qty * net_premium * 100

            if cost > self.roi["current_capital"] * config.MAX_POSITION_SIZE_PCT * 1.1:
                logger.warning("{}: cost ${:.0f} exceeds budget — skip", ticker, cost)
                continue

            spread_tag = f" [SPREAD short={short_symbol}]" if short_symbol else ""
            logger.info(
                "[ENTRY] {} {} | score={:.1f} | qty={} | net_prem={:.2f} | cost=${:.2f}{}",
                ticker, signal.upper(), c["score"], qty, net_premium, cost, spread_tag,
            )

            # Place long leg
            order = self.place_option_order(long_symbol, qty, "buy")
            if order is None:
                continue

            # Place short leg
            if short_symbol:
                short_order = self.place_option_order(short_symbol, qty, "sell")
                if short_order is None:
                    logger.warning("Short leg failed — holding naked long for {}", ticker)
                    short_symbol = None
                    short_entry_price = 0.0
                    net_premium = long_premium

            self.open_positions[long_symbol] = {
                "ticker":             ticker,
                "direction":          signal,
                "qty":                qty,
                "entry_price":        net_premium,
                "short_symbol":       short_symbol,
                "short_entry_price":  short_entry_price,
                "is_spread":          short_symbol is not None,
                "open_ts":            datetime.now(timezone.utc).isoformat(),
                "peak_pnl_pct":       0.0,
                "trailing_floor_pct": -config.STOP_LOSS_PCT,
            }
            self._active_tickers.add(ticker)

            self.log_trade({
                "timestamp":          datetime.now(timezone.utc).isoformat(),
                "action":             "OPEN",
                "ticker":             ticker,
                "symbol":             long_symbol,
                "short_symbol":       short_symbol,
                "short_entry_price":  round(short_entry_price, 4),
                "direction":          signal,
                "qty":                qty,
                "entry_price":        round(net_premium, 4),
                "long_premium":       round(long_premium, 4),
                "cost":               round(cost, 2),
                "rsi_1m":             round(c["rsi"], 2),
                "vol_momentum":       round(c["vol_mom"], 2),
                "score":              round(c["score"], 2),
                "regime":             regime["label"],
                "is_spread":          short_symbol is not None,
                "order_id":           str(order.id),
            })

    # -----------------------------------------------------------------------
    # Summary + market hours
    # -----------------------------------------------------------------------

    def print_roi_summary(self) -> None:
        r = self.roi
        trades   = r["total_trades"]
        wins     = r["winning_trades"]
        win_rate = (wins / trades * 100) if trades else 0.0
        logger.info(
            "[ROI] Capital=${:,.2f} | PnL=${:+,.2f} | ROI={:+.2f}% | "
            "Trades={} W={} L={} WinRate={:.0f}% | OpenPositions={}",
            r["current_capital"], r["total_pnl"], r["roi_pct"],
            trades, wins, r["losing_trades"], win_rate, len(self.open_positions),
        )

    def is_market_open(self) -> bool:
        try:
            return bool(self.trading_client.get_clock().is_open)
        except Exception as exc:
            logger.error("Market hours check failed: {}", exc)
            return False

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self) -> None:
        logger.info("Bot running. Press Ctrl+C to stop.")
        schedule.every(config.SCAN_INTERVAL_SECONDS).seconds.do(self.run_scan)
        schedule.every(config.MONITOR_INTERVAL_SECONDS).seconds.do(self.monitor_positions)
        schedule.every(config.ROI_REPORT_INTERVAL_MIN).minutes.do(self.print_roi_summary)

        self.run_scan()
        self.print_roi_summary()

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutdown requested. Final state:")
            self.print_roi_summary()
            logger.info("Bot stopped cleanly.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not config.API_KEY or not config.SECRET_KEY:
        sys.exit("[FATAL] ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Check .env file.")
    bot = AlpacaOptionsBot()
    bot.run()
