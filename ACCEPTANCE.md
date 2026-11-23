# Acceptance review

**Activation decision: READY FOR OBSERVATION after configuring and verifying paper
credentials. Not ready for unattended paper execution.** No real order was placed,
no remote configuration changed, and no push or commit was made.

## Implemented and exercised locally

| Area | Evidence / boundary |
|---|---|
| Existing functionality | All ten original regression tests retained; pure baseline modules preserved; old unsafe executor replaced by compatibility routing |
| Configuration / paper-only | Typed safe defaults, fixed paper origin, mode/acknowledgement gates, secret-safe diagnostics |
| Universe / data | Tradable asset filtering, daily liquidity snapshots, batches, SDK pagination, cache refresh and explicit coverage; synthetic/mocked provider tests |
| Calendar / signals | Holidays, DST, early closes, completed candles, warmup, all nine setup fixtures, relative strength and regime |
| Earnings / evidence | Supported Finnhub calendar adapter, actual/estimate null handling, release-news discovery, strict chronology, optional macro adapter; source URLs retained |
| AI | Official OpenAI structured parsing, exact source/candidate/context binding, cache, budgets, throttling; mock API success/failure tests |
| Deterministic risk | Price/quote, net reward/risk, cash, caps, sector and correlation controls; cannot be overridden by a model |
| Paper adapter / ownership | No alternate endpoint; deterministic durable intents; broker account/orders/fills; manual/mixed positions blocked |
| Protection / exits | Stop and target quantity checks, partial-fill recovery, cancel/fill races, 15-session exits, earnings and optional technical exit policies |
| Durable state | Real local SQLite lock/journal/restart tests; PostgreSQL adapter/advisory-lock contract mocked only |
| Ledger / Sheets / alerts | Atomic fill checkpoints/cost basis; dedicated schema and dedup; mock Sheets/webhooks; Saturday report; no submission/no-trade alerts |
| Research | Shared risk simulator, delayed execution, gaps/costs, fixed targets, archive-only AI, three-mode/cost comparison; synthetic scenarios only |
| Workflows / docs | Single scheduled executor, no order path on push, explicit modes, environment secrets, YAML structural checks and documented one-push procedure |

## External blockers — not passed

- **Alpaca read-only account/data/news/quote/asset/calendar entitlement:** no local paper keys.
- **OpenAI real structured-output call:** no API key/billing configuration.
- **Finnhub earnings/economic calendar entitlement and completeness:** no key; optional macro timestamps must pass strict timezone checks.
- **Persistent PostgreSQL service:** no connection configured and no local PostgreSQL server; only its client contract was mocked.
- **Real paper fills, partial fills, bracket/OCO activation and cancellation recovery:** deliberately not submitted without explicit opt-in; no live-money testing is possible through supported configuration.
- **Google Sheets and webhook destination:** no local credentials/destination; no remote sheet was opened or modified.
- **Historical 100-day or longer real-market replay / out-of-sample comparison:** missing data and archived AI evidence.
- **Actual GitHub runner execution and provider throughput at scale:** requires the user's push and credentialed workflows.

These blockers preclude declaring the execution/reconciliation/data stack ready
for unattended use. `EXECUTION_VALIDATED_ACK` is intentionally unset; it is not a
substitute for performing the external checks.

## Explicitly limited or deferred

- Universe eligibility is based on Alpaca metadata/name screening and feed liquidity;
  it does not claim exhaustive instrument classification or consolidated-market coverage.
- Earnings discovery has a configured per-scan cap. Calendar snapshots are refreshed
  hourly. Provider absence or incomplete information is reported, not invented.
- Primary SEC/IR document crawling, an independent sector-news subscription, full
  historical consensus revisions, margins/FCF/earnings-quality extraction and complete
  licensed event coverage are not separate implemented data products. Available
  company news/calendar facts are passed to AI; missing information is explicit.
- Optional macro calendar is implemented but off by default and unverified externally.
  Sector-relative price evidence is implemented; publisher coverage remains limited.
- Optional trailing-stop replacement is deferred; initial stop/target, deterministic
  time/event exits and optional daily breakdown cover the supported exit policies.
- Generic webhook delivery cannot guarantee exactly once across a crash. Stable event
  IDs and durable dedup reduce duplicates; the ledger itself is transactional.
- Rejected-candidate 5/15-session price markouts are implemented and tested; they
  are observational price changes, not a claim of realizable missed trades. No
  real forward observation or live long-period statistical validation has run.
- Runtime PostgreSQL backup/retention and provider onboarding are operator setup tasks.
  No paid infrastructure was provisioned or made mandatory for observation.

Every execution safeguard tested locally is still a mocked/synthetic result unless
TEST_REPORT.md explicitly identifies a real service. A function's existence is not
presented as completed external validation.
