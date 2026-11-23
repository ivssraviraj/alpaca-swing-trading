from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from .ai import validate_approval
from .calendar import entry_session, held_sessions
from .evidence import news_events
from .models import fingerprint
from .observation import capture_markouts, pending_rejections
from .risk import plan_order
from .signals import SECTOR_ETFS, regime, scan


def correlations(symbol, daily, held):
    output = {}
    if symbol not in daily:
        return output
    for other in held:
        if other not in daily:
            continue
        joined = (
            pd.concat(
                [
                    daily[symbol].close.pct_change().rename("a"),
                    daily[other].close.pct_change().rename("b"),
                ],
                axis=1,
            )
            .dropna()
            .tail(60)
        )
        if len(joined) >= 30:
            value = joined.a.corr(joined.b)
            if pd.notna(value):
                output[other] = float(value)
    return output


class Pipeline:
    def __init__(self, settings, store, broker, data, universe, evidence, ai, reporter, clock=None):
        self.cfg, self.store, self.broker, self.data = settings, store, broker, data
        self.universe, self.evidence, self.ai, self.reporter = universe, evidence, ai, reporter
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, mode):
        self.store.require_lock()
        now = self.clock()
        # Every mode reconciles first; execution mode also maintains exits, even
        # if AI/news/data subsequently fail or KILL_SWITCH blocks entries.
        try:
            portfolio = self.broker.manage(now)
        except Exception as exc:
            if mode == "paper-execute":
                self.reporter.notify(
                    "safety",
                    "PAPER reconciliation failed; inspect owned positions. " + type(exc).__name__,
                    "reconciliation-failure:" + now.date().isoformat(),
                )
            raise
        self.reporter.fills()
        self.reporter.sheet()
        if mode == "weekly-report":
            watched = sorted({c.symbol for c in pending_rejections(self.store, now)})
            if watched:
                capture_markouts(self.store, self.data.daily(watched, now), now)
            return self.reporter.weekly(now)
        if mode == "paper-execute" and (
            not portfolio.safe
            or self.cfg.kill_switch
            or not entry_session(now, self.cfg.entry_close_buffer_minutes)
        ):
            self.store.audit(
                "entry_freeze",
                {"reasons": portfolio.reasons, "kill_switch": self.cfg.kill_switch},
                now,
            )
            return {"status": "ENTRY_FREEZE", "scanned": 0}
        universe = self.universe.build(now)
        symbols = [r["symbol"] for r in universe]
        all_symbols = sorted(
            set(symbols + ["SPY", "QQQ", "IWM"] + list(SECTOR_ETFS) + list(portfolio.positions))
        )
        daily = self.data.daily(all_symbols, now)
        capture_markouts(self.store, daily, now)
        intraday = self.data.intraday(symbols, now)
        try:
            events, calendar_ok = self.evidence.calendar(now)
        except Exception as exc:
            events, calendar_ok = [], False
            self.store.audit("event_failure", {"error": type(exc).__name__}, now)
        try:
            macro_events, macro_ok = self.evidence.macro(now)
            events.extend(macro_events)
        except Exception as exc:
            macro_ok = False
            self.store.audit("macro_failure", {"error": type(exc).__name__}, now)
        discovery = sorted(
            {
                e.symbol
                for e in events
                if e.symbol in symbols and e.event_at <= now and held_sessions(e.event_at, now) <= 5
            }
        )
        for symbol in discovery[: self.cfg.event_discovery_limit]:
            try:
                events.extend(news_events(self.evidence.news(symbol, now)))
            except Exception as exc:
                self.store.audit(
                    "event_evidence_missing", {"symbol": symbol, "error": type(exc).__name__}, now
                )
        self.store.audit(
            "earnings_funnel",
            {
                "calendar_candidates": len(discovery),
                "news_attempted": min(len(discovery), self.cfg.event_discovery_limit),
                "verified_release_events": sum(e.kind == "earnings_release" for e in events),
                "discovery_cap": self.cfg.event_discovery_limit,
            },
            now,
        )
        market_regime = regime(daily, now, events, macro_ok)
        self.store.audit("regime", market_regime.model_dump(mode="json"), now)
        if mode == "paper-execute":
            portfolio = self.broker.manage(now, events, daily)
            if not portfolio.safe:
                return {"status": "ENTRY_FREEZE", "scanned": 0, "reasons": portfolio.reasons}
        candidates, scanned = [], 0
        identities = {r["symbol"]: r for r in universe}
        for row in universe:
            symbol = row["symbol"]
            if symbol not in intraday or symbol not in daily:
                continue
            scanned += 1
            scan_time = now
            if mode == "observe" and not intraday[symbol].empty:
                last_completed = intraday[symbol].index[-1].to_pydatetime() + timedelta(
                    minutes=5, seconds=30
                )
                if now - last_completed > timedelta(minutes=self.cfg.signal_ttl_minutes):
                    scan_time = last_completed
            found = scan(
                symbol,
                row["name"],
                intraday[symbol],
                daily[symbol],
                daily.get("SPY", pd.DataFrame()),
                events,
                market_regime if scan_time == now else regime(daily, scan_time),
                self.cfg,
                scan_time,
                row["sector"],
            )
            for c in found:
                self.store.audit("signal", c.model_dump(mode="json"), now)
            candidates.extend(found)
        # Earnings-calendar names are prioritized, but a date is not a verified release.
        recent_earnings = {
            e.symbol for e in events if e.event_at <= now and held_sessions(e.event_at, now) <= 5
        }
        candidates.sort(
            key=lambda c: (
                -(c.score + (2 if c.symbol in recent_earnings else 0)),
                c.symbol,
                c.setup,
            )
        )
        shortlist, seen = [], set()
        for c in candidates:
            if c.symbol not in seen:
                shortlist.append(c)
                seen.add(c.symbol)
            if len(shortlist) == self.cfg.shortlist_size:
                break
        submitted, conditional_count = 0, 0
        for c in shortlist:
            try:
                now = self.clock()
                if now >= c.expires_at:
                    raise ValueError("Historical observation only; technical signal expired")
                news = self.evidence.news(c.symbol, now)
                enriched = list({e.id: e for e in events + news_events(news)}.values())
                row = identities[c.symbol]
                choices = scan(
                    c.symbol,
                    c.company,
                    intraday[c.symbol],
                    daily[c.symbol],
                    daily.get("SPY", pd.DataFrame()),
                    enriched,
                    market_regime,
                    self.cfg,
                    now,
                    row["sector"],
                )
                if not choices:
                    raise ValueError("Signal no longer valid before AI")
                c = max(choices, key=lambda x: (x.score, x.setup.startswith("post_earnings")))
                approval = self.ai.evaluate(c, news, enriched, now)
                now = self.clock()
                # Retrieve again after AI latency: new news invalidates cached approval.
                latest_news = self.evidence.news(c.symbol, now)
                validate_approval(approval, c, latest_news, now, self.cfg, events=enriched)
                refreshed = self.data.intraday([c.symbol], now)
                if c.symbol not in refreshed:
                    raise ValueError("Missing final technical data")
                final = scan(
                    c.symbol,
                    c.company,
                    refreshed[c.symbol],
                    daily[c.symbol],
                    daily.get("SPY", pd.DataFrame()),
                    enriched,
                    market_regime,
                    self.cfg,
                    now,
                    row["sector"],
                )
                if not any(
                    fingerprint(x.model_dump(mode="json")) == approval.candidate_hash for x in final
                ):
                    raise ValueError("Signal changed after approval")
                portfolio = self.broker.reconcile(now)
                corr = correlations(c.symbol, daily, portfolio.positions)
                plan = plan_order(
                    c,
                    approval,
                    latest_news,
                    self.data.quote(c.symbol),
                    portfolio,
                    self.cfg,
                    now,
                    enriched,
                    calendar_ok,
                    corr,
                )
                self.store.audit(
                    "risk_approved",
                    {"signal_id": c.signal_id, "quantity": plan.quantity, "mode": mode},
                    now,
                )
                if mode == "paper-execute" and submitted < self.cfg.max_entries_per_scan:
                    result = self.broker.execute(
                        c,
                        approval,
                        latest_news,
                        self.data,
                        self.clock(),
                        enriched,
                        calendar_ok,
                        corr,
                    )
                    submitted += int(result is not None)
                elif conditional_count < self.cfg.max_entries_per_scan:
                    conditional_count += 1
                    self.store.audit(
                        "conditional_setup", {"signal_id": c.signal_id, "mode": mode}, now
                    )
            except Exception as exc:
                self.store.audit(
                    "candidate_rejected",
                    {
                        "symbol": c.symbol,
                        "signal_id": c.signal_id,
                        "candidate": c.model_dump(mode="json"),
                        "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                    },
                    self.clock(),
                )
        self.broker.manage(self.clock())
        self.reporter.fills()
        self.reporter.sheet()
        summary = {
            "status": mode,
            "universe_requested": self.cfg.universe_size,
            "universe_selected": len(symbols),
            "scanned": scanned,
            "signals": len(candidates),
            "shortlisted": len(shortlist),
            "submitted": submitted,
            "conditional_setups": conditional_count,
            "feed": self.cfg.data_feed,
            "event_calendar": calendar_ok,
        }
        self.store.audit("scan", summary, self.clock())
        return summary
