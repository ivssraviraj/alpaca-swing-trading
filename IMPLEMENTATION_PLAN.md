# Implementation plan and initial audit

The repository starts with a two-setup technical scanner, a next-bar replay,
optional Sheets/webhooks, three workflows and ten offline tests. No historical
replay artifacts or local credentials are present. The existing virtual environment
uses Python 3.9.6; the supported application will use Python 3.12.

Confirmed issues: historical requests cap total records at 10,000; execution has
no AI gate; kill switch prevents existing-position management; quote/no-chase checks
are absent; time exits default off; notification policy includes unfilled orders;
SQLite/durable approval state is absent; old connectivity script executes on import.

Implementation sequence:
1. Keep pure legacy signal/replay interfaces for reproducible comparison; retire
   the old order-placement path in favor of a single guarded application.
2. Add validated models/config, calendar, batched data, daily universe, event/news
   evidence, nine technical setups, regime and deterministic portfolio checks.
3. Add official OpenAI structured output, evidence binding, durable cache and budgets.
4. Add paper-only execution, exclusive writer lock, durable intents, broker recovery,
   confirmed-fill audit, protective/time exits independent of AI and kill switch.
5. Add optional isolated Sheets ledger and filtered notifications, replay/reports,
   safe workflows, local commands and operational documentation.
6. Execute offline tests, lint, typing, fixture replay and scaling measurements;
   run real read-only services only when credentials exist. Never place an order.

State choice: local SQLite for local work; PostgreSQL for scheduled execution and
AI budgets. GitHub artifacts are audit exports, never the authority for critical
state. No new paid service is mandatory for observation. A configured persistent
PostgreSQL endpoint is an explicit dependency for scheduled execution.

The existing ChatGPT tasks and spreadsheet portfolios will not be accessed or
modified. Execution stays disabled. No commit or push is performed by this task.

## Future scope added 2026-11-22

The preferred API model is now GPT-5.4. See [Future enhancements](FUTURE_ENHANCEMENTS.md)
for the planned Sheets watchlist and research queue, personal ChatGPT research
handoff, smaller-model evidence validation, and deterministic Python execution.
The new handoff is a backlog item; no connected spreadsheet, ChatGPT task, or
broker execution setting is activated by this documentation.
