from __future__ import annotations

import json
import re
from pathlib import Path

from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from .calendar import NY
from .models import fingerprint
from .providers import read_call


def text(value):
    return str(getattr(value, "value", value)).lower()


def asset_eligible(asset):
    if (
        text(asset.status) != "active"
        or not asset.tradable
        or text(asset.asset_class) != "us_equity"
    ):
        return False
    if text(asset.exchange) not in {"nyse", "nasdaq", "arca", "amex", "bats", "nysearca"}:
        return False
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", asset.symbol):
        return False
    # Alpaca metadata has no definitive leverage flag; conservatively exclude
    # names identifying derivatives, leveraged/inverse and single-stock funds.
    return not re.search(
        r"\b(2x|3x|4x|ultra\w*|short|bear|inverse|leveraged|daily|warrant|rights)\b",
        asset.name,
        re.I,
    )


class Universe:
    def __init__(self, trading, data, settings, store):
        self.trading, self.data, self.cfg, self.store = trading, data, settings, store

    def build(self, now):
        identity = [
            self.cfg.universe_mode,
            self.cfg.universe_size,
            self.cfg.symbols,
            self.cfg.min_price,
            self.cfg.min_daily_dollar_volume,
            self.cfg.data_feed,
        ]
        key = (
            "universe:"
            + now.astimezone(__import__("zoneinfo").ZoneInfo(NY)).date().isoformat()
            + ":"
            + fingerprint(identity)
        )
        saved = self.store.get(key)
        if saved is not None:
            return saved
        assets = read_call(
            self.trading.get_all_assets,
            GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY),
        )
        assets = [a for a in assets if asset_eligible(a)]
        if self.cfg.universe_mode == "static":
            requested = set(self.cfg.symbols.split(","))
            assets = [a for a in assets if a.symbol in requested]
        daily = self.data.daily([a.symbol for a in assets], now)
        sectors = (
            json.loads(Path(self.cfg.sector_map_path).read_text())
            if self.cfg.sector_map_path
            else {}
        )
        selected, rejected = [], []
        for asset in assets:
            bars = daily.get(asset.symbol)
            reason = "insufficient_daily_data"
            if bars is not None and len(bars) >= self.cfg.min_daily_bars:
                last = bars.iloc[-1]
                dollar_volume = float((bars.close * bars.volume).tail(20).median())
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
            rejected.append({"symbol": asset.symbol, "reason": reason})
        selected.sort(key=lambda x: (-x["median_daily_dollar_volume"], x["symbol"]))
        result = selected[: self.cfg.universe_size]
        self.store.put(key, result)
        self.store.audit(
            "universe",
            {
                "target": self.cfg.universe_size,
                "asset_count": len(assets),
                "eligible": len(selected),
                "selected": result,
                "rejected": rejected,
            },
            now,
        )
        self.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.reports_dir / "universe.json").write_text(json.dumps(result, indent=2))
        return result
