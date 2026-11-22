#!/usr/bin/env python3
"""Dashboard: python3 dashboard.py [--json] [--db PATH]. Also writes dashboard.json next to the ledger."""
import json, argparse, os
import config, db

def build(db_path=None):
    con = db.connect(db_path)
    acct = dict(con.execute("SELECT * FROM account WHERE id=1").fetchone())
    opens = db.open_positions(con)
    open_rows, unreal, pv = [], 0.0, 0.0
    for p in opens:
        px = p["last_price"] or p["entry_mid_price"]; val = px * p["qty"]
        # unrealized = value at last verified mid minus all-in cost (exit costs not yet deducted)
        u = val - p["cost_usd"]; unreal += u; pv += val
        open_rows.append({"symbol": p["symbol"], "token_address": p["token_address"], "pair_address": p["pair_address"],
            "entry_time": p["entry_time"], "entry_mid_price": p["entry_mid_price"], "entry_fill_price": p["entry_fill_price"],
            "qty": p["qty"], "cost_usd": p["cost_usd"], "last_price": px, "last_price_time": p["last_price_time"],
            "market_value_usd": val, "unrealized_pnl_usd": u, "stop_price": p["stop_price"], "target_price": p["target_price"],
            "source": p["last_price_source"]})
    closed = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='closed' ORDER BY exit_time")]
    realized = sum(c["realized_pnl_usd"] for c in closed)
    wins = sum(1 for c in closed if c["realized_pnl_usd"] > 0)
    fees = con.execute("SELECT COALESCE(SUM(fee_usd),0), COALESCE(SUM(slippage_usd),0), COUNT(*) FROM fills").fetchone()
    eqs = [r[0] for r in con.execute("SELECT equity_usd FROM equity_snapshots ORDER BY id")]
    cash = acct["cash_usd"]; equity = cash + pv
    peak, mdd, mdd_usd = acct["start_balance_usd"], 0.0, 0.0
    for e in eqs + [equity]:
        peak = max(peak, e)
        if peak > 0 and (peak - e) / peak > mdd:
            mdd, mdd_usd = (peak - e) / peak, peak - e
    last_scan = con.execute("SELECT ts, tokens_seen, candidates_passed_prefilter, sources FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    last_run = con.execute("SELECT id, started_at, finished_at, status, trigger FROM runs WHERE status!='skipped_overlap' ORDER BY id DESC LIMIT 1").fetchone()
    trades = [dict(r) for r in con.execute(
        "SELECT f.id, f.position_id, f.side, f.ts, p.symbol, f.token_address, f.pair_address, f.mid_price, f.fill_price, f.qty,"
        " f.gross_usd, f.net_usd, f.fee_usd, f.slippage_usd, f.price_fetched_at, f.source_url, p.exit_reason, p.realized_pnl_usd"
        " FROM fills f JOIN positions p ON p.id=f.position_id ORDER BY f.id")]
    for t in trades:
        if t["side"] == "BUY":
            t["exit_reason"] = None; t["realized_pnl_usd"] = None
    sched_file = os.path.join(config.BASE_DIR, "schedule_status.json")
    schedule = json.load(open(sched_file)) if os.path.exists(sched_file) else {"active": False, "next_run": None,
               "note": "Scheduled runs are NOT active (awaiting approval)."}
    out = {
        "mode": "PAPER TRADING - SIMULATION ONLY (no wallet, no real transactions)",
        "generated_at": db.now_iso(),
        "start_balance_usd": acct["start_balance_usd"], "portfolio_value_usd": equity, "cash_usd": cash,
        "positions_value_usd": pv, "open_positions": open_rows, "realized_pnl_usd": realized,
        "unrealized_pnl_usd": unreal, "total_pnl_usd": equity - acct["start_balance_usd"],
        "closed_trades": len(closed), "wins": wins, "losses": len(closed) - wins,
        "win_rate_pct": (100.0 * wins / len(closed)) if closed else None,
        "total_fills": fees[2], "total_fees_usd": fees[0], "total_slippage_usd": fees[1],
        "max_drawdown_pct": mdd * 100, "max_drawdown_usd": mdd_usd, "equity_snapshots": len(eqs),
        "latest_scan": dict(last_scan) if last_scan else None, "last_run": dict(last_run) if last_run else None,
        "runs_total": con.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "overlap_skips": con.execute("SELECT COUNT(*) FROM runs WHERE status='skipped_overlap'").fetchone()[0],
        "errors_count": con.execute("SELECT COUNT(*) FROM errors").fetchone()[0],
        "skips_count": con.execute("SELECT COUNT(*) FROM skips").fetchone()[0],
        "skips_by_stage": {r[0]: r[1] for r in con.execute("SELECT stage, COUNT(*) FROM skips GROUP BY stage")},
        "recent_errors": [dict(r) for r in con.execute("SELECT ts, stage, message FROM errors ORDER BY id DESC LIMIT 10")],
        "recent_evaluations": [dict(r) for r in con.execute(
            "SELECT ts, symbol, token_address, price_usd, liquidity_usd, safety_passed, safety_detail, score, decision, source_url"
            " FROM evaluations ORDER BY id DESC LIMIT 15")],
        "trade_history": trades, "schedule": schedule,
        "recent_runs": [dict(r) for r in con.execute(
            "SELECT id, started_at, finished_at, status, trigger, event, github_run_id, github_run_url, scheduled_for,"
            " queued_at, job_started_at, start_delay_sec, error_summary FROM runs ORDER BY id DESC LIMIT 25")],
        "problem_runs": [dict(r) for r in con.execute(
            "SELECT id, started_at, status, trigger, event, github_run_url, error_summary FROM runs"
            " WHERE status != 'ok' ORDER BY id DESC LIMIT 10")],
        "last_github_run": (lambda r: dict(r) if r else None)(con.execute(
            "SELECT id, started_at, finished_at, status, event, github_run_id, github_run_url, scheduled_for, start_delay_sec,"
            " error_summary FROM runs WHERE trigger='github-actions' ORDER BY id DESC LIMIT 1").fetchone()),
    }
    con.close()
    return out

def text(d):
    f = lambda x: "n/a" if x is None else f"${x:,.4f}"
    L = [f"=== Solana Paper Bot 2.0 — {d['mode']} ===",
         f"Portfolio value: {f(d['portfolio_value_usd'])}  (start {f(d['start_balance_usd'])}, total P/L {f(d['total_pnl_usd'])})",
         f"Cash: {f(d['cash_usd'])}   Positions value: {f(d['positions_value_usd'])}",
         f"Realized P/L: {f(d['realized_pnl_usd'])}   Unrealized P/L: {f(d['unrealized_pnl_usd'])}",
         f"Closed trades: {d['closed_trades']}  Win rate: {'n/a' if d['win_rate_pct'] is None else f'{d["win_rate_pct"]:.1f}%'}  Fills: {d['total_fills']}",
         f"Fees: {f(d['total_fees_usd'])}  Est. slippage: {f(d['total_slippage_usd'])}  Max drawdown: {d['max_drawdown_pct']:.2f}%",
         f"Latest scan: {d['latest_scan']['ts'] if d['latest_scan'] else 'never'}   Runs: {d['runs_total']} (overlap skips {d['overlap_skips']})",
         f"Errors: {d['errors_count']}  Skips: {d['skips_count']} {d['skips_by_stage']}",
         f"Schedule: {d['schedule'].get('note')}", "Open positions:"]
    for p in d["open_positions"]:
        L.append(f"  {p['symbol']} {p['token_address']} qty {p['qty']:.4f} entry {p['entry_mid_price']:.8g} last {p['last_price']:.8g} uPnL {f(p['unrealized_pnl_usd'])}")
    if not d["open_positions"]:
        L.append("  (none)")
    L.append("Trade history:")
    for t in d["trade_history"]:
        L.append(f"  #{t['id']} {t['ts']} {t['side']} {t['symbol']} mid {t['mid_price']:.8g} fill {t['fill_price']:.8g} net {f(t['net_usd'])}"
                 + (f" {t['exit_reason']} PnL {f(t['realized_pnl_usd'])}" if t['side'] == 'SELL' else ""))
    if not d["trade_history"]:
        L.append("  (none)")
    return "\n".join(L)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--json", action="store_true"); ap.add_argument("--db"); a = ap.parse_args()
    d = build(a.db)
    if not a.db:
        json.dump(d, open(os.path.join(config.BASE_DIR, "dashboard.json"), "w"), indent=2, default=str)
    print(json.dumps(d, indent=2, default=str) if a.json else text(d))
