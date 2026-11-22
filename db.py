"""SQLite ledger for the paper-trading bot."""
import sqlite3, json, datetime
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cash_usd REAL NOT NULL, start_balance_usd REAL NOT NULL, created_at TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'PAPER');
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, status TEXT, trigger TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, sources TEXT, tokens_seen INTEGER,
  candidates_passed_prefilter INTEGER, candidates TEXT);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, token_address TEXT, pair_address TEXT, symbol TEXT,
  price_usd REAL, liquidity_usd REAL, safety_passed INTEGER, safety_detail TEXT, score REAL, score_detail TEXT,
  decision TEXT, source_url TEXT);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, token_address TEXT NOT NULL, pair_address TEXT NOT NULL,
  symbol TEXT, name TEXT, dex_id TEXT, status TEXT NOT NULL,  -- open|closed
  entry_time TEXT, entry_mid_price REAL, entry_fill_price REAL, qty REAL, cost_usd REAL,
  entry_fee_usd REAL, entry_slippage_usd REAL, stop_price REAL, target_price REAL, score REAL, entry_source_url TEXT,
  last_price REAL, last_price_time TEXT, last_price_source TEXT,
  exit_time TEXT, exit_mid_price REAL, exit_fill_price REAL, proceeds_usd REAL, exit_fee_usd REAL,
  exit_slippage_usd REAL, realized_pnl_usd REAL, exit_reason TEXT, exit_source_url TEXT, exit_run_id INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_token ON positions(token_address) WHERE status='open';
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER, run_id INTEGER, side TEXT, ts TEXT,
  token_address TEXT, pair_address TEXT, mid_price REAL, fill_price REAL, qty REAL, gross_usd REAL, net_usd REAL,
  fee_usd REAL, slippage_usd REAL, price_fetched_at TEXT, source_url TEXT, raw_pair TEXT);
CREATE TABLE IF NOT EXISTS skips (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, token_address TEXT, symbol TEXT, stage TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, stage TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS equity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, cash_usd REAL, positions_value_usd REAL, equity_usd REAL);
CREATE TABLE IF NOT EXISTS safety_cache (
  token_address TEXT PRIMARY KEY, fetched_at TEXT, source_url TEXT, report TEXT);
"""

# GitHub Actions metadata (added 2026-11-22; NULL for earlier box/routine runs)
RUN_EXTRA_COLUMNS = [("event", "TEXT"), ("github_run_id", "TEXT"), ("github_run_attempt", "INTEGER"),
                     ("github_run_url", "TEXT"), ("scheduled_for", "TEXT"), ("queued_at", "TEXT"),
                     ("job_started_at", "TEXT"), ("start_delay_sec", "REAL"), ("error_summary", "TEXT")]

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def connect(path=None):
    con = sqlite3.connect(path or config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    for col, typ in RUN_EXTRA_COLUMNS:
        if col not in have:
            con.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
    if con.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0:
        con.execute("INSERT INTO account(id,cash_usd,start_balance_usd,created_at) VALUES (1,?,?,?)",
                    (config.START_BALANCE_USD, config.START_BALANCE_USD, now_iso()))
        con.commit()
    return con

def log_skip(con, run_id, token, symbol, stage, reason):
    con.execute("INSERT INTO skips(run_id,ts,token_address,symbol,stage,reason) VALUES (?,?,?,?,?,?)",
                (run_id, now_iso(), token, symbol, stage, reason))

def log_error(con, run_id, stage, message):
    con.execute("INSERT INTO errors(run_id,ts,stage,message) VALUES (?,?,?,?)", (run_id, now_iso(), stage, str(message)[:2000]))

def get_cash(con):
    return con.execute("SELECT cash_usd FROM account WHERE id=1").fetchone()[0]

def open_positions(con):
    return [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='open' ORDER BY id")]

def j(x):
    return json.dumps(x, default=str)
