CREATE TABLE account (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cash_usd REAL NOT NULL, start_balance_usd REAL NOT NULL, created_at TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'PAPER');
CREATE TABLE equity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, cash_usd REAL, positions_value_usd REAL, equity_usd REAL);
CREATE TABLE errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, stage TEXT, message TEXT);
CREATE TABLE evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, token_address TEXT, pair_address TEXT, symbol TEXT,
  price_usd REAL, liquidity_usd REAL, safety_passed INTEGER, safety_detail TEXT, score REAL, score_detail TEXT,
  decision TEXT, source_url TEXT);
CREATE TABLE fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER, run_id INTEGER, side TEXT, ts TEXT,
  token_address TEXT, pair_address TEXT, mid_price REAL, fill_price REAL, qty REAL, gross_usd REAL, net_usd REAL,
  fee_usd REAL, slippage_usd REAL, price_fetched_at TEXT, source_url TEXT, raw_pair TEXT);
CREATE TABLE positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, token_address TEXT NOT NULL, pair_address TEXT NOT NULL,
  symbol TEXT, name TEXT, dex_id TEXT, status TEXT NOT NULL,  -- open|closed
  entry_time TEXT, entry_mid_price REAL, entry_fill_price REAL, qty REAL, cost_usd REAL,
  entry_fee_usd REAL, entry_slippage_usd REAL, stop_price REAL, target_price REAL, score REAL, entry_source_url TEXT,
  last_price REAL, last_price_time TEXT, last_price_source TEXT,
  exit_time TEXT, exit_mid_price REAL, exit_fill_price REAL, proceeds_usd REAL, exit_fee_usd REAL,
  exit_slippage_usd REAL, realized_pnl_usd REAL, exit_reason TEXT, exit_source_url TEXT, exit_run_id INTEGER);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, status TEXT, trigger TEXT, summary TEXT, event TEXT, github_run_id TEXT, github_run_attempt INTEGER, github_run_url TEXT, scheduled_for TEXT, queued_at TEXT, job_started_at TEXT, start_delay_sec REAL, error_summary TEXT);
CREATE TABLE scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, sources TEXT, tokens_seen INTEGER,
  candidates_passed_prefilter INTEGER, candidates TEXT);
CREATE TABLE skips (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, token_address TEXT, symbol TEXT, stage TEXT, reason TEXT);
CREATE UNIQUE INDEX one_open_per_token ON positions(token_address) WHERE status='open';
