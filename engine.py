#!/usr/bin/env python3
"""Alpaca paper-only swing scanner, replay, guarded order execution and reconciliation.

Modes:
  observe: data + signals, no orders, works after market close
  replay:  historical portfolio replay, writes reports/, no orders
  paper:   broker-managed BRACKET entry; requires two explicit safety gates
  report:  account/order status + optional reporting, no orders

Always TradingClient(..., paper=True). NEVER supports live-trading credentials.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from strategy import Limits, Signal, bar_is_fresh, candidate_at, current_reg_session, shares_for_trade
from replay import run_replay

LOG = logging.getLogger("swing")
SYMBOLS = tuple(s.strip().upper() for s in os.getenv(
    "SYMBOLS", "SPY,QQQ,IWM,AAPL,MSFT,NVDA").split(",") if s.strip())
CLIENT_PREFIX = "sw-"
ACTIVE = {"new", "accepted", "pending_new", "partially_filled", "held",
          "accepted_for_bidding", "pending_cancel", "pending_replace", "stopped",
          "calculated", "suspended"}


def as_text(value) -> str:
    return str(getattr(value, "value", value)).lower()


def dec(value) -> Decimal:
    return Decimal(str(value))


def flag(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() == "true"


def money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def notify(message: str, kind: str = "info") -> None:
    LOG.info("ALERT [%s] %s", kind.upper(), message)
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return
    style = os.environ.get("ALERT_WEBHOOK_KIND", "discord").lower()
    payload = {"text": message} if style == "slack" else {"content": message[:1950]}
    try:
        request = Request(url, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=8) as response:
            if response.status >= 300:
                LOG.warning("Webhook returned HTTP %s", response.status)
    except Exception as exc:
        LOG.warning("Webhook notification failed (%s)", type(exc).__name__)


@dataclass
class Clients:
    trading: object
    market: object
    limits: Limits


def connect() -> Clients:
    """All Alpaca SDK imports are deferred so pure business logic can be tested offline."""
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    key = os.environ["APCA_API_KEY_ID"]
    secret = os.environ["APCA_API_SECRET_KEY"]
    if not key or not secret:
        raise RuntimeError("Missing Alpaca keys")
    limits = replace(Limits(), strategy_capital=dec(os.getenv("STRATEGY_CAPITAL_USD", "25000")))
    if not 100 <= limits.strategy_capital <= 1000000:
        raise RuntimeError("Invalid STRATEGY_CAPITAL_USD")
    return Clients(TradingClient(key, secret, paper=True),
                   StockHistoricalDataClient(key, secret), limits)


def get_frames(client, symbols: tuple[str, ...], days: int) -> tuple[dict, dict]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    # Fetch each symbol separately so the 10k request cap does not truncate an
    # early symbol when a long replay is requested.
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=2)
    intraday, daily = {}, {}
    for symbol in symbols:
        req_5m = StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=end - timedelta(days=days), end=end, feed=DataFeed.IEX, limit=10000)
        req_daily = StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame.Day,
            start=end - timedelta(days=days + 110), end=end,
            feed=DataFeed.IEX, limit=10000)
        frame_5m = client.get_stock_bars(req_5m).df
        frame_daily = client.get_stock_bars(req_daily).df
        if frame_5m.empty or frame_daily.empty:
            LOG.warning("%s: missing IEX 5m or daily bars", symbol)
            continue
        def symbol_frame(frame):
            if isinstance(frame.index, pd.MultiIndex):
                return frame.xs(symbol, level="symbol").sort_index()
            return frame.sort_index()
        # Historical endpoint may include the currently forming 5-minute bar.
        # Remove it first, otherwise every scheduled :03/:18/:33/:48 run
        # could inspect an incomplete :00/:15/:30/:45 candle and skip trades.
        completed_cutoff = now - timedelta(minutes=5, seconds=30)
        cleaned = current_reg_session(symbol_frame(frame_5m))
        intraday[symbol] = cleaned[cleaned.index <= completed_cutoff]
        daily[symbol] = symbol_frame(frame_daily)
        LOG.info("DATA %s intraday_bars=%d daily_bars=%d", symbol,
                 len(intraday[symbol]), len(daily[symbol]))
    if not intraday:
        raise RuntimeError("No IEX data returned. Failing closed")
    return intraday, daily


def recent_orders(trading):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    all_orders = trading.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.ALL, limit=500, nested=True,
        after=datetime.now(timezone.utc) - timedelta(days=65)))
    if len(all_orders) == 500:
        raise RuntimeError("Order history may be truncated (500). Refusing new orders")
    open_orders = trading.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN, limit=500, nested=False))
    if len(open_orders) == 500:
        raise RuntimeError("Open order query may be truncated. Refusing new orders")
    return all_orders, open_orders


def all_order_rows(orders):
    for parent in orders:
        yield parent
        for child in (getattr(parent, "legs", None) or []):
            yield child


def is_active(order) -> bool:
    return as_text(order.status) in ACTIVE


def buy_parent(orders, symbol):
    matched = [o for o in orders if o.symbol == symbol
               and str(getattr(o, "client_order_id", "")).startswith(CLIENT_PREFIX)
               and as_text(o.side) == "buy"
               and dec(getattr(o, "filled_qty", 0) or 0) > 0]
    # Most recent is correct if a symbol was exited and later re-entered.
    return max(matched, key=lambda o: getattr(o, "submitted_at", datetime.min.replace(tzinfo=timezone.utc))) if matched else None


def stop_leg(parent):
    children = getattr(parent, "legs", None) or []
    return next((leg for leg in children if as_text(getattr(leg, "type", ""))
                 in ("stop", "stop_limit") and is_active(leg)), None)


def managed_exposure(positions, all_orders):
    """Fail closed if any paper position is not known, bracketed, and protected.

    Never silently modify unrelated/manual holdings or orphan positions.
    """
    exposure = Decimal("0")
    risk = Decimal("0")
    parents = {}
    for pos in positions:
        if as_text(getattr(pos, "side", "long")) != "long":
            raise RuntimeError(f"Short position {pos.symbol}: account not isolated")
        parent = buy_parent(all_orders, pos.symbol)
        if parent is None:
            raise RuntimeError(f"Unmanaged position {pos.symbol}: use dedicated paper account")
        leg = stop_leg(parent)
        if leg is None:
            raise RuntimeError(f"PROTECTION ALERT {pos.symbol}: missing active stop leg; inspect Alpaca NOW")
        qty = dec(pos.qty)
        entry = dec(pos.avg_entry_price)
        stop = dec(leg.stop_price)
        if qty <= 0 or stop <= 0:
            raise RuntimeError("Invalid position / stop quantity")
        exposure += qty * entry
        risk += qty * max(Decimal("0"), entry - stop)
        parents[pos.symbol] = parent
    return exposure, risk, parents


def observe_signals(intraday, daily, *, limits: Limits,
                    live: bool, now: datetime) -> list[Signal]:
    found = []
    for symbol, frame in intraday.items():
        if len(frame) < 61:
            LOG.info("%s: insufficient intraday history", symbol)
            continue
        last_time = frame.index[-1].to_pydatetime()
        if live and not bar_is_fresh(last_time, now):
            LOG.info("%s: stale/incomplete bar at %s: no live orders", symbol, last_time)
            continue
        LOG.info("%s: inspected bar=%s (historical=%s)", symbol, last_time,
                 not live)
        signal = candidate_at(symbol, frame, daily[symbol], len(frame)-1, limits=limits)
        if signal:
            LOG.info("SIGNAL %s setup=%s ref=%.2f stop=%.2f target=%.2f volume_ratio=%.2f",
                     symbol, signal.setup, signal.reference, signal.stop,
                     signal.target, signal.volume_ratio)
            found.append(signal)
        else:
            LOG.info("%s: no validated signal", symbol)
    return found


def order_id(signal: Signal) -> str:
    # Alpaca client_order_id max length 48; deterministic across workflow restarts.
    when = signal.bar_time.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    return f"{CLIENT_PREFIX}{signal.symbol}-{when}"


def valid_session(trading, now: datetime) -> bool:
    clock = trading.get_clock()
    LOG.info("Market clock_open=%s next_close=%s", clock.is_open, clock.next_close)
    if not clock.is_open:
        return False
    # Avoid entry shortly before the bell, including early-close days.
    if (clock.next_close - now).total_seconds() < 25 * 60:
        LOG.info("Too close to session close; skip new entries")
        return False
    return True


def ensure_account_ready(account):
    if (as_text(account.status) != "active" or bool(account.trading_blocked)
            or bool(account.account_blocked)):
        raise RuntimeError("Alpaca paper account not active / blocked")
    if dec(account.equity) <= 0 or dec(account.cash) <= 0:
        raise RuntimeError("Paper account equity or cash nonpositive")


def validate_paper_gate():
    if (not flag("PAPER_ORDERS_ENABLED") or os.getenv("PAPER_ONLY_ACK") != "YES_PAPER_ONLY"
            or flag("KILL_SWITCH")):
        raise RuntimeError("PAPER ORDER LOCKED: set PAPER_ORDERS_ENABLED=true and "
                           "PAPER_ONLY_ACK=YES_PAPER_ONLY; ensure KILL_SWITCH=false")


def earnings_blocked(symbol: str, now: datetime) -> bool:
    """Optional user-maintained blackout dates; NOT an automated earnings feed."""
    raw = os.getenv("EARNINGS_DATES_JSON", "").strip()
    if not raw:
        return False
    try:
        dates = json.loads(raw)
        event_date = datetime.fromisoformat(str(dates[symbol])).date() if symbol in dates else None
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Invalid EARNINGS_DATES_JSON; fail closed") from exc
    return event_date is not None and abs((event_date - now.date()).days) <= 3


def time_exit_if_due(trading, positions, all_orders, now: datetime) -> bool:
    """Optional time-based liquidation. Must cancel bracket exits before closing.

    Warning: this creates a brief protection gap. Therefore disabled by default.
    A failure after cancellation requires immediate manual inspection of Alpaca.
    """
    if not flag("ENABLE_TIME_EXITS"):
        return False
    days = int(os.getenv("TIME_EXIT_CALENDAR_DAYS", "21"))
    if days < 7:
        raise RuntimeError("TIME_EXIT_CALENDAR_DAYS must be >=7")
    for p in positions:
        parent = buy_parent(all_orders, p.symbol)
        if not parent:
            continue  # managed_exposure will halt on unmanaged positions
        filled_at = getattr(parent, "filled_at", None)
        if filled_at is None or (now - filled_at).total_seconds() < days * 86400:
            continue
        LOG.warning("TIME EXIT due for %s after %d calendar days", p.symbol, days)
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            active_legs = [leg for leg in (getattr(parent, "legs", None) or [])
                           if is_active(leg)]
            # Alpaca bracket/OCO cancellation propagates to the sibling exit.
            # Cancel just ONE leg and wait for the whole group to clear.
            if active_legs:
                trading.cancel_order_by_id(active_legs[0].id)
            for _ in range(8):
                pending = trading.get_orders(filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN, limit=500, nested=False))
                if not any(o.symbol == p.symbol for o in pending):
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Bracket exit orders not fully canceled")
            # Re-read position immediately before broker-side close request.
            current = {q.symbol: q for q in trading.get_all_positions()}
            if p.symbol in current:
                result = trading.close_position(p.symbol)
                notify(f"PAPER TIME EXIT REQUESTED: {p.symbol} close order={result.id}; "
                       "verify the fill in Alpaca")
            else:
                LOG.info("%s already closed during cancellation", p.symbol)
            return True  # no new entries during manual/aged-position cleanup
        except Exception:
            notify(f"CRITICAL PAPER TIME EXIT {p.symbol}: inspect Alpaca immediately; "
                   "protective orders may have been canceled", "critical")
            raise
    return False


def trail_existing_stops(trading, parents, intraday, daily, now):
    """Optional broker-side bracket-stop replacement, upward only for long trades."""
    if not flag("ENABLE_TRAILING_STOPS"):
        return
    from alpaca.trading.requests import ReplaceOrderRequest
    for symbol, parent in parents.items():
        if symbol not in intraday or symbol not in daily:
            continue
        frame = intraday[symbol]
        if frame.empty or not bar_is_fresh(frame.index[-1].to_pydatetime(), now):
            continue
        leg = stop_leg(parent)
        if leg is None:
            continue
        last = dec(frame.iloc[-1]["close"])
        entry = dec(getattr(parent, "filled_avg_price", 0) or 0)
        if entry <= 0 or last < entry * Decimal("1.03"):
            continue  # wait for at least 3% favorable movement
        hist = daily[symbol]
        hist = hist[hist.index.tz_convert("America/New_York").date <
                    now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()]
        if len(hist) < 16:
            continue
        x = hist.tail(16).astype(float)
        pc = x["close"].shift(1)
        tr = pd.concat([x["high"]-x["low"], (x["high"]-pc).abs(),
                        (x["low"]-pc).abs()], axis=1).max(axis=1).iloc[1:]
        atr = dec(tr.mean())
        suggested = last - max(atr * Decimal("1.5"), last * Decimal("0.015"))
        old_stop = dec(leg.stop_price)
        new_stop = suggested.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if new_stop > old_stop + Decimal("0.02") and new_stop < last * Decimal("0.99"):
            try:
                updated = trading.replace_order_by_id(
                    leg.id, order_data=ReplaceOrderRequest(stop_price=money(new_stop)))
                notify(f"PAPER STOP REPLACE REQUESTED: {symbol} {old_stop} -> "
                       f"{new_stop} broker={updated.id}; verify acceptance")
            except Exception:
                notify(f"PAPER STOP REPLACE failed for {symbol}; inspect Alpaca", "critical")
                raise


def execute_paper(clients: Clients, intraday, daily, now: datetime):
    """Bracket-entry-only paper engine. Stops/targets hosted at Alpaca, not GitHub."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest

    validate_paper_gate()
    trading = clients.trading
    account = trading.get_account()
    ensure_account_ready(account)
    if not valid_session(trading, now):
        LOG.info("Market closed / end of session; no new entries")
        return

    all_orders, open_orders = recent_orders(trading)
    positions = trading.get_all_positions()
    if time_exit_if_due(trading, positions, all_orders, now):
        LOG.info("Time exit requested, skipping new entries")
        return
    exposure, risk, parents = managed_exposure(positions, all_orders)
    trail_existing_stops(trading, parents, intraday, daily, now)
    # Any unrelated open order signals account interference; no new entries.
    known_ids = {str(getattr(o, "id", ""))
                 for parent in all_orders
                 if str(getattr(parent, "client_order_id", "")).startswith(CLIENT_PREFIX)
                 for o in [parent, *(getattr(parent, "legs", None) or [])]}
    foreign = [o for o in open_orders if str(getattr(o, "id", "")) not in known_ids]
    if foreign:
        raise RuntimeError("Unknown open orders; refuse entries until reconciled")

    candidates = observe_signals(intraday, daily, limits=clients.limits, live=True, now=now)
    # One bracket per run limits correlated order storms and reduces account races.
    pending_symbols = {o.symbol for o in open_orders}
    history_ids = {str(getattr(o, "client_order_id", "")) for o in all_order_rows(all_orders)}
    equity = dec(account.equity)
    cash = min(dec(account.cash), dec(account.buying_power))
    # Existing open BUY orders reserve notional on top of current positions.
    reserved = sum((dec(getattr(o, "qty", 0) or 0) *
                    dec(getattr(o, "limit_price", 0) or 0)
                    for o in open_orders if as_text(o.side) == "buy"), Decimal("0"))
    available_cash = max(Decimal("0"), cash - reserved)
    for s in sorted(candidates, key=lambda s: (s.setup != "breakout", -s.volume_ratio)):
        if earnings_blocked(s.symbol, now):
            LOG.info("%s: configured earnings blackout", s.symbol)
            continue
        if s.symbol in parents or s.symbol in pending_symbols:
            LOG.info("%s: held or pending, skip", s.symbol)
            continue
        client_id = order_id(s)
        if client_id in history_ids:
            LOG.info("%s: previously submitted candle, skip", client_id)
            continue
        # A fresh REST read before placing an order to handle in-flight restarts.
        all_updated, open_updated = recent_orders(trading)
        if client_id in {str(getattr(o, "client_order_id", ""))
                         for o in all_order_rows(all_updated)}:
            LOG.info("%s: idempotent duplicate suppressed", client_id)
            continue
        if any(o.symbol == s.symbol for o in open_updated):
            continue
        if any(p.symbol == s.symbol for p in trading.get_all_positions()):
            continue
        price, stop, target = dec(s.reference), dec(s.stop), dec(s.target)
        qty = shares_for_trade(price, stop, equity, available_cash, exposure,
                               risk, len(positions), clients.limits)
        if qty <= 0:
            LOG.info("%s: risk/cash/portfolio cap rejected", s.symbol)
            continue
        # Broker will reject malformed bracket levels; do not fall back to unprotected BUY.
        req = MarketOrderRequest(
            symbol=s.symbol, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC, order_class=OrderClass.BRACKET,
            client_order_id=client_id, extended_hours=False,
            take_profit=TakeProfitRequest(limit_price=money(target)),
            stop_loss=StopLossRequest(stop_price=money(stop)))
        try:
            submitted = trading.submit_order(order_data=req)
        except Exception:
            # Submission failure may be ambiguous (timeout after accepted order).
            # NEVER blindly retry. Inspect broker by deterministic ID manually.
            notify(f"ALPACA PAPER: ambiguous order response for {client_id}; "
                   "inspect paper orders, no retry", "critical")
            raise
        notify(f"PAPER ENTRY SUBMITTED: {s.symbol} {qty} shares, "
               f"{s.setup}, ref ${s.reference:.2f}, stop ${s.stop:.2f}, "
               f"target ${s.target:.2f}, broker order {submitted.id}. "
               "Submitted is NOT filled.")
        LOG.info("Order status after submit=%s", submitted.status)
        break
    else:
        LOG.info("No eligible paper entries this run")


def order_snapshot(orders):
    """Append-only state changes for this bot's own bracket parents and legs."""
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for parent in orders:
        if not str(getattr(parent, "client_order_id", "")).startswith(CLIENT_PREFIX):
            continue
        for o in [parent, *(getattr(parent, "legs", None) or [])]:
            status = as_text(o.status)
            qty = str(getattr(o, "filled_qty", 0) or 0)
            price = str(getattr(o, "filled_avg_price", "") or "")
            key = f"{o.id}|{status}|{qty}|{price}"
            rows.append([key, now, str(o.id), str(getattr(o, "client_order_id", "")),
                         str(o.symbol), as_text(o.side), as_text(o.type), status,
                         str(getattr(o, "qty", "")), qty, price,
                         str(getattr(o, "submitted_at", ""))])
    return rows


def sync_sheet(orders):
    """OPTIONAL: use a NEW tab; never touch old Swing Trading/Investing tabs."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    auth = os.getenv("GOOGLE_SA_JSON_BASE64", "").strip()
    if not sheet_id or not auth:
        LOG.info("Google Sheets sync not configured; broker orders remain source of truth")
        return
    import gspread
    try:
        credentials = json.loads(base64.b64decode(auth, validate=True))
        gc = gspread.service_account_from_dict(credentials)
        book = gc.open_by_key(sheet_id)
        name = "Alpaca Paper Orders"
        try:
            tab = book.worksheet(name)
        except gspread.WorksheetNotFound:
            tab = book.add_worksheet(title=name, rows=1000, cols=12)
            tab.append_row(["event_key", "captured_at_utc", "order_id", "client_order_id",
                            "symbol", "side", "type", "status", "qty", "filled_qty",
                            "filled_avg_price", "submitted_at"], value_input_option="RAW")
        existing = set(tab.col_values(1))
        new = [row for row in order_snapshot(orders) if row[0] not in existing]
        if new:
            tab.append_rows(new, value_input_option="RAW")
        LOG.info("Sheets: appended %s new order-state events", len(new))
    except Exception as exc:
        # Never turn a sync failure into an automatic resubmission of an order.
        LOG.error("Sheets sync failed: %s: %s", type(exc).__name__, exc)


def report_status(clients: Clients, *, weekly=False):
    trading = clients.trading
    account = trading.get_account()
    all_orders, open_orders = recent_orders(trading)
    positions = trading.get_all_positions()
    bot_parents = [o for o in all_orders
                   if str(getattr(o, "client_order_id", "")).startswith(CLIENT_PREFIX)]
    filled = [o for o in all_order_rows(bot_parents)
              if dec(getattr(o, "filled_qty", 0) or 0) > 0]
    message = ("ALPACA PAPER REPORT: "
               f"equity={account.equity}, cash={account.cash}, "
               f"positions={len(positions)}, open_orders={len(open_orders)}, "
               f"bot_order_parents_last_65d={len(bot_parents)}, "
               f"filled_order_events_last_65d={len(filled)}. "
               "Broker equity is NOT the separate historical Sheet P&L.")
    LOG.info(message)
    for p in positions:
        LOG.info("POSITION %s qty=%s avg=%s unrealized_pl=%s",
                 p.symbol, p.qty, p.avg_entry_price, p.unrealized_pl)
    if weekly:
        notify(message)
    sync_sheet(all_orders)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("observe", "replay", "paper", "report"),
                        default=os.getenv("MODE", "observe"))
    parser.add_argument("--days", type=int, default=int(os.getenv("REPLAY_DAYS", "45")))
    args = parser.parse_args()
    if not 10 <= args.days <= 100:
        raise ValueError("Replay days must be in [10, 100]; API fetch capped at 10k bars / symbol")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(set(SYMBOLS)) != len(SYMBOLS) or not SYMBOLS:
        raise RuntimeError("Symbols must be unique and non-empty")
    clients = connect()
    now = datetime.now(timezone.utc)
    if args.mode == "report":
        report_status(clients, weekly=True)
        return
    if args.mode == "paper":
        # Validate keys/safety BEFORE market data calls; fail closed.
        validate_paper_gate()
    days = args.days if args.mode == "replay" else 16
    intraday, daily = get_frames(clients.market, SYMBOLS, days)
    if args.mode == "replay":
        result = run_replay(intraday, daily, starting_cash=float(clients.limits.strategy_capital),
                            limits=clients.limits)
        LOG.info("REPLAY (indicative, IEX-only): %s", json.dumps(result))
    elif args.mode == "observe":
        account = clients.trading.get_account()
        LOG.info("Paper account status=%s", account.status)
        observe_signals(intraday, daily, limits=clients.limits, live=False, now=now)
        LOG.info("OBSERVATION ONLY: no orders placed")
    elif args.mode == "paper":
        execute_paper(clients, intraday, daily, now)
        # Re-fetch as authoritative fill/order-status state.
        report_status(clients)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.exception("Engine failed CLOSED: %s", error)
        # Do not leak keys; only exception type and generic message to log.
        sys.exit(1)
