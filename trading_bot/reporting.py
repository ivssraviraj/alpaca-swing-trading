from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .providers import BoundedSession

SHEET_TITLE = "Alpaca Paper Fills"
SHEET_HEADER = [
    "event_key",
    "portfolio",
    "symbol",
    "side",
    "quantity",
    "fill_price",
    "fill_time_utc",
    "order_id",
    "setup",
    "realized_pnl",
    "ai_reason",
    "stop",
    "target",
]


class Reporter:
    def __init__(self, settings, store, http=None):
        self.cfg, self.store = settings, store
        self.http = http or BoundedSession(
            settings.request_timeout_seconds, settings.request_interval_seconds
        )

    def notify(self, kind, message, key):
        if kind not in {"fill", "weekly", "safety"}:
            return False
        if self.store.get("notified:" + key):
            return True
        url = self.cfg.alert_webhook_url.get_secret_value()
        if not url:
            return False
        if not url.startswith("https://"):
            raise ValueError("Notification webhook requires HTTPS")
        field = "text" if self.cfg.alert_webhook_kind == "slack" else "content"
        try:
            response = self.http.post(url, json={field: message[:1900]})
            response.raise_for_status()
            self.store.put("notified:" + key, True)
            return True
        except Exception as exc:
            self.store.audit("notification_failure", {"kind": kind, "error": type(exc).__name__})
            return False

    def fills(self):
        for event in self.store.events("fill"):
            f = event["payload"]
            local_time = (
                datetime.fromisoformat(f["fill_time"])
                .astimezone(ZoneInfo("America/New_York"))
                .strftime("%Y-%m-%d %H:%M:%S %Z")
            )
            message = (
                f"PAPER TRADE {f['side'].upper()} {f['symbol']} | {f['quantity']:g} shares "
                f"@ ${f['fill_price']:.4f} | {local_time} | {f['setup']} | "
                f"stop={f['stop']} target={f['target']} | realized P&L={f['realized_pnl']} | "
                f"AI: {f['ai_reason']} | broker={f['order_id']} | event={event['id']}"
            )
            self.notify("fill", message, event["id"])

    def sheet(self, book=None):
        if not self.cfg.sheets_enabled:
            return "DISABLED"
        try:
            import gspread

            if book is None:
                if (
                    not self.cfg.google_sheet_id
                    or not self.cfg.google_sa_json_base64.get_secret_value()
                ):
                    raise ValueError("Missing Sheets configuration")
                credentials = json.loads(
                    base64.b64decode(
                        self.cfg.google_sa_json_base64.get_secret_value(), validate=True
                    )
                )
                book = gspread.service_account_from_dict(credentials).open_by_key(
                    self.cfg.google_sheet_id
                )
            # Only the dedicated broker-fill tab is inspected or modified.
            try:
                tab = book.worksheet(SHEET_TITLE)
            except gspread.WorksheetNotFound:
                tab = book.add_worksheet(title=SHEET_TITLE, rows=1000, cols=len(SHEET_HEADER))
                tab.append_row(SHEET_HEADER, value_input_option="RAW")
            if tab.row_values(1) != SHEET_HEADER:
                raise ValueError("Dedicated Alpaca ledger schema mismatch; no writes")
            existing = set(tab.col_values(1))
            rows = []
            for event in self.store.events("fill"):
                if event["id"] in existing:
                    continue
                f = event["payload"]
                rows.append(
                    [
                        event["id"],
                        "ALPACA_PAPER",
                        f["symbol"],
                        f["side"],
                        f["quantity"],
                        f["fill_price"],
                        f["fill_time"],
                        f["order_id"],
                        f["setup"],
                        f["realized_pnl"],
                        f["ai_reason"],
                        f["stop"],
                        f["target"],
                    ]
                )
            if rows:
                tab.append_rows(rows, value_input_option="RAW")
            self.store.audit("sheets_sync", {"rows": len(rows), "status": "SUCCESS"})
            return "SUCCESS"
        except Exception as exc:
            self.store.audit("sheets_failure", {"error": type(exc).__name__})
            return "FAILED"

    def weekly(self, now):
        all_events = self.store.events()
        recent = [e for e in all_events if e["at"] >= (now - timedelta(days=7)).isoformat()]
        fills = [e["payload"] for e in recent if e["kind"] == "fill"]
        sells = [f for f in fills if f["side"] == "sell"]
        snapshots = [e["payload"] for e in all_events if e["kind"] == "portfolio"]
        decisions = [e["payload"] for e in recent if e["kind"] == "ai_decision"]
        markouts = [e["payload"] for e in recent if e["kind"] == "forward_markout"]
        peak, drawdown = 0.0, 0.0
        for p in snapshots:
            marked = p.get("strategy_marked_equity") or p["equity"]
            peak = max(peak, marked)
            drawdown = max(drawdown, (peak - marked) / peak if peak else 0)
        cycles, closed = {}, []
        all_fills = sorted(
            [e["payload"] for e in all_events if e["kind"] == "fill"],
            key=lambda f: (f["fill_time"], f["side"] != "buy"),
        )
        for f in all_fills:
            cycle = cycles.setdefault(f["symbol"], {"qty": 0.0, "pnl": 0.0, "setup": f["setup"]})
            cycle["qty"] += f["quantity"] * (1 if f["side"] == "buy" else -1)
            cycle["pnl"] += f.get("realized_pnl") or 0
            if f["side"] == "sell" and abs(cycle["qty"]) < 1e-6:
                if datetime.fromisoformat(f["fill_time"]) >= now - timedelta(days=7):
                    closed.append(
                        {"symbol": f["symbol"], "setup": cycle["setup"], "pnl": cycle["pnl"]}
                    )
                del cycles[f["symbol"]]
        by_setup = {}
        for f in sells:
            by_setup[f["setup"]] = by_setup.get(f["setup"], 0) + (f["realized_pnl"] or 0)
        report = {
            "period_end": now.isoformat(),
            "portfolio": "ALPACA_PAPER_ONLY",
            "latest_portfolio": snapshots[-1] if snapshots else None,
            "realized_pnl_week": sum(f["realized_pnl"] or 0 for f in sells),
            "new_positions": sorted({f["symbol"] for f in fills if f["side"] == "buy"}),
            "sell_fills": len(sells),
            "closed_positions": closed,
            "closed_trade_win_rate": sum(c["pnl"] > 0 for c in closed) / len(closed)
            if closed
            else None,
            "performance_by_setup": by_setup,
            "winning_sell_fill_fraction": sum((f["realized_pnl"] or 0) > 0 for f in sells)
            / len(sells)
            if sells
            else None,
            "drawdown_snapshot_estimate": drawdown,
            "ai_decisions": dict(Counter(d["decision"]["decision"] for d in decisions)),
            "estimated_api_cost": sum(d.get("estimated_usd") or 0 for d in decisions),
            "funnel": dict(Counter(e["kind"] for e in recent)),
            "incidents": [e for e in recent if "failure" in e["kind"]],
            "missed_opportunities": {
                "forward_price_markouts": markouts,
                "positive_markout_count": sum(m["gross_price_return"] > 0 for m in markouts),
                "caveat": "Rejected-candidate 5/15-session price changes, not realizable trades or foregone P&L; empty means not yet observed",
            },
            "next_improvements": [
                "Collect forward point-in-time evidence",
                "Validate execution recovery against paper service",
            ],
            "caveat": "Fill-based metrics; partial fills are not independent closed trades. Snapshot drawdown can miss intraday losses.",
        }
        self.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.reports_dir / "weekly_report.json").write_text(json.dumps(report, indent=2))
        local = now.astimezone(ZoneInfo("America/New_York"))
        if local.weekday() == 5:
            self.notify(
                "weekly",
                "PAPER WEEKLY REPORT " + json.dumps(report, default=str),
                "weekly:" + local.date().isoformat(),
            )
        return report
