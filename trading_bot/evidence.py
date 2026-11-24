from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.requests import NewsRequest

from .calendar import NY, held_sessions
from .diagnostics import emit, traced
from .models import Event, News, fingerprint
from .providers import BoundedSession, read_call


def validate_news(news, symbol, as_of, max_age_hours, historical=False):
    if not news:
        raise ValueError("Missing news evidence")
    for n in news:
        if symbol not in n.symbols or not n.summary.strip():
            raise ValueError("Missing company-specific verified content")
        if not n.published_at <= n.updated_at <= n.retrieved_at:
            raise ValueError("Inconsistent news chronology")
        if n.updated_at > as_of or (as_of - n.published_at).total_seconds() > max_age_hours * 3600:
            raise ValueError("Future or stale news")
        if historical and n.retrieved_at > as_of:
            raise ValueError("Historical evidence was not archived point-in-time")


class EvidenceService:
    def __init__(self, client, settings, store):
        self.client, self.cfg, self.store = client, settings, store
        self.http = BoundedSession(
            settings.request_timeout_seconds, settings.request_interval_seconds
        )

    @traced("evidence.news")
    def news(self, symbol, now):
        emit("news_requested", symbol=symbol, as_of=now, max_age_hours=self.cfg.news_max_age_hours)
        response = read_call(
            self.client.get_news,
            NewsRequest(
                symbols=symbol,
                start=now - timedelta(hours=self.cfg.news_max_age_hours),
                end=now,
                include_content=True,
                exclude_contentless=True,
                limit=50,
            ),
        )
        result = []
        for item in response.data["news"]:
            if symbol not in item.symbols:
                continue
            summary = re.sub(r"<[^>]+>", " ", item.summary or item.content or "")[:1500]
            n = News(
                id=str(item.id),
                symbols=item.symbols,
                title=item.headline,
                source=item.source,
                url=item.url,
                published_at=item.created_at,
                updated_at=item.updated_at,
                retrieved_at=now,
                summary=summary,
            )
            result.append(n)
        result.sort(key=lambda n: (n.published_at, n.id), reverse=True)
        # Fail rather than quietly ignoring unknown future/invalid news.
        emit(
            "news_received",
            symbol=symbol,
            response_items=len(response.data["news"]),
            matching_items=len(result),
            newest_published_at=result[0].published_at if result else None,
        )
        validate_news(result, symbol, now, self.cfg.news_max_age_hours)
        result = result[:8]
        self.store.audit(
            "news", {"symbol": symbol, "items": [n.model_dump(mode="json") for n in result]}, now
        )
        return result

    @traced("evidence.earnings_calendar")
    def calendar(self, now):
        key = "earnings-calendar:" + now.astimezone(ZoneInfo(NY)).strftime("%Y-%m-%dT%H")
        cached = self.store.get(key)
        if cached is not None:
            emit("earnings_calendar_cache_hit", events=len(cached), as_of=now)
            return [Event.model_validate(e) for e in cached], True
        if not self.cfg.finnhub_api_key.get_secret_value():
            emit(
                "earnings_calendar_unavailable",
                "WARNING",
                reason="Finnhub credential not configured",
            )
            return [], False
        response = read_call(
            self.http.get,
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "from": (now - timedelta(days=10)).date().isoformat(),
                "to": (now + timedelta(days=30)).date().isoformat(),
            },
            headers={"X-Finnhub-Token": self.cfg.finnhub_api_key.get_secret_value()},
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw.get("earningsCalendar"), list):
            raise ValueError("Unavailable earnings calendar")
        events = []
        for e in raw["earningsCalendar"]:
            # Dates alone do not establish public release timestamps or point-in-time consensus.
            event_at = datetime.fromisoformat(e["date"]).replace(tzinfo=ZoneInfo(NY))
            events.append(
                Event(
                    id="finnhub-" + fingerprint([e["symbol"], e["date"]])[:20],
                    symbol=e["symbol"],
                    kind="earnings_calendar",
                    event_at=event_at,
                    known_at=now,
                    source="Finnhub calendar (current snapshot)",
                    url="https://finnhub.io/docs/api/earnings-calendar",
                    facts={
                        k: e.get(k)
                        for k in (
                            "epsActual",
                            "epsEstimate",
                            "revenueActual",
                            "revenueEstimate",
                            "hour",
                        )
                    },
                )
            )
        self.store.put(key, [e.model_dump(mode="json") for e in events])
        emit(
            "earnings_calendar_received",
            "INFO",
            events=len(events),
            lookback_days=10,
            forward_days=30,
            exact_release_times_verified=False,
        )
        self.store.audit(
            "calendar_snapshot",
            {
                "known_at": now.isoformat(),
                "valid_until": (now + timedelta(hours=1)).isoformat(),
                "source": "Finnhub earnings calendar",
            },
            now,
        )
        return events, True

    @traced("evidence.macro_calendar")
    def macro(self, now):
        if not self.cfg.macro_calendar_enabled or not self.cfg.finnhub_api_key.get_secret_value():
            emit(
                "macro_calendar_skipped",
                enabled=self.cfg.macro_calendar_enabled,
                configured=bool(self.cfg.finnhub_api_key.get_secret_value()),
            )
            return [], False
        key = "macro:" + now.date().isoformat()
        cached = self.store.get(key)
        if cached is not None:
            return [Event.model_validate(e) for e in cached], True
        response = read_call(
            self.http.get,
            "https://finnhub.io/api/v1/calendar/economic",
            params={
                "from": now.date().isoformat(),
                "to": (now + timedelta(days=7)).date().isoformat(),
            },
            headers={"X-Finnhub-Token": self.cfg.finnhub_api_key.get_secret_value()},
        )
        response.raise_for_status()
        rows = response.json().get("economicCalendar")
        if not isinstance(rows, list):
            raise ValueError("Unavailable economic calendar")
        result = []
        for row in rows:
            if row.get("country") not in {"US", "USA"}:
                continue
            raw = row["time"]
            when = (
                datetime.fromtimestamp(raw, timezone.utc)
                if isinstance(raw, (int, float))
                else datetime.fromisoformat(raw.replace("Z", "+00:00"))
            )
            if when.tzinfo is None:
                raise ValueError("Economic calendar timestamp lacks explicit timezone")
            result.append(
                Event(
                    id="macro-" + fingerprint([row.get("event"), raw])[:20],
                    symbol="MARKET",
                    kind="macro",
                    event_at=when,
                    known_at=now,
                    source="Finnhub economic calendar",
                    url="https://finnhub.io/docs/api/economic-calendar",
                    facts={
                        k: row.get(k)
                        for k in ("event", "impact", "actual", "estimate", "prev", "unit")
                    },
                )
            )
        self.store.put(key, [e.model_dump(mode="json") for e in result])
        return result, True


def news_events(news):
    events = []
    for n in news:
        # Discovery only: a verified release-style headline, never an inferred beat.
        title = n.title.lower()
        if re.search(r"\b(reports?|announces?)\b.*\b(results|earnings)\b", title):
            kind = "earnings_release"
        elif re.search(r"guidance|outlook", title):
            kind = "guidance"
        elif re.search(r"sec|10-k|10-q|8-k", title):
            kind = "filing"
        else:
            kind = "company_news"
        for symbol in n.symbols:
            events.append(
                Event(
                    id="news-" + n.id + "-" + symbol,
                    symbol=symbol,
                    kind=kind,
                    event_at=n.published_at,
                    known_at=max(n.published_at, n.updated_at),
                    source=n.source,
                    url=n.url,
                    facts={"headline": n.title},
                )
            )
    return events


def imminent_earnings(symbol, events, now, sessions):
    for event in events:
        if event.symbol != symbol or event.kind != "earnings_calendar":
            continue
        event_day = event.event_at.astimezone(ZoneInfo(NY)).date()
        today = now.astimezone(ZoneInfo(NY)).date()
        if event_day >= today and held_sessions(now, event.event_at) <= sessions:
            return True
    return False
