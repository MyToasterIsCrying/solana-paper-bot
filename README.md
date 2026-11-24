# Solana Paper Bot 2.0 — PAPER TRADING ONLY

A simulated Solana memecoin trading bot with a **$100 virtual account**. No wallet, no private keys, no real transactions,
no paid services, no secrets. Data comes from free public APIs: DEX Screener (prices, pairs, discovery), GeckoTerminal
(discovery only) and RugCheck (safety).

**Dashboard:** GitHub Pages site built from `docs/` by the existing GitHub Actions workflow.

## Existing GitHub Actions workflow
`.github/workflows/scan.yml` runs every 15 minutes (`cron: */15 * * * *`, best effort: GitHub may delay scheduled runs)
and on manual dispatch. Each run restores `ledger.db` from `ledger/`, runs one cycle (`gh_run.py` -> `run_cycle.py`),
rebuilds the dashboard, dumps the ledger back to `ledger/` and commits it to `main`, then deploys Pages.

Pipeline per cycle: monitor open positions (exit at the first fresh verified price past stop/target) -> SCANNER ->
SAFETY -> ANALYST -> RISK -> EXECUTOR -> equity snapshot. This workflow remains unchanged by the local service work.

Rules (`config.py`): 5% of equity per position, max 3 open, 6% stop, 12% target, entry score >= 65, no averaging down,
no leverage, fees + spread + slippage modelled on every simulated fill. Missing or stale data, or missing safety data,
means skip/reject.

## Ledger storage
`ledger/` holds the SQLite ledger as diff-friendly SQL text (`schema.sql`, `state.sql`, and one file per UTC day for
append-only history). `python3 tools/ledger_io.py restore|dump|verify`.

## Duplicate / overlap protection
The workflow has a concurrency group, local processes use a cross-platform process lock, and the ledger has a unique DB
index allowing one open position per token. GitHub-hosted and local processes do not share an OS lock. Do not migrate
ledger writing to the local service until GitHub Actions is no longer writing the same ledger.

## Experimental Windows-local service
`local_service.py` is an opt-in, paper-only service on the `dev/windows-local-monitor` development branch. It schedules
open-position monitoring every 60 seconds and launches token discovery every 15 minutes in a separate worker process.
The worker receives only a snapshot of cached safety reports and open tokens; it does not open or write the ledger. The
service process is the only writer to its local ledger. All public API work for discovery happens outside the monitor.
New paper entries are disabled by default; they require the explicit `--allow-entries` option after separate approval.

The service defaults to `%LOCALAPPDATA%\SolanaPaperBot\` for its database, lock, log, and dashboard JSON, keeping its
active SQLite files outside the OneDrive-synced repository. It refuses to create a fresh account or use the configured
GitHub/production ledger path, and requires an explicit migration-confirmation environment variable before startup.
Before any use, stop or disable every GitHub workflow that can restore, run, dump, or commit that ledger; wait for its
active runs to finish; back up the latest ledger; then restore that snapshot to the separate local data directory. This
development change does not alter the active workflow or start the service.

Safe migration outline (run from the repository root, and only after stopping GitHub ledger writers):

1. Copy the complete latest remote `ledger/` directory to a dated backup and verify its hashes.
2. Set the verified backup directory and ensure the separate local database does not already exist:

       $env:PAPERBOT_LEDGER_SOURCE = 'C:\path\to\verified-backup\ledger'
       $env:PAPERBOT_LOCAL_DB = Join-Path $env:LOCALAPPDATA 'SolanaPaperBot\local_ledger.db'
       New-Item -ItemType Directory -Path (Split-Path $env:PAPERBOT_LOCAL_DB) -Force
       & "$env:LOCALAPPDATA\SolanaPaperBot\venv\Scripts\python.exe" -c "import os; from tools.ledger_io import restore; p=os.environ['PAPERBOT_LOCAL_DB']; assert not os.path.exists(p), 'local ledger already exists; preserve it and choose a new path'; restore(src=os.environ['PAPERBOT_LEDGER_SOURCE'], db=p)"

3. Verify the local database is the expected PAPER account and compare the trade/run counts and cash with the
   pre-migration snapshot. Keep the SQL backup unchanged.
4. Start the service manually in PowerShell; this explicit flag is intentionally required:

       $env:PAPERBOT_LOCAL_MIGRATION_CONFIRMED = 'YES'
       & "$env:LOCALAPPDATA\SolanaPaperBot\venv\Scripts\python.exe" .\local_service.py

Do not set the confirmation flag until migration is complete. The service does not install a Windows Task Scheduler task
or start automatically. Keep the PC awake and connected for monitoring. A wake or restart triggers a fresh check; missed
intervals are logged and are not replayed. An outage, sleep, stale quote, or price gap can still delay an exit or produce
a simulated fill beyond the configured stop. A stale or failed quote is logged and never treated as a fill. The stop
remains a threshold on the first fresh observed mid-price, not a guaranteed stop-price execution.

An isolated timed trial can use `--duration-sec N --synthetic-monitor` with a separate copy of the PAPER ledger. The
monitor fixture cycles through fresh, stale, and failed quotes; the discovery worker remains read-only against free
public APIs. The synthetic monitor option refuses the configured production or local ledger paths and cannot be
combined with `--allow-entries`.

Local dashboard output is written under `%LOCALAPPDATA%\SolanaPaperBot\local_dashboard.json`; the committed dashboard
and ledger remain untouched during development. Offline tests use synthetic prices only:

    python -m unittest tests.test_local_service tests.test_accounting -v

The verified remote-ledger backup is preserved under
`%LOCALAPPDATA%\SolanaPaperBot\backups\main-672b6b9-20261124T0101`.

## Dashboard-only Pages publishing

The local dashboard publisher reads only the canonical AppData dashboard and opens the canonical local ledger in
SQLite read-only mode. The service marks snapshots publishable only when they come from that canonical ledger and
contain no synthetic fixture markers. The publisher checks PAPER mode, snapshot age, account totals, and position/fill
counts before building the existing `docs/` site. It stages only `docs/index.html`, `docs/dashboard.json`,
`docs/.nojekyll`, and the Pages-only workflow on the separate `dashboard-feed` branch; unexpected files, including
ledger files, cause it to stop. Pushing is opt-in via `--publish`; the workflow validates freshness and fixture markers,
has no ledger-writing permission, and deploys only `docs/`. It does not check out `main` or run a trading cycle.

Run `python tools/publish_dashboard.py` for local validation/build only. Do not add `--publish` until deployment is
separately approved. The existing trading workflow remains disabled and unchanged.

## Prepared Task Scheduler configuration

`tools/prepare_local_service_task.ps1` writes disabled task definitions for the service and dashboard publisher under
`%LOCALAPPDATA%\SolanaPaperBot\`; it does not register or enable either task. The service task starts only at logon
when later enabled, retries failures three times at one-minute intervals, ignores duplicate instances, and relies on
the service process lock as a second overlap guard. The publisher task runs every 15 minutes when enabled, uses only
the dashboard publisher's explicit `--publish` path, and also ignores duplicate instances. The publisher validates
the canonical PAPER ledger read-only and stages only dashboard files; it cannot write the ledger. Do not import or
enable either task until the local-ledger migration, Pages workflow availability, and automatic operation are
separately approved.

## Local use
    pip install -r requirements.txt
    python3 tools/ledger_io.py restore
    python3 run_cycle.py            # one cycle (don't push a local ledger while Actions is running)
    python3 dashboard.py
    python3 -m unittest tests.test_accounting -v   # temp DB + synthetic TEST-FIXTURE prices only
