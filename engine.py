"""Paper-only, fail-closed swing execution prototype.

DRY_RUN=true by default. Explicit ENABLE_PAPER_ORDERS=true required for paper orders.
Not investment advice; sample signal needs independent validation.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger("swing")
NY = ZoneInfo("America/New_York")
SYMBOLS = ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA")
RISK_FRACTION = Decimal("0.005")
MAX_NOTIONAL_FRACTION = Decimal("0.10")


def flag(name: str) -> bool:
    return os.getenv(name, "false").lower() == "true"


def run():
    key = os.environ["APCA_API_KEY_ID"]
    secret = os.environ["APCA_API_SECRET_KEY"]
    trading = TradingClient(key, secret, paper=True)
    market = StockHistoricalDataClient(key, secret)
    account = trading.get_account()
    clock = trading.get_clock()
    now = datetime.now(timezone.utc)
    LOG.info("Paper account status=%s clock_open=%s", account.status, clock.is_open)
    if not clock.is_open:
        LOG.info("Market closed: no entries")
        return
    if account.trading_blocked or account.account_blocked:
        raise RuntimeError("Trading blocked")
    if not flag("ENABLE_PAPER_ORDERS"):
        LOG.info("Observation mode: ENABLE_PAPER_ORDERS not true")

    # Pull extra lookback; the free IEX feed is not full-market coverage.
    end = now - timedelta(minutes=20)
    start = end - timedelta(days=12)
    bars = market.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=list(SYMBOLS),
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start, end=end, feed=DataFeed.IEX,
    )).df
    if bars.empty:
        raise RuntimeError("No IEX bars; fail closed")
    positions = {p.symbol for p in trading.get_all_positions()}
    orders = trading.get_orders()
    pending = {o.symbol for o in orders if str(o.status).lower().split('.')[-1] in
               {"new", "accepted", "pending_new", "partially_filled", "held", "pending_replace"}}
    equity = Decimal(str(account.equity))
    cash = Decimal(str(account.cash))
    if equity <= 0:
        raise RuntimeError("Nonpositive equity")

    for symbol in SYMBOLS:
        if symbol in positions or symbol in pending:
            LOG.info("%s: already held or pending", symbol)
            continue
        try:
            frame = bars.xs(symbol, level="symbol").sort_index()
        except KeyError:
            LOG.info("%s: no bars", symbol)
            continue
        # Only completed, regular-session bars; no backfilling missed orders.
        local = frame.index.tz_convert(NY)
        frame = frame[(local.hour > 9) | ((local.hour == 9) & (local.minute >= 30))]
        local = frame.index.tz_convert(NY)
        frame = frame[(local.hour < 16)]
        if len(frame) < 22:
            LOG.info("%s: insufficient bars", symbol)
            continue
        latest = frame.iloc[-1]
        previous = frame.iloc[-21:-1]
        # Experimental signal only: close above previous 20-bar high with rising volume.
        breakout = float(latest.close) > float(previous.high.max())
        volume_ok = float(latest.volume) > 1.5 * float(previous.volume.mean())
        LOG.info("%s: breakout=%s volume=%s last=%s", symbol, breakout, volume_ok, latest.close)
        if not (breakout and volume_ok):
            continue
        price = Decimal(str(latest.close))
        stop = Decimal(str(previous.low.min()))
        per_share_risk = price - stop
        if per_share_risk <= 0 or per_share_risk / price > Decimal("0.08"):
            LOG.info("%s: invalid/too-wide stop", symbol)
            continue
        qty_by_risk = (equity * RISK_FRACTION / per_share_risk).to_integral_value(rounding=ROUND_DOWN)
        qty_by_cap = (equity * MAX_NOTIONAL_FRACTION / price).to_integral_value(rounding=ROUND_DOWN)
        qty_by_cash = (cash / price).to_integral_value(rounding=ROUND_DOWN)
        qty = int(min(qty_by_risk, qty_by_cap, qty_by_cash))
        if qty < 1:
            LOG.info("%s: size below one share", symbol)
            continue
        LOG.info("SIGNAL %s qty=%s reference=%s stop_reference=%s", symbol, qty, price, stop)
        if not flag("ENABLE_PAPER_ORDERS"):
            continue
        # Explicit paper-only order. Market order can fill at a different price.
        # This prototype DOES NOT place a protective stop: keep orders disabled
        # until exits, persistence, and portfolio-level risk are implemented.
        raise RuntimeError("Paper execution intentionally locked: exit protection not implemented")


if __name__ == "__main__":
    run()
