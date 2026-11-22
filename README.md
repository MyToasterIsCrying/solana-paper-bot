# Solana Paper Bot 2.0 — PAPER TRADING ONLY

A simulated Solana memecoin trading bot with a **$100 virtual account**. No wallet, no private keys, no real transactions,
no paid services, no secrets. Data comes from free public APIs: DEX Screener (prices, pairs, discovery), GeckoTerminal
(discovery only) and RugCheck (safety).

**Dashboard:** GitHub Pages site built from `docs/` on every run.

## How it runs
`.github/workflows/scan.yml` runs every 15 minutes (`cron: */15 * * * *`, best effort: GitHub may delay scheduled runs)
and on manual dispatch. Each run restores `ledger.db` from `ledger/`, runs one cycle (`gh_run.py` -> `run_cycle.py`),
rebuilds the dashboard, dumps the ledger back to `ledger/` and commits it to `main`, then deploys Pages.

Pipeline per cycle: monitor open positions (exit at the first fresh verified price past stop/target) -> SCANNER ->
SAFETY -> ANALYST -> RISK -> EXECUTOR -> equity snapshot.

Rules (`config.py`): 5% of equity per position, max 3 open, 6% stop, 12% target, entry score >= 65, no averaging down,
no leverage, fees + spread + slippage modelled on every simulated fill. Missing or stale data, or missing safety data,
means skip/reject.

## Ledger storage
`ledger/` holds the SQLite ledger as diff-friendly SQL text (`schema.sql`, `state.sql`, and one file per UTC day for
append-only history). `python3 tools/ledger_io.py restore|dump|verify`.

## Duplicate / overlap protection
Workflow `concurrency` group (one run at a time), an fcntl lock in `run_cycle.py`, and a unique DB index allowing one
open position per token.

## Local use
    pip install -r requirements.txt
    python3 tools/ledger_io.py restore
    python3 run_cycle.py            # one cycle (don't push a local ledger while Actions is running)
    python3 dashboard.py
    python3 -m unittest tests.test_accounting -v   # temp DB + synthetic TEST-FIXTURE prices only
