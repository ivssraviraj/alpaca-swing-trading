"""Durable journal and whole-run single-writer lock. No secrets stored here."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import fingerprint


class Store:
    def __init__(self, path: Path, database_url: str = ""):
        self.path = path
        self.postgres = bool(database_url)
        self.locked = False
        if database_url:
            import psycopg

            self.db = psycopg.connect(database_url, autocommit=True, connect_timeout=10)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        for statement in (
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL)",
        ):
            self.execute(statement)

    def execute(self, sql, params=()):
        return self.db.execute(sql.replace("?", "%s") if self.postgres else sql, params)

    @contextmanager
    def writer(self):
        import fcntl

        handle = None
        acquired = False
        try:
            if self.postgres:
                acquired = self.execute("SELECT pg_try_advisory_lock(714209331)").fetchone()[0]
            else:
                handle = self.path.with_suffix(".lock").open("a")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            if not acquired:
                raise RuntimeError("Another trading invocation holds the writer lock")
            self.locked = True
            yield self
        finally:
            self.locked = False
            if acquired and self.postgres:
                self.execute("SELECT pg_advisory_unlock(714209331)")
            if handle:
                handle.close()

    def require_lock(self):
        if not self.locked:
            raise RuntimeError("Durable writer lock required")
        if self.postgres:
            # A lost PostgreSQL session also loses its advisory lock. Never reconnect
            # and continue execution under a stale lock flag.
            self.execute("SELECT 1")

    def get(self, key, default=None):
        row = self.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.require_lock()
        self.execute(
            "INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=str, sort_keys=True)),
        )

    def audit(self, kind, payload, at=None, key=None):
        self.require_lock()
        identifier = key or str(uuid.uuid4())
        self.execute(
            "INSERT INTO audit VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (
                identifier,
                (at or datetime.now(timezone.utc)).isoformat(),
                kind,
                json.dumps(payload, default=str, sort_keys=True),
            ),
        )
        return identifier

    def events(self, kind=None):
        rows = self.execute(
            "SELECT id,at,kind,payload FROM audit"
            + (" WHERE kind=?" if kind else "")
            + " ORDER BY at,id",
            (kind,) if kind else (),
        ).fetchall()
        return [dict(id=r[0], at=r[1], kind=r[2], payload=json.loads(r[3])) for r in rows]

    def intent(self, identifier, symbol, payload):
        self.require_lock()
        result = self.execute(
            "INSERT INTO intents VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (identifier, symbol, json.dumps(payload, default=str), "RESERVED"),
        )
        return result.rowcount == 1

    def intents(self):
        return [
            dict(id=r[0], symbol=r[1], payload=json.loads(r[2]), state=r[3])
            for r in self.execute("SELECT * FROM intents ORDER BY id").fetchall()
        ]

    def mark_intent(self, identifier, state):
        self.require_lock()
        self.execute("UPDATE intents SET state=? WHERE id=?", (state, identifier))

    def reserve_ai(self, now, amount, max_cost, max_calls, interval):
        self.require_lock()
        key = "budget:" + now.date().isoformat()
        value = self.get(key, {"reserved_usd": 0.0, "calls": 0})
        last = self.get("ai:last_call")
        if last and (now - datetime.fromisoformat(last)).total_seconds() < interval:
            return False
        if value["calls"] >= max_calls or value["reserved_usd"] + amount > max_cost:
            return False
        # Pessimistic reservation is deliberately never refunded after timeout.
        value["calls"] += 1
        value["reserved_usd"] += amount
        self.put(key, value)
        self.put("ai:last_call", now.isoformat())
        return True

    def export(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "audit.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.events()))
        records = self.events("research_record")
        archive = {
            "provenance": "Forward runtime records; no generated historical approvals",
            "approvals": {},
            "events": [],
            "identities": {},
            "calendar_snapshots": [],
        }
        event_map = {}
        from .models import Candidate

        for row in records:
            record = row["payload"]
            candidate = Candidate.model_validate(record["candidate"])
            archive["approvals"][candidate.signal_id] = record
            archive["identities"][candidate.symbol] = {
                "name": candidate.company,
                "sector": candidate.sector,
            }
            event_map.update({e["id"] + e["known_at"]: e for e in record["events"]})
        archive["events"] = list(event_map.values())
        archive["calendar_snapshots"] = [e["payload"] for e in self.events("calendar_snapshot")]
        (directory / "forward_archive.json").write_text(json.dumps(archive, indent=2))
        return fingerprint(self.events())

    def close(self):
        self.db.close()
