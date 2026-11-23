# One consolidated GitHub update

No push, commit, deployment or remote settings change was performed by this task.
The repository is prepared for one local review and your final commit/push.

## One-time settings

Use the GitHub environment named `paper` (create it in repository Settings →
Environments). Existing repository-level Alpaca secrets can remain; environment
secrets may override them. Keep automatic execution disabled.

Required for observation: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` (paper credentials).
For AI observation: `OPENAI_API_KEY`, `AI_ENABLED=true`, a supported `OPENAI_MODEL`,
and a persistent `DATABASE_URL` for scheduled calls and their budgets.
For calendar-aware entry proposals: `FINNHUB_API_KEY` with earnings calendar access.
Optional reporting: `GOOGLE_SA_JSON_BASE64`, `GOOGLE_SHEET_ID`, `SHEETS_ENABLED=true`,
`ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_KIND`. Add credentials in GitHub Secrets, not Variables.
Never expose them in chat or command output.

Start with these nonsecret Variables:

```text
PAPER_ORDERS_ENABLED=false
KILL_SWITCH=false
STRATEGY_CAPITAL_USD=25000
AI_ENABLED=false
OPENAI_MODEL=gpt-5.4
AI_INPUT_USD_PER_MILLION=2.5
AI_OUTPUT_USD_PER_MILLION=15
UNIVERSE_MODE=static
SYMBOLS=SPY,QQQ,IWM,AAPL,MSFT,NVDA
UNIVERSE_SIZE=100
DATA_FEED=iex
SHEETS_ENABLED=false
```

Existing GitHub repository/environment Variables override workflow defaults. Update
`OPENAI_MODEL` and both rate variables together when deploying the GPT-5.4 preference;
editing local `.env` does not change remote Variables. The $1 daily guard uses
conservative reservations, so it may stop before the 30-call ceiling even when
provider-side sharing makes eligible traffic complimentary.

After connectivity/coverage is verified, set `UNIVERSE_MODE=dynamic` to use discovery.
The requested size is a target, not a promised analyzed count. Review request time,
provider limits and GitHub quota before moving from 100 to 250/500/1,000.

Do not set execution acknowledgements from this delivery alone. Activation requires
reviewed real-service recovery tests, persistent state, enabled AI, available evidence
and calendar, and all documented gates. Existing `YES_PAPER_ONLY` by itself cannot
activate the new executor. A ChatGPT subscription is not a configured API credential
or proof of API funding; verify API billing separately.

## Review, commit and push once

From the repository root, after reviewing `.env.example` and the reports:

```bash
source .venv312/bin/activate
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m pytest
python scripts/check_workflows.py
python scripts/secret_scan.py
git diff --check
git status --short
git add .github .gitignore .env.example .python-version pyproject.toml requirements.txt requirements.lock \
  engine.py strategy.py replay.py data_audit.py test_connection.py trading_bot tests scripts \
  README.md ARCHITECTURE.md STRATEGY.md CONFIGURATION.md OPERATIONS.md \
  TEST_REPORT.md BACKTEST_REPORT.md DEPLOYMENT.md ACCEPTANCE.md IMPLEMENTATION_PLAN.md FUTURE_ENHANCEMENTS.md
git diff --cached --check
git diff --cached --stat
git commit -m "Integrate AI-reviewed paper swing engine with durable safeguards and tests"
git push origin main
```

Do not add `.env`, runtime state, credentials, local virtual environments, or private
reports. `.gitignore` excludes them. The explicit `git add` list includes source and
synthetic fixtures only. Existing `.idea/.gitignore` is left as it was.

## Post-push checks

1. **Validate Swing Code** runs on push/PR. It has no brokerage secrets, runs offline
   tests/static checks and synthetic replay, and cannot trigger an order workflow.
2. Run **Alpaca Connection Test** manually. Expect a PAPER account status and nonempty
   configured-feed bar count. It is read-only. Missing credentials should fail BLOCKED.
3. Run **Alpaca Swing Engine → observe** manually with execution disabled. Inspect
   `alpaca-audit-<run id>` for actual universe, coverage and rejection reasons.
4. Run **replay** for 100 days. Inspect coverage before interpreting metrics. With
   no historical calendar/AI archive, strict default entry gates can yield zero
   eligible trades; missing evidence is not an AI pass. See BACKTEST_REPORT.md for
   the technical-only research procedure and limitations.
5. Once API billing and persistent state are configured, run **check-ai**, then enable
   AI in observation and run **paper-dry-run**. No broker mutations occur in either.
6. Verify the Saturday report/destination separately. Sheet synchronization requires
   the dedicated tab schema and authorized service account. No ChatGPT task is invoked.

There is exactly one scheduled trading workflow, `swing-scan.yml`. It covers weekdays
at roughly 15-minute UTC intervals spanning EST/EDT sessions and a Saturday summary.
Python decides session eligibility, including early closes. `cancel-in-progress:false`
and a shared concurrency group prevent competing scheduled executions; PostgreSQL
is the cross-process safeguard. A kill switch does not switch scheduled execution
into observation, preserving authorized exit management. GitHub cron is best-effort,
not an execution SLA. No push event can directly place orders.
