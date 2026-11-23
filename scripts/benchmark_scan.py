"""Synthetic CPU benchmark only; does not measure provider throughput/entitlements."""

import json
import time
from pathlib import Path

from tests.helpers import NOW, cfg, frames
from trading_bot.signals import regime, scan

settings = cfg(Path("reports/benchmark-state"))
bars, daily = frames()
market_regime = regime({s: daily for s in ("SPY", "QQQ", "IWM")}, NOW)
results = []
for size in (100, 250, 500, 1000):
    started = time.perf_counter()
    candidates = sum(
        len(scan(f"T{i}", f"Fixture {i}", bars, daily, daily, [], market_regime, settings, NOW))
        for i in range(size)
    )
    results.append(
        {
            "symbols": size,
            "seconds": round(time.perf_counter() - started, 3),
            "candidates": candidates,
        }
    )
output = {
    "kind": "SYNTHETIC CPU ONLY; no API calls, no full-market throughput claim",
    "results": results,
}
Path("reports").mkdir(exist_ok=True)
Path("reports/benchmark.json").write_text(json.dumps(output, indent=2))
print(json.dumps(output))
