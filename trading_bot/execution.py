"""Paper-only broker adapter. Broker reads are authoritative; no submission retries."""

from __future__ import annotations

from datetime import datetime, timezone

from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from .calendar import held_sessions
from .config import PAPER_URL
from .diagnostics import emit, error_details, traced
from .models import Approval, Candidate, Event, News, Portfolio, fingerprint
from .providers import read_call
from .risk import plan_order
from .universe import text

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
PROTECTIVE = {"new", "accepted", "partially_filled"}


def active(order):
    return text(order.status) not in TERMINAL


def flatten(orders):
    found = {}

    def visit(order):
        found[str(order.id)] = order
        for leg in getattr(order, "legs", None) or []:
            visit(leg)

    for order in orders:
        visit(order)
    return list(found.values())


class Broker:
    def __init__(self, client, settings, store, mode, notify):
        self.client, self.cfg, self.store, self.mode, self.notify = (
            client,
            settings,
            store,
            mode,
            notify,
        )
        if str(getattr(client, "_base_url", "")) != PAPER_URL:
            raise ValueError("Wrong trading endpoint: PAPER required")

    @traced("broker.mutation_gate")
    def mutation_gate(self):
        emit(
            "mutation_gate",
            mode=self.mode,
            paper_orders_enabled=self.cfg.paper_orders_enabled,
            kill_switch=self.cfg.kill_switch,
            writer_locked=self.store.locked,
        )
        self.cfg.execution_gate(self.mode)
        self.store.require_lock()
        if str(self.client._base_url) != PAPER_URL:
            raise ValueError("Wrong trading endpoint")

    def by_client_id(self, identifier):
        try:
            return read_call(self.client.get_order_by_client_id, identifier)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise

    @traced("broker.cancel_order")
    def cancel_order(self, identifier):
        emit("paper_order_cancellation", "INFO", order_id=str(identifier))
        return self.client.cancel_order_by_id(identifier)

    @traced("broker.order_history")
    def orders(self):
        from alpaca.common.enums import Sort

        recent, until = [], None
        for _ in range(50):
            page = read_call(
                self.client.get_orders,
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.ALL,
                    limit=500,
                    nested=True,
                    until=until,
                    direction=Sort.DESC,
                ),
            )
            recent.extend(page)
            emit("order_history_page", page_number=_ + 1, returned=len(page), until=until)
            if len(page) < 500:
                break
            oldest = min(o.submitted_at for o in page)
            if until is not None and oldest >= until:
                raise ValueError("Order pagination cannot advance across timestamp ties")
            until = oldest
        else:
            raise ValueError("Order history exceeds bounded recovery window")
        opened = read_call(
            self.client.get_orders,
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True),
        )
        if len(opened) >= 500:
            raise ValueError("Open order page saturated; refuse ambiguous exposure")
        known = []
        for intent in self.store.intents():
            if intent["state"] not in TERMINAL:
                order = self.by_client_id(intent["id"])
                if order is not None:
                    known.append(order)
        return flatten([*recent, *opened, *known])

    @traced("broker.reconcile")
    def reconcile(self, now):
        self.store.require_lock()
        account = read_call(self.client.get_account)
        positions = read_call(self.client.get_all_positions)
        orders = self.orders()
        by_client = {str(o.client_order_id): o for o in orders}
        ownership, candidates, owned_ids = {}, {}, set()
        reasons = []
        intents = self.store.intents()
        for intent in intents:
            order = by_client.get(intent["id"])
            if order is None:
                if intent["state"] not in TERMINAL:
                    reasons.append("Unresolved submission " + intent["id"])
                continue
            self.store.mark_intent(intent["id"], text(order.status))
            group = flatten([order])
            owned_ids.update(str(o.id) for o in group)
            payload = intent["payload"]
            c = Candidate.model_validate(payload["candidate"])
            previous_candidate = candidates.get(intent["symbol"])
            if previous_candidate is None or previous_candidate.signal_at <= c.signal_at:
                candidates[intent["symbol"]] = c
            for o in group:
                ownership[str(o.id)] = (c, payload)
        # Cumulative fill deltas are processed BUY before SELL to preserve cost basis.
        owned = [o for o in orders if str(o.id) in owned_ids]
        for order in sorted(owned, key=lambda o: (text(o.side) != "buy", str(o.id))):
            c, payload = ownership[str(order.id)]
            self.record_fill(order, c, payload, now)
        owned_quantity = {}
        for o in owned:
            owned_quantity[o.symbol] = owned_quantity.get(o.symbol, 0) + (
                1 if text(o.side) == "buy" else -1
            ) * float(o.filled_qty or 0)
        # Include old closed orders from durable fill checkpoints, not truncated history.
        ledger_qty = {}
        for e in self.store.events("fill"):
            f = e["payload"]
            ledger_qty[f["symbol"]] = (
                ledger_qty.get(f["symbol"], 0) + (1 if f["side"] == "buy" else -1) * f["quantity"]
            )
        exposure, risk, sectors, quantities = 0.0, 0.0, {}, {}
        self.positions, self.last_orders, self.candidates = positions, orders, candidates
        self.owned_ids, self.ledger_qty = owned_ids, ledger_qty
        for pos in positions:
            qty = float(pos.qty)
            c = candidates.get(pos.symbol)
            if text(pos.side) != "long" or not c or abs(ledger_qty.get(pos.symbol, 0) - qty) > 1e-6:
                reasons.append("Unowned or mixed/manual position " + pos.symbol)
                continue
            quantities[pos.symbol] = qty
            exposure += qty * max(float(pos.current_price), float(pos.avg_entry_price))
            sectors[c.sector] = sectors.get(c.sector, 0) + 1
            stops = [
                o
                for o in owned
                if o.symbol == pos.symbol
                and text(o.side) == "sell"
                and text(o.type) in {"stop", "stop_limit"}
                and text(o.status) in PROTECTIVE
            ]
            covered = sum(max(0.0, float(o.qty) - float(o.filled_qty or 0)) for o in stops)
            targets = [
                o
                for o in owned
                if o.symbol == pos.symbol
                and text(o.side) == "sell"
                and text(o.type) == "limit"
                and text(o.status) in PROTECTIVE
            ]
            target_covered = sum(max(0.0, float(o.qty) - float(o.filled_qty or 0)) for o in targets)
            if abs(covered - qty) > 1e-6 or abs(target_covered - qty) > 1e-6:
                reasons.append("Missing/mismatched protective exit " + pos.symbol)
            stop = min((float(o.stop_price) for o in stops), default=c.stop)
            risk += qty * (
                max(0.0, float(pos.current_price) - stop) + float(pos.current_price) * 0.005
            )
        actual_symbols = {p.symbol for p in positions}
        for symbol, qty in ledger_qty.items():
            if qty > 1e-6 and symbol not in actual_symbols:
                reasons.append("Broker/ledger position mismatch " + symbol)
        pending = []
        for o in orders:
            if not active(o):
                continue
            if str(o.id) not in owned_ids:
                reasons.append("Unowned open order " + o.symbol)
            if text(o.side) == "buy":
                pending.append(o.symbol)
                unfilled = max(0.0, float(o.qty) - float(o.filled_qty or 0))
                if not o.limit_price:
                    reasons.append("Unbounded pending BUY " + o.symbol)
                else:
                    price = float(o.limit_price)
                    exposure += unfilled * price
                    c = candidates.get(o.symbol)
                    risk += unfilled * (price - c.stop + price * 0.005) if c else unfilled * price
        if text(account.status) != "active" or account.trading_blocked or account.account_blocked:
            reasons.append("Account blocked/inactive")
        realized = sum(e["payload"].get("realized_pnl", 0) or 0 for e in self.store.events("fill"))
        unrealized = sum(float(p.unrealized_pl) for p in positions if p.symbol in quantities)
        equity = min(
            float(account.equity),
            self.cfg.strategy_capital_usd,
            self.cfg.strategy_capital_usd + realized + unrealized,
        )
        snapshot = Portfolio(
            equity=equity,
            broker_equity=float(account.equity),
            strategy_marked_equity=self.cfg.strategy_capital_usd + realized + unrealized,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            cash=float(account.cash),
            buying_power=float(account.buying_power),
            exposure=exposure,
            stop_risk=risk,
            positions=quantities,
            pending_symbols=sorted(set(pending)),
            sector_counts=sectors,
            safe=not reasons,
            reasons=sorted(set(reasons)),
        )
        self.store.audit("portfolio", snapshot.model_dump(mode="json"), now)
        return snapshot

    @traced("broker.record_fill")
    def record_fill(self, order, candidate, payload, now):
        qty, avg = float(order.filled_qty or 0), float(order.filled_avg_price or 0)
        if qty <= 0:
            return
        if avg <= 0:
            raise ValueError("Confirmed fill has invalid price")
        previous = self.store.get("fill:" + str(order.id), {"qty": 0.0, "notional": 0.0})
        if qty < previous["qty"]:
            raise ValueError("Broker fill quantity regressed")
        if qty == previous["qty"]:
            return
        delta = qty - previous["qty"]
        price = (qty * avg - previous["notional"]) / delta
        side = text(order.side)
        basis_key = "basis:" + candidate.symbol
        basis = self.store.get(basis_key, {"qty": 0.0, "cost": 0.0})
        pnl = None
        if side == "buy":
            basis["qty"] += delta
            basis["cost"] += delta * price
        else:
            if basis["qty"] + 1e-6 < delta:
                raise ValueError("Exit fill exceeds owned cost basis")
            unit_cost = basis["cost"] / basis["qty"]
            pnl = delta * (price - unit_cost)
            basis["qty"] -= delta
            basis["cost"] -= delta * unit_cost
        fill_time = getattr(order, "filled_at", None) or getattr(order, "updated_at", None)
        if fill_time is None:
            raise ValueError("Fill timestamp unavailable")
        event = dict(
            label="ALPACA PAPER FILL",
            symbol=order.symbol,
            side=side,
            quantity=delta,
            fill_price=price,
            fill_time=fill_time.isoformat(),
            captured_at=now.isoformat(),
            order_id=str(order.id),
            setup=candidate.setup,
            stop=candidate.stop,
            target=candidate.target,
            realized_pnl=pnl,
            ai_reason=payload.get("ai_reason", "protective exit"),
        )
        # Transaction makes fill checkpoint, cost basis and audit atomic across crashes.
        self.store.execute("BEGIN")
        try:
            self.store.audit("fill", event, now, key=f"fill:{order.id}:{qty}")
            self.store.put("fill:" + str(order.id), {"qty": qty, "notional": qty * avg})
            self.store.put(basis_key, basis)
            self.store.execute("COMMIT")
        except Exception:
            self.store.execute("ROLLBACK")
            raise

    @traced("broker.execute")
    def execute(self, candidate, approval, news, market, now, events, calendar_ok, correlations):
        emit(
            "execution_requested",
            "INFO",
            symbol=candidate.symbol,
            signal_id=candidate.signal_id,
            setup=candidate.setup,
        )
        self.mutation_gate()
        if self.cfg.data_feed == "iex" and self.cfg.iex_execution_ack != "I_ACCEPT_IEX_LIMITATIONS":
            raise ValueError("IEX execution limitations must be explicitly acknowledged")
        current = self.reconcile(now)
        clock = read_call(self.client.get_clock)
        emit(
            "broker_clock",
            open=clock.is_open,
            timestamp=clock.timestamp,
            next_close=clock.next_close,
            as_of=now,
        )
        if (
            not clock.is_open
            or abs((clock.timestamp - now).total_seconds()) > 90
            or (clock.next_close - now).total_seconds() < self.cfg.entry_close_buffer_minutes * 60
        ):
            raise ValueError("Broker clock closed/stale")
        quote = market.quote(candidate.symbol)
        plan = plan_order(
            candidate,
            approval,
            news,
            quote,
            current,
            self.cfg,
            now,
            events,
            calendar_ok,
            correlations,
        )
        payload = {
            "candidate": candidate.model_dump(mode="json"),
            "kind": "entry",
            "ai_reason": approval.decision.reasoning_summary,
            "quantity": plan.quantity,
            "approval": approval.model_dump(mode="json"),
            "news": [n.model_dump(mode="json") for n in news],
            "events": [e.model_dump(mode="json") for e in events],
        }
        if self.by_client_id(candidate.signal_id) is not None:
            emit(
                "entry_duplicate_skipped",
                "INFO",
                symbol=candidate.symbol,
                signal_id=candidate.signal_id,
                source="broker",
            )
            return None
        if not self.store.intent(candidate.signal_id, candidate.symbol, payload):
            emit(
                "entry_duplicate_skipped",
                "INFO",
                symbol=candidate.symbol,
                signal_id=candidate.signal_id,
                source="durable_intent",
            )
            return None
        request = LimitOrderRequest(
            symbol=candidate.symbol,
            qty=plan.quantity,
            limit_price=plan.limit_price,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            client_order_id=candidate.signal_id,
            extended_hours=False,
            stop_loss=StopLossRequest(stop_price=candidate.stop),
            take_profit=TakeProfitRequest(limit_price=candidate.target),
        )
        # GTC brackets preserve protection overnight; entry expiry is explicitly
        # canceled on each reconciliation when its signal expires.
        result = self.submit_once(request, now)
        # Do not leave a resting entry working until the next best-effort cron.
        # A cancellation failure is ambiguous and freezes subsequent risk.
        if result is not None and active(result):
            latest = self.by_client_id(candidate.signal_id)
            if latest is None:
                raise ValueError("Entry acknowledgement disappeared")
            if active(latest):
                try:
                    self.cancel_order(latest.id)
                except Exception:
                    self.notify(
                        "safety",
                        "PAPER entry cancellation uncertain: " + candidate.symbol,
                        "cancel:" + candidate.signal_id,
                    )
                    raise ValueError("Entry cancellation ambiguous") from None
        self.manage(
            datetime.now(timezone.utc)
            if abs((datetime.now(timezone.utc) - now).total_seconds()) < 120
            else now
        )
        return result

    @traced("broker.submit_once")
    def submit_once(self, request, now):
        self.mutation_gate()
        intent = next((i for i in self.store.intents() if i["id"] == request.client_order_id), None)
        if intent is None or intent["state"] != "RESERVED" or request.symbol != intent["symbol"]:
            raise ValueError("A fresh durable intent is required for submission")
        if text(request.side) == "buy":
            from .ai import validate_approval

            payload = intent["payload"]
            if self.cfg.kill_switch or payload.get("kind") != "entry":
                raise ValueError("New entry is frozen")
            c = Candidate.model_validate(payload["candidate"])
            validate_approval(
                Approval.model_validate(payload["approval"]),
                c,
                [News.model_validate(n) for n in payload["news"]],
                now,
                self.cfg,
                events=[Event.model_validate(e) for e in payload.get("events", [])],
            )
            if (
                float(request.qty) > payload["quantity"]
                or request.client_order_id != c.signal_id
                or text(request.order_class) != "bracket"
                or text(request.type) != "limit"
                or float(request.limit_price) != c.entry_high
                or request.stop_loss.stop_price != c.stop
                or request.take_profit.limit_price != c.target
            ):
                raise ValueError("Order differs from deterministic approved plan")
        try:
            emit(
                "paper_order_submission",
                "INFO",
                symbol=request.symbol,
                client_order_id=request.client_order_id,
                side=text(request.side),
                quantity=float(request.qty),
                order_type=text(request.type),
            )
            result = self.client.submit_order(order_data=request)
            self.store.mark_intent(request.client_order_id, text(result.status))
            self.store.audit(
                "order_ack",
                {
                    "client_order_id": request.client_order_id,
                    "order_id": str(result.id),
                    "status": text(result.status),
                },
                now,
            )
            return result
        except Exception as exc:
            emit(
                "paper_submission_ambiguous",
                "ERROR",
                symbol=request.symbol,
                client_order_id=request.client_order_id,
                **error_details(exc),
            )
            self.store.mark_intent(request.client_order_id, "AMBIGUOUS")
            # Read by deterministic ID before any further action. Never resubmit.
            try:
                found = self.by_client_id(request.client_order_id)
                if found is not None:
                    emit(
                        "ambiguous_submission_recovered",
                        "INFO",
                        client_order_id=request.client_order_id,
                        status=text(found.status),
                    )
                    self.store.mark_intent(request.client_order_id, text(found.status))
                    return found
            except Exception:
                pass
            self.notify(
                "safety",
                "Ambiguous PAPER order; inspect broker: " + request.client_order_id,
                "ambiguous:" + request.client_order_id,
            )
            raise ValueError("Ambiguous paper submission; entries frozen") from None

    @traced("broker.manage_positions")
    def manage(self, now, events=(), daily=None):
        snapshot = self.reconcile(now)
        if self.mode != "paper-execute":
            emit("position_mutations_skipped", mode=self.mode, reason="non_execution_mode")
            return snapshot
        self.mutation_gate()  # kill switch intentionally does not gate exits
        for reason in snapshot.reasons:
            if not reason.startswith("Missing/mismatched protective exit"):
                self.notify(
                    "safety",
                    "PAPER reconciliation requires intervention: " + reason,
                    "unsafe-state:" + fingerprint(reason),
                )
        for order in self.last_orders:
            c = self.candidates.get(order.symbol)
            if (
                str(order.id) in self.owned_ids
                and c
                and text(order.side) == "buy"
                and active(order)
                and now >= c.expires_at
            ):
                self.cancel_order(order.id)
        for pos in self.positions:
            c = self.candidates.get(pos.symbol)
            if not c or abs(self.ledger_qty.get(pos.symbol, 0) - float(pos.qty)) > 1e-6:
                continue
            entries = [
                o
                for o in self.last_orders
                if str(o.id) in self.owned_ids
                and o.symbol == pos.symbol
                and text(o.side) == "buy"
                and float(o.filled_qty or 0) > 0
                and str(o.client_order_id) == c.signal_id
            ]
            opened = min(
                (getattr(o, "filled_at", None) or getattr(o, "created_at", now) for o in entries),
                default=now,
            )
            missing = any("protective exit " + pos.symbol in r for r in snapshot.reasons)
            due = held_sessions(opened, now) >= c.max_hold_sessions
            from .evidence import imminent_earnings
            from .signals import prior_daily

            event_exit = self.cfg.earnings_exit_enabled and imminent_earnings(
                pos.symbol, events, now, 1
            )
            breakdown = False
            if self.cfg.technical_exit_enabled and daily and pos.symbol in daily:
                history = prior_daily(daily[pos.symbol], now)
                breakdown = len(history) >= 21 and bool(
                    (history.close.tail(2) < history.close.rolling(20).mean().tail(2)).all()
                )
            if missing or due or event_exit or breakdown:
                if missing:
                    self.notify(
                        "safety",
                        "Missing PAPER protection: " + pos.symbol,
                        "protection:" + c.signal_id,
                    )
                self.store.audit(
                    "exit_policy",
                    {
                        "symbol": pos.symbol,
                        "time": due,
                        "earnings": event_exit,
                        "technical_breakdown": breakdown,
                        "protection_missing": missing,
                    },
                    now,
                )
                self.recover(pos.symbol, c, now, close=due or event_exit or breakdown)
        return self.reconcile(now)

    @traced("broker.recover_position")
    def recover(self, symbol, candidate, now, close=False):
        """Cancel group, wait for terminal state, reread actual position; never oversell."""
        emit("position_recovery_requested", "WARNING", symbol=symbol, close=close)
        self.mutation_gate()
        clock = read_call(self.client.get_clock)
        if not clock.is_open:
            self.notify(
                "safety",
                "PAPER exit recovery waiting for market open: " + symbol,
                "closed-recovery:" + candidate.signal_id,
            )
            return
        owned_open = [o for o in self.last_orders if o.symbol == symbol and active(o)]
        if any(str(o.id) not in self.owned_ids for o in owned_open):
            raise ValueError("Manual order prevents exit recovery")
        # Cancel one member, then reconcile before canceling another. OCO siblings
        # can fill during cancellation; no close request until every member is terminal.
        for order in owned_open:
            current = read_call(self.client.get_order_by_id, order.id)
            if active(current) and text(current.status) != "pending_cancel":
                self.cancel_order(current.id)
        fresh = self.orders()
        if any(o.symbol == symbol and active(o) for o in fresh):
            return  # cancellation asynchronous; next invocation reconciles
        self.reconcile(now)
        pos = next((p for p in self.positions if p.symbol == symbol), None)
        if pos is None:
            return
        qty = float(pos.qty)
        if qty <= 0 or abs(self.ledger_qty.get(symbol, 0) - qty) > 1e-6:
            raise ValueError("Ownership changed during cancellation")
        close = (
            close
            or float(pos.current_price) <= candidate.stop
            or float(pos.current_price) >= candidate.target
        )
        kind = "close" if close else "protect"
        identifier = "ax-" + fingerprint([candidate.signal_id, kind])[:32]
        if self.by_client_id(identifier) is not None:
            self.notify(
                "safety",
                "Recovery order needs inspection: " + symbol,
                "recovery-stuck:" + identifier,
            )
            return
        if not self.store.intent(
            identifier, symbol, {"candidate": candidate.model_dump(mode="json"), "kind": kind}
        ):
            return
        if close:
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=identifier,
            )
        else:
            request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                order_class=OrderClass.OCO,
                client_order_id=identifier,
                take_profit=TakeProfitRequest(limit_price=candidate.target),
                stop_loss=StopLossRequest(stop_price=candidate.stop),
            )
        self.submit_once(request, now)
