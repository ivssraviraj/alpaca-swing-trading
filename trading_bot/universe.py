from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from .calendar import NY
from .diagnostics import emit, traced
from .models import fingerprint
from .providers import read_call


def text(value):
    return str(getattr(value, "value", value)).lower()


def asset_eligible(asset):
    return asset_rejection_reason(asset) is None


def asset_rejection_reason(asset):
    if text(asset.status) != "active":
        return "inactive"
    if not asset.tradable:
        return "not_tradable"
    if text(asset.asset_class) != "us_equity":
        return "asset_class"
    if text(asset.exchange) not in {"nyse", "nasdaq", "arca", "amex", "bats", "nysearca"}:
        return "exchange"
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", asset.symbol):
        return "symbol_format"
    # Alpaca metadata has no definitive leverage flag; conservatively exclude
    # names identifying derivatives, leveraged/inverse and single-stock funds.
    if re.search(
        r"\b(2x|3x|4x|ultra\w*|short|bear|inverse|leveraged|daily|warrant|rights)\b",
        asset.name,
        re.I,
    ):
        return "excluded_product_name"
    return None


class Universe:
    def __init__(self, trading, data, settings, store):
        self.trading, self.data, self.cfg, self.store = trading, data, settings, store

    @traced("universe.build")
    def build(self, now):
        sectors = (
            json.loads(Path(self.cfg.sector_map_path).read_text())
            if self.cfg.sector_map_path
            else {}
        )
        if not isinstance(sectors, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v.strip()
            for k, v in sectors.items()
        ):
            raise ValueError("Sector map must contain symbol-to-nonempty-sector strings")
        identity = [
            self.cfg.universe_mode,
            self.cfg.universe_size,
            self.cfg.symbols,
            self.cfg.min_price,
            self.cfg.min_daily_dollar_volume,
            self.cfg.data_feed,
            fingerprint(sectors),
        ]
        key = (
            "universe:"
            + now.astimezone(__import__("zoneinfo").ZoneInfo(NY)).date().isoformat()
            + ":"
            + fingerprint(identity)
        )
        saved = self.store.get(key)
        if saved is not None:
            emit("universe_cache_hit", "INFO", selected_count=len(saved), as_of=now)
            self.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
            (self.cfg.reports_dir / "universe.json").write_text(json.dumps(saved, indent=2))
            return saved
        emit(
            "universe_cache_miss",
            mode=self.cfg.universe_mode,
            target=self.cfg.universe_size,
            as_of=now,
        )
        assets = read_call(
            self.trading.get_all_assets,
            GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY),
        )
        raw_count = len(assets)
        eligible_assets, metadata_failures = [], Counter()
        for asset in assets:
            reason = asset_rejection_reason(asset)
            if reason:
                metadata_failures[reason] += 1
                emit("asset_rejected", symbol=asset.symbol, reason=reason)
            else:
                eligible_assets.append(asset)
        assets = eligible_assets
        emit(
            "asset_eligibility",
            returned=raw_count,
            metadata_eligible=len(assets),
            metadata_rejected=raw_count - len(assets),
            rejection_counts=dict(metadata_failures),
        )
        if self.cfg.universe_mode == "static":
            requested = set(self.cfg.symbols.split(","))
            assets = [a for a in assets if a.symbol in requested]
            emit(
                "static_watchlist",
                requested=sorted(requested),
                eligible=[a.symbol for a in assets],
                unavailable=sorted(requested - {a.symbol for a in assets}),
            )
        daily = self.data.daily([a.symbol for a in assets], now)
        selected, rejected = [], []
        for asset in assets:
            bars = daily.get(asset.symbol)
            metrics = {
                "daily_bars": 0 if bars is None else len(bars),
                "required_daily_bars": self.cfg.min_daily_bars,
            }
            reason = "insufficient_daily_data"
            if bars is not None and len(bars) >= self.cfg.min_daily_bars:
                last = bars.iloc[-1]
                dollar_volume = float((bars.close * bars.volume).tail(20).median())
                metrics.update(
                    price=float(last.close),
                    min_price=self.cfg.min_price,
                    median_daily_dollar_volume=dollar_volume,
                    min_daily_dollar_volume=self.cfg.min_daily_dollar_volume,
                )
                reason = "price" if last.close < self.cfg.min_price else "liquidity"
                if (
                    last.close >= self.cfg.min_price
                    and dollar_volume >= self.cfg.min_daily_dollar_volume
                ):
                    selected.append(
                        dict(
                            symbol=asset.symbol,
                            name=asset.name,
                            exchange=text(asset.exchange),
                            sector=sectors.get(asset.symbol, "UNKNOWN"),
                            price=float(last.close),
                            median_daily_dollar_volume=dollar_volume,
                            selected_at=now.isoformat(),
                            source="Alpaca assets + " + self.cfg.data_feed,
                            reason="eligible",
                        )
                    )
                    continue
            rejected.append({"symbol": asset.symbol, "reason": reason, **metrics})
        selected.sort(key=lambda x: (-x["median_daily_dollar_volume"], x["symbol"]))
        result = selected[: self.cfg.universe_size]
        self.store.put(key, result)
        self.store.audit(
            "universe",
            {
                "target": self.cfg.universe_size,
                "returned_assets": raw_count,
                "metadata_rejection_counts": dict(metadata_failures),
                "asset_count": len(assets),
                "eligible": len(selected),
                "selected": result,
                "rejected": rejected,
                "rejection_counts": dict(Counter(r["reason"] for r in rejected)),
                "eligible_excluded_by_rank_limit": len(selected) - len(result),
                "lowest_selected_dollar_volume": result[-1]["median_daily_dollar_volume"]
                if result
                else None,
                "sector_mapping_count": len(sectors),
            },
            now,
        )
        self.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.reports_dir / "universe.json").write_text(json.dumps(result, indent=2))
        return result
