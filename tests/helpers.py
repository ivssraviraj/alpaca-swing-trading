from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pandas as pd

from trading_bot.ai import AIService
from trading_bot.config import PAPER_URL, Settings
from trading_bot.execution import Broker, active, flatten
from trading_bot.models import Decision, News, Quote
from trading_bot.pipeline import Pipeline
from trading_bot.reporting import Reporter
from trading_bot.signals import regime, scan
from trading_bot.state import Store

NOW = datetime(2026, 11, 10, 19, 35, 30, tzinfo=timezone.utc)


def frames(count=61, outcome=None):
    dates = pd.date_range("2026-08-24", periods=55, freq="B", tz="America/New_York")
    daily = pd.DataFrame(
        {
            "open": [100 + i * 0.5 for i in range(55)],
            "close": [100 + i * 0.5 for i in range(55)],
            "high": [102 + i * 0.5 for i in range(55)],
            "low": [98 + i * 0.5 for i in range(55)],
            "volume": [1000000] * 55,
        },
        index=dates,
    )
    index = pd.date_range("2026-11-10 09:30", periods=count, freq="5min", tz="America/New_York")
    prices = [127.0] * count
    for i in range(60, count):
        prices[i] = 131.0
    if outcome and count >= 64:
        prices[-1] = 145.0 if outcome == "win" else 120.0
    bars = pd.DataFrame(
        {
            "open": prices,
            "close": prices,
            "high": [p + 0.1 for p in prices],
            "low": [p - 0.1 for p in prices],
            "volume": [10000] * count,
        },
        index=index,
    )
    bars.iloc[60, bars.columns.get_loc("volume")] = 30000
    return bars, daily


def cfg(tmp_path, **overrides):
    values = dict(
        _env_file=None,
        github_actions=False,
        database_url="",
        state_path=tmp_path / "state.sqlite3",
        reports_dir=tmp_path / "reports",
        apca_api_key_id="fixture-key",
        apca_api_secret_key="fixture-secret",
        ai_enabled=True,
        paper_orders_enabled=True,
        paper_only_ack="YES_PAPER_ONLY",
        execution_validated_ack="PAPER_CHECKS_REVIEWED",
        iex_execution_ack="I_ACCEPT_IEX_LIMITATIONS",
        ai_min_interval_seconds=0,
        request_interval_seconds=0,
    )
    values.update(overrides)
    return Settings(**values)


def news(now=NOW):
    return [
        News(
            id="fixture-news",
            symbols=["AAPL"],
            title="Company reports quarterly results",
            source="Synthetic test publisher",
            url="https://example.com/fixture-release",
            published_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1),
            retrieved_at=now,
            summary="SYNTHETIC TEST DATA: verified fixture company release; no investment facts.",
        )
    ]


def candidate(settings, now=NOW):
    bars, daily = frames()
    market_regime = regime({s: daily for s in ("SPY", "QQQ", "IWM")}, now)
    return scan("AAPL", "Fixture Company", bars, daily, daily, [], market_regime, settings, now)[0]


def decision(c, items, verdict="APPROVE"):
    return Decision(
        symbol=c.symbol,
        decision=verdict,
        confidence=0.8,
        catalyst_type="earnings",
        bullish_factors=["Synthetic positive fixture"],
        bearish_factors=[],
        critical_risks=[],
        missing_information=[],
        evidence=[n.model_dump(include={"title", "source", "url", "published_at"}) for n in items],
        reasoning_summary="Synthetic evidence confirms fixture thesis",
        invalidation_conditions=["Break of technical stop"],
        approval_expires_at=c.expires_at,
    )


class FakeAI:
    def __init__(self, verdict="APPROVE", fail=None, mutate=None):
        self.responses = self
        self.calls, self.verdict, self.fail, self.mutate = 0, verdict, fail, mutate

    def parse(self, **kwargs):
        import json

        from trading_bot.models import Candidate

        self.calls += 1
        if self.fail:
            raise self.fail
        package = json.loads(kwargs["input"][-1]["content"])
        c = Candidate.model_validate(package["candidate"])
        d = decision(c, [News.model_validate(n) for n in package["news"]], self.verdict)
        if self.mutate:
            d = self.mutate(d)
        return NS(status="completed", output_parsed=d, usage=None)


class FakeEvidence:
    def __init__(self, fail=None):
        self.fail = fail
        self.items = news()
        self.calls = 0
        self.changed = False

    def news(self, symbol, now):
        self.calls += 1
        if self.fail:
            raise self.fail
        if self.changed and self.calls > 1:
            return [self.items[0].model_copy(update={"summary": "New material contradiction"})]
        return self.items

    def calendar(self, now):
        return [], True

    def macro(self, now):
        return [], False


class FakeMarket:
    def __init__(self):
        self.bars, self.day = frames()
        self.missing = False
        self.changed = False
        self.calls = 0
        self.latest_quote = Quote(symbol="AAPL", bid=131, ask=131.02, at=NOW, feed="iex")

    def daily(self, symbols, now):
        return {s: self.day for s in symbols}

    def intraday(self, symbols, now):
        self.calls += 1
        if self.missing:
            return {}
        bars = self.bars.copy()
        if self.changed and self.calls > 1:
            bars.iloc[-1, bars.columns.get_loc("close")] = 128
        return {s: bars for s in symbols}

    def quote(self, symbol):
        return self.latest_quote


def order(identifier, symbol="AAPL", side="buy", status="filled", qty=10, price=131, **kwargs):
    data = dict(
        id=identifier,
        client_order_id=identifier,
        symbol=symbol,
        side=side,
        status=status,
        qty=str(qty),
        filled_qty=str(qty if status == "filled" else 0),
        filled_avg_price=str(price) if status == "filled" else None,
        limit_price=str(price),
        stop_price=None,
        type="limit",
        legs=[],
        filled_at=NOW if status == "filled" else None,
        updated_at=NOW,
        created_at=NOW,
        submitted_at=NOW,
    )
    data.update(kwargs)
    return NS(**data)


class NotFound(Exception):
    status_code = 404


class FakeTrading:
    _base_url = PAPER_URL

    def __init__(self):
        self.account = NS(
            status="active",
            trading_blocked=False,
            account_blocked=False,
            equity="25000",
            cash="25000",
            buying_power="25000",
        )
        self.current_positions, self.roots, self.submissions, self.cancellations = [], [], [], []
        self.open = True
        self.timeout = False
        self.fail_submission = False
        self.fill_ratio = 1.0
        self.pending_cancel = False

    def get_account(self):
        return self.account

    def get_clock(self):
        return NS(
            is_open=self.open, timestamp=NOW, next_close=NOW.replace(hour=21, minute=0, second=0)
        )

    def get_all_positions(self):
        return self.current_positions

    def get_orders(self, filter=None):
        if filter and str(filter.status.value) == "open":
            return [o for o in self.roots if active(o) or any(active(x) for x in o.legs)]
        return self.roots

    def get_order_by_client_id(self, identifier):
        return (
            next((o for o in flatten(self.roots) if o.client_order_id == identifier), None)
            or self._missing()
        )

    def _missing(self):
        raise NotFound()

    def get_order_by_id(self, identifier):
        return next(o for o in flatten(self.roots) if o.id == identifier)

    def submit_order(self, order_data):
        self.submissions.append(order_data)
        if self.fail_submission:
            raise TimeoutError("ambiguous")
        qty = float(order_data.qty)
        if str(order_data.side.value) == "buy":
            fill = qty * self.fill_ratio
            price = 131.0
            parent = order(
                order_data.client_order_id,
                qty=qty,
                price=price,
                status="filled" if self.fill_ratio == 1 else "partially_filled",
                filled_qty=str(fill),
                filled_avg_price=str(price),
                filled_at=NOW,
            )
            stop = order(
                parent.id + "-stop",
                side="sell",
                qty=qty,
                status="new" if self.fill_ratio == 1 else "held",
                type="stop",
                stop_price=str(order_data.stop_loss.stop_price),
            )
            target = order(
                parent.id + "-target",
                side="sell",
                qty=qty,
                status="new" if self.fill_ratio == 1 else "held",
                price=order_data.take_profit.limit_price,
            )
            parent.legs = [stop, target]
            self.current_positions = [
                NS(
                    symbol="AAPL",
                    qty=str(fill),
                    side="long",
                    avg_entry_price="131",
                    current_price="131",
                    unrealized_pl="0",
                )
            ]
            self.account.cash = str(25000 - fill * price)
        elif getattr(order_data.order_class, "value", None) == "oco":
            parent = order(
                order_data.client_order_id,
                side="sell",
                qty=qty,
                status="new",
                price=order_data.take_profit.limit_price,
            )
            parent.legs = [
                order(
                    parent.id + "-stop",
                    side="sell",
                    qty=qty,
                    status="new",
                    type="stop",
                    stop_price=str(order_data.stop_loss.stop_price),
                )
            ]
        else:
            parent = order(order_data.client_order_id, side="sell", qty=qty)
            self.current_positions = []
        self.roots.append(parent)
        if self.timeout:
            raise TimeoutError("accepted before timeout")
        return parent

    def cancel_order_by_id(self, identifier):
        self.cancellations.append(identifier)
        o = self.get_order_by_id(identifier)
        o.status = "pending_cancel" if self.pending_cancel else "canceled"
        for parent in self.roots:
            if o in parent.legs:
                for sibling in parent.legs:
                    sibling.status = o.status

    def exit(self, outcome="win"):
        parent = self.roots[0]
        leg = parent.legs[1 if outcome == "win" else 0]
        leg.status = "filled"
        leg.filled_qty = parent.filled_qty
        leg.filled_avg_price = leg.limit_price if outcome == "win" else leg.stop_price
        leg.filled_at = NOW + timedelta(minutes=5)
        parent.legs[0 if outcome == "win" else 1].status = "canceled"
        self.current_positions = []


def environment(tmp_path, mode="paper-execute", **overrides):
    settings = cfg(tmp_path, **overrides)
    store = Store(settings.state_path)
    trading, market, evidence, ai_client = FakeTrading(), FakeMarket(), FakeEvidence(), FakeAI()
    reporter = Reporter(settings, store)
    broker = Broker(trading, settings, store, mode, reporter.notify)
    universe = NS(
        build=lambda now: [{"symbol": "AAPL", "name": "Fixture Company", "sector": "UNKNOWN"}]
    )
    ai = AIService(settings, store, ai_client)
    pipeline = Pipeline(
        settings, store, broker, market, universe, evidence, ai, reporter, clock=lambda: NOW
    )
    return NS(
        cfg=settings,
        store=store,
        trading=trading,
        market=market,
        evidence=evidence,
        ai_client=ai_client,
        reporter=reporter,
        broker=broker,
        ai=ai,
        pipeline=pipeline,
    )
