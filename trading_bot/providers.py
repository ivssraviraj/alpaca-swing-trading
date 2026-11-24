"""Official Alpaca clients, bounded transport and read-only retry policy."""

from __future__ import annotations

import random
import time
from datetime import timedelta
from urllib.parse import urlsplit

import pandas as pd
import requests
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient

from .calendar import regular_completed
from .config import PAPER_URL
from .diagnostics import emit, error_details, register_secrets, traced
from .models import Quote


class BoundedSession(requests.Session):
    def __init__(self, timeout, interval):
        super().__init__()
        self.timeout = timeout
        self.interval = interval
        self.last = 0.0

    def request(self, method, url, **kwargs):
        time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        kwargs.setdefault("timeout", self.timeout)
        kwargs["allow_redirects"] = False
        started = time.monotonic()
        emit("http_request", method=method, host=urlsplit(url).hostname, timeout=kwargs["timeout"])
        try:
            response = super().request(method, url, **kwargs)
        except Exception as exc:
            emit(
                "http_failure",
                "WARNING",
                method=method,
                host=urlsplit(url).hostname,
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                **error_details(exc),
            )
            raise
        emit(
            "http_response",
            method=method,
            host=urlsplit(url).hostname,
            status=getattr(response, "status_code", None),
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return response


def bounded(client, settings):
    # SDK 0.43 exposes no transport timeout option. Pinned compatibility is tested.
    client._session = BoundedSession(
        settings.request_timeout_seconds, settings.request_interval_seconds
    )
    client._retry = 0  # never retry POST/DELETE in the SDK
    return client


def read_call(fn, *args, **kwargs):
    operation = getattr(fn, "__qualname__", type(fn).__name__)
    for attempt in range(3):
        started = time.monotonic()
        emit("provider_read_started", operation=operation, attempt=attempt + 1)
        try:
            result = fn(*args, **kwargs)
            emit(
                "provider_read_completed",
                operation=operation,
                attempt=attempt + 1,
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
            )
            return result
        except Exception as exc:
            emit(
                "provider_read_failed",
                "WARNING",
                operation=operation,
                attempt=attempt + 1,
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                **error_details(exc),
            )
            code = getattr(exc, "status_code", None)
            response = getattr(exc, "response", None)
            if response is not None:
                code = response.status_code
            if attempt == 2 or (
                code not in (429, 500, 502, 503, 504)
                and not isinstance(exc, (requests.Timeout, requests.ConnectionError))
            ):
                raise
            retry_after = response.headers.get("Retry-After", "0") if response is not None else "0"
            try:
                delay = min(30, max(float(retry_after), 2**attempt + random.random()))
            except ValueError:
                delay = 2**attempt + random.random()
            emit(
                "provider_retry_scheduled",
                operation=operation,
                next_attempt=attempt + 2,
                delay_seconds=delay,
            )
            time.sleep(delay)


def clients(settings):
    register_secrets(settings)
    settings.require_broker_credentials()
    key, secret = (
        settings.apca_api_key_id.get_secret_value(),
        settings.apca_api_secret_key.get_secret_value(),
    )
    trading = bounded(TradingClient(key, secret, paper=True, url_override=PAPER_URL), settings)
    market = bounded(StockHistoricalDataClient(key, secret), settings)
    news = bounded(NewsClient(key, secret), settings)
    return trading, market, news


def batches(symbols, size):
    symbols = sorted(set(symbols))
    for i in range(0, len(symbols), size):
        yield symbols[i : i + size]


class MarketData:
    def __init__(self, client, settings, store):
        self.client, self.cfg, self.store = client, settings, store

    @traced("market_data.bars")
    def bars(self, symbols, start, end, daily=False):
        result, missing = {}, []
        for batch_number, batch in enumerate(batches(symbols, self.cfg.batch_size), 1):
            emit(
                "market_batch_requested",
                batch=batch_number,
                symbols=batch,
                start=start,
                end=end,
                timeframe="1Day" if daily else "5Min",
                feed=self.cfg.data_feed,
            )
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day if daily else TimeFrame(5, TimeFrameUnit.Minute),
                start=start,
                end=end,
                adjustment=Adjustment.SPLIT,
                feed=DataFeed(self.cfg.data_feed),
            )  # No total limit: SDK walks all pages.
            try:
                raw = read_call(self.client.get_stock_bars, request).df
                for symbol in batch:
                    if raw.empty or symbol not in raw.index.get_level_values("symbol"):
                        missing.append(symbol)
                        continue
                    frame = raw.xs(symbol, level="symbol").sort_index()
                    if frame.index.has_duplicates or frame.isna().any().any():
                        raise ValueError("Invalid or duplicate market bars")
                    if (frame[["open", "high", "low", "close"]] <= 0).any().any() or (
                        frame.volume < 0
                    ).any():
                        raise ValueError("Invalid OHLCV")
                    if (
                        (frame.high < frame[["open", "close", "low"]].max(axis=1))
                        | (frame.low > frame[["open", "close"]].min(axis=1))
                    ).any():
                        raise ValueError("Inconsistent OHLC")
                    if daily:
                        frame = frame[
                            frame.index.tz_convert("America/New_York").date
                            < pd.Timestamp(end).tz_convert("America/New_York").date()
                        ]
                    else:
                        frame = regular_completed(frame, end)
                    result[symbol] = frame
                emit(
                    "market_batch_completed",
                    batch=batch_number,
                    returned=[s for s in batch if s in result],
                    missing=[s for s in batch if s not in result],
                    bars={s: len(result[s]) for s in batch if s in result},
                )
            except Exception as exc:
                missing.extend(batch)
                self.store.audit(
                    "data_failure",
                    {
                        "symbols": batch,
                        "batch": batch_number,
                        "timeframe": "1Day" if daily else "5Min",
                        **error_details(exc),
                    },
                )
        self.store.audit(
            "coverage",
            {
                "requested": len(set(symbols)),
                "returned": len(result),
                "missing": sorted(set(missing)),
                "feed": self.cfg.data_feed,
                "daily": daily,
            },
        )
        return result

    @traced("market_data.daily")
    def daily(self, symbols, now):
        # A daily cache is reusable only within the same NY date and feed.
        key = (
            "daily:"
            + self.cfg.data_feed
            + ":"
            + pd.Timestamp(now).tz_convert("America/New_York").date().isoformat()
        )
        cached = self.store.get(key, {})
        missing = [s for s in symbols if s not in cached]
        emit(
            "daily_cache",
            requested=len(symbols),
            hits=len(symbols) - len(missing),
            misses=len(missing),
            feed=self.cfg.data_feed,
            as_of=now,
        )
        if missing:
            fetched = self.bars(missing, now - timedelta(days=130), now, daily=True)
            cached.update({s: encode_frame(f) for s, f in fetched.items()})
            self.store.put(key, cached)
        return {s: decode_frame(cached[s]) for s in symbols if s in cached}

    @traced("market_data.intraday")
    def intraday(self, symbols, now):
        key = (
            "intraday:"
            + self.cfg.data_feed
            + ":"
            + pd.Timestamp(now).tz_convert("America/New_York").date().isoformat()
        )
        cached = self.store.get(key, {})
        result = {}
        cold = [s for s in symbols if s not in cached]
        warm = [s for s in symbols if s in cached]
        for group, incremental in ((cold, False), (warm, True)):
            if not group:
                continue
            start = now - timedelta(days=7)
            if incremental:
                start = min(pd.Timestamp(cached[s]["index"][-1]) for s in group) - timedelta(
                    minutes=10
                )
            refreshed = self.bars(group, start, now)
            for symbol, frame in refreshed.items():
                if incremental:
                    prior = decode_frame(cached[symbol])
                    frame = pd.concat([prior, frame])
                    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
                    frame = frame[frame.index >= now - timedelta(days=7)]
                if not frame.empty:
                    result[symbol] = frame
                    cached[symbol] = encode_frame(frame)
        self.store.put(key, cached)
        self.store.audit(
            "data_refresh",
            {"cold_symbols": len(cold), "incremental_symbols": len(warm), "returned": len(result)},
            now,
        )
        return result

    @traced("market_data.quote")
    def quote(self, symbol):
        raw = read_call(
            self.client.get_stock_latest_quote,
            StockLatestQuoteRequest(symbol_or_symbols=[symbol], feed=DataFeed(self.cfg.data_feed)),
        )[symbol]
        quote = Quote(
            symbol=symbol,
            bid=raw.bid_price,
            ask=raw.ask_price,
            at=raw.timestamp,
            feed=self.cfg.data_feed,
        )
        emit(
            "quote_received",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            quote_at=quote.at,
            feed=quote.feed,
        )
        return quote


def encode_frame(frame):
    return {"index": [x.isoformat() for x in frame.index], "data": frame.to_dict("list")}


def decode_frame(value):
    return pd.DataFrame(value["data"], index=pd.to_datetime(value["index"], utc=True))
