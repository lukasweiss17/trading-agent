"""
test_filters.py — Functionality tests for v2.1 improvements:
  #7 Bid-ask spread filter
  #8 Earnings blackout filter
  #9 EMA trend filter

Usage:
    python test_filters.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

# Load .env so credentials are available
from dotenv import load_dotenv
load_dotenv()

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"

def ok(msg):  print(f"{GREEN}  PASS{RESET}  {msg}")
def fail(msg): print(f"{RED}  FAIL{RESET}  {msg}"); _failures.append(msg)
def info(msg): print(f"{YELLOW}  INFO{RESET}  {msg}")

_failures: list[str] = []

# ── Bootstrap bot (suppresses INFO logs during test) ─────────────────────────
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING",
           format="<level>{level:<8}</level> | {message}", colorize=True)

import config
from bot import AlpacaOptionsBot

print("\n=== Initialising bot ===")
bot = AlpacaOptionsBot()
print()

# =============================================================================
# TEST 1 — Earnings blackout logic (unit test, no API needed)
# =============================================================================
print("── Test 1: Earnings blackout logic ──────────────────────────────────────")

today = date.today()

# 1a. Earnings TODAY → should block
bot._earnings_cache["TEST_A"] = today
assert bot._is_earnings_blackout("TEST_A"), "earnings today should block"
ok("1a. Earnings today → blocked")

# 1b. Earnings in DTE_MAX+1 days → should block
bot._earnings_cache["TEST_B"] = today + timedelta(days=config.DTE_MAX + 1)
assert bot._is_earnings_blackout("TEST_B"), "earnings at end of window should block"
ok(f"1b. Earnings in DTE_MAX+1={config.DTE_MAX+1} days → blocked")

# 1c. Earnings YESTERDAY → block (day-after IV crush)
bot._earnings_cache["TEST_C"] = today - timedelta(days=config.EARNINGS_BLACKOUT_DAYS_AFTER)
assert bot._is_earnings_blackout("TEST_C"), "day-after earnings should block"
ok("1c. Day-after earnings → blocked")

# 1d. Earnings far in the future → should NOT block
bot._earnings_cache["TEST_D"] = today + timedelta(days=30)
assert not bot._is_earnings_blackout("TEST_D"), "earnings 30 days away should allow"
ok("1d. Earnings 30 days away → allowed")

# 1e. Earnings unknown (None) → should NOT block
bot._earnings_cache["TEST_E"] = None
assert not bot._is_earnings_blackout("TEST_E"), "unknown earnings should allow"
ok("1e. Unknown earnings → allowed (don't over-block)")

# 1f. Ticker not in cache at all → should NOT block
assert not bot._is_earnings_blackout("NOT_IN_CACHE"), "missing cache entry should allow"
ok("1f. Ticker not in cache → allowed")

print()

# =============================================================================
# TEST 2 — EMA trend score & filter (uses live bar data)
# =============================================================================
print("── Test 2: EMA trend filter (live daily bars) ───────────────────────────")

# 2a. Score for SPY — should return 0–5, not error
spy_score = bot._ema_trend_score("SPY")
assert 0 <= spy_score <= 5, f"EMA score out of range: {spy_score}"
ok(f"2a. SPY EMA score = {spy_score}/5 (valid 0-5 range)")

# 2b. Score for QQQ
qqq_score = bot._ema_trend_score("QQQ")
assert 0 <= qqq_score <= 5
ok(f"2b. QQQ EMA score = {qqq_score}/5")

# 2c. Score for TSLA
tsla_score = bot._ema_trend_score("TSLA")
assert 0 <= tsla_score <= 5
ok(f"2c. TSLA EMA score = {tsla_score}/5")

# 2d. Filter logic — inject known score via monkey-patch and verify decisions
original_score_fn = bot._ema_trend_score

# Strong uptrend (score=5): calls should pass, puts should fail
bot._ema_trend_score = lambda t: 5
assert     bot._ema_trend_filter("X", "call", "NORMAL (VIX=15)"), "score=5 should allow CALL"
assert not bot._ema_trend_filter("X", "put",  "NORMAL (VIX=15)"), "score=5 should block PUT"
ok("2d. Uptrend (score=5): CALL allowed, PUT blocked")

# Strong downtrend (score=0): puts should pass, calls should fail
bot._ema_trend_score = lambda t: 0
assert not bot._ema_trend_filter("X", "call", "NORMAL (VIX=15)"), "score=0 should block CALL"
assert     bot._ema_trend_filter("X", "put",  "NORMAL (VIX=15)"), "score=0 should allow PUT"
ok("2e. Downtrend (score=0): PUT allowed, CALL blocked")

# Neutral (score=3): calls pass, puts fail (borderline — 3 = minimum for calls)
bot._ema_trend_score = lambda t: 3
assert     bot._ema_trend_filter("X", "call", "NORMAL (VIX=15)"), "score=3 should allow CALL"
assert not bot._ema_trend_filter("X", "put",  "NORMAL (VIX=15)"), "score=3 should block PUT"
ok("2f. Neutral (score=3): CALL allowed, PUT blocked")

# Borderline bearish (score=2): puts pass, calls fail
bot._ema_trend_score = lambda t: 2
assert not bot._ema_trend_filter("X", "call", "NORMAL (VIX=15)"), "score=2 should block CALL"
assert     bot._ema_trend_filter("X", "put",  "NORMAL (VIX=15)"), "score=2 should allow PUT"
ok("2g. Weak downtrend (score=2): PUT allowed, CALL blocked")

# PANIC regime: mean-reversion calls need extreme downtrend (score<=1)
bot._ema_trend_score = lambda t: 0
assert bot._ema_trend_filter("X", "call", "PANIC (VIX=45)"), "PANIC+score=0 should allow CALL"
ok("2h. PANIC regime + extreme downtrend (score=0): CALL allowed (mean reversion)")

bot._ema_trend_score = lambda t: 3
assert not bot._ema_trend_filter("X", "call", "PANIC (VIX=45)"), "PANIC+score=3 should block CALL"
ok("2i. PANIC regime + neutral trend (score=3): CALL blocked (not extreme enough)")

bot._ema_trend_score = original_score_fn  # restore
print()

# =============================================================================
# TEST 3 — Bid-ask spread filter (uses live option quotes)
# =============================================================================
print("── Test 3: Bid-ask spread filter (live option quotes) ───────────────────")

# 3a. Unit test spread logic by monkey-patching the API call
from alpaca.data.requests import OptionLatestQuoteRequest

class MockQuote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask

def make_mock_spread_check(bid, ask):
    """Replace option_data client temporarily."""
    class MockOptionData:
        def get_option_latest_quote(self, req):
            sym = req.symbol_or_symbols
            # symbol_or_symbols is a str or list — never iterate a str char by char
            if isinstance(sym, str):
                key = sym
            else:
                key = list(sym)[0]
            return {key: MockQuote(bid, ask)}
    return MockOptionData()

original_option_data = bot.option_data

# Tight spread (acceptable): bid=0.50 ask=0.55 → spread=0.05, spread_pct=9%
bot.option_data = make_mock_spread_check(0.50, 0.55)
ok_flag, mid = bot._check_option_spread("SPY_FAKE")
assert ok_flag and abs(mid - 0.525) < 0.001, f"tight spread should pass, got ok={ok_flag} mid={mid}"
ok(f"3a. Tight spread: bid=0.50 ask=0.55 → allowed (spread=9%, mid={mid:.3f})")

# Wide spread pct (>15%): bid=0.20 ask=0.30 → spread=0.10, spread_pct=40%
bot.option_data = make_mock_spread_check(0.20, 0.30)
ok_flag, mid = bot._check_option_spread("SPY_FAKE")
assert not ok_flag, f"wide spread_pct should fail, got ok={ok_flag}"
ok("3b. Wide spread %: bid=0.20 ask=0.30 → blocked (spread_pct=40%)")

# Wide spread abs (>$0.10): bid=0.80 ask=0.95 → spread=0.15, spread_pct=17%
bot.option_data = make_mock_spread_check(0.80, 0.95)
ok_flag, mid = bot._check_option_spread("SPY_FAKE")
assert not ok_flag, f"wide abs spread should fail, got ok={ok_flag}"
ok("3c. Wide absolute spread: bid=0.80 ask=0.95 → blocked (spread=$0.15)")

# Exactly at limit: bid=0.50 ask=0.60 → spread=0.10 (==MAX_OPTION_SPREAD_ABS)
# Should be blocked (> is strict but == is also blocked since > threshold)
bot.option_data = make_mock_spread_check(0.50, 0.60)
ok_flag, mid = bot._check_option_spread("SPY_FAKE")
# spread=0.10 exactly equals MAX_OPTION_SPREAD_ABS → blocked (not strictly less than)
assert not ok_flag, f"spread at exact limit should fail"
ok("3d. Spread at exact abs limit ($0.10): blocked (not strictly less than limit)")

# Zero ask (no market) → should fail
bot.option_data = make_mock_spread_check(0.0, 0.0)
ok_flag, mid = bot._check_option_spread("SPY_FAKE")
assert not ok_flag, "zero ask should fail"
ok("3e. Zero ask (no market) → blocked")

bot.option_data = original_option_data  # restore

# 3b. Real live quote test for SPY (should be tight, liquid)
import datetime as _dt
from bot import ET as _ET
now_et = _dt.datetime.now(_ET)
market_open = 9 <= now_et.hour < 16
if market_open:
    from bot import TF_5MIN
    bars = bot._get_bars("SPY", __import__("alpaca.data.timeframe", fromlist=["TimeFrame"]).TimeFrame.Day, 5)
    if bars is not None and not bars.empty:
        # Get a real SPY option
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import ContractType
        from datetime import timedelta as _td
        today = date.today()
        req = GetOptionContractsRequest(
            underlying_symbols=["SPY"],
            expiration_date_gte=today + _td(days=1),
            expiration_date_lte=today + _td(days=3),
            type=ContractType.CALL,
            limit=5,
        )
        resp = bot.trading_client.get_option_contracts(req)
        contracts = getattr(resp, "option_contracts", []) or []
        if contracts:
            atm_sym = contracts[0].symbol
            ok_flag, mid = bot._check_option_spread(atm_sym)
            info(f"3f. Live SPY option {atm_sym}: spread_ok={ok_flag} mid={mid}")
        else:
            info("3f. No live SPY contracts found (market may be closed or weekend)")
    else:
        info("3f. No SPY bar data (market may be closed)")
else:
    info("3f. Market closed — skipping live spread test")

print()

# =============================================================================
# TEST 4 — Earnings cache refresh (live yfinance call)
# =============================================================================
print("── Test 4: Earnings cache refresh (live yfinance) ───────────────────────")

try:
    import yfinance as yf
    # Force refresh by resetting date
    bot._earnings_cache_date = date.min
    bot._refresh_earnings_cache()
    cached_count = sum(1 for v in bot._earnings_cache.values() if v is not None)
    ok(f"4a. Cache refreshed: {len(bot._earnings_cache)} tickers, {cached_count} with known earnings dates")

    # Check a few well-known tickers
    for t in ["AAPL", "NVDA", "TSLA"]:
        dt = bot._earnings_cache.get(t)
        if dt:
            ok(f"4b. {t} next earnings: {dt}")
        else:
            info(f"4b. {t} earnings date not found (may be post-season)")

    # Verify idempotency: second call should not re-fetch
    import time
    t0 = time.time()
    bot._refresh_earnings_cache()
    elapsed = time.time() - t0
    assert elapsed < 0.05, f"Second refresh should be instant (cached), took {elapsed:.2f}s"
    ok(f"4c. Cache idempotency: second refresh skipped in {elapsed*1000:.0f}ms")

except ImportError:
    info("4a. yfinance not installed — earnings tests skipped")

print()

# =============================================================================
# SUMMARY
# =============================================================================
total = 20  # approximate expected checks
print("═" * 60)
if _failures:
    print(f"{RED}FAILED: {len(_failures)} assertion(s){RESET}")
    for f in _failures:
        print(f"  • {f}")
    sys.exit(1)
else:
    print(f"{GREEN}ALL TESTS PASSED{RESET}")
    print("v2.1 filters (earnings, spread, EMA) are working correctly.")
print("═" * 60)
