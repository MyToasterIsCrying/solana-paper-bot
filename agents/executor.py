"""EXECUTOR agent: simulated fills only. Never submits real transactions; there is no wallet or key anywhere in this code."""
import json, datetime
import config, db
from agents import risk

def _age_sec(fetched_at, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.datetime.fromisoformat(fetched_at)).total_seconds()

def quote_is_fresh(q, now=None):
    if not q or not q.get("priceUsd") or not q.get("_fetched_at"):
        return False, "missing price or fetch timestamp"
    age = _age_sec(q["_fetched_at"], now)
    if age > config.MAX_DATA_AGE_SEC:
        return False, f"quote is {age:.0f}s old (> {config.MAX_DATA_AGE_SEC}s)"
    return True, ""

def open_position(con, run_id, cand, budget, q, score):
    mid = float(q["priceUsd"]); liq = (q.get("liquidity") or {}).get("usd")
    b = risk.simulate_buy(budget, mid, liq)
    ts = db.now_iso()
    cur = con.cursor()
    try:
        cur.execute("UPDATE account SET cash_usd = cash_usd - ? WHERE id=1 AND cash_usd >= ?", (budget, budget))
        if cur.rowcount != 1:
            raise RuntimeError("insufficient virtual cash")
        cur.execute("""INSERT INTO positions(run_id,token_address,pair_address,symbol,name,dex_id,status,entry_time,
            entry_mid_price,entry_fill_price,qty,cost_usd,entry_fee_usd,entry_slippage_usd,stop_price,target_price,score,
            entry_source_url,last_price,last_price_time,last_price_source) VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, cand["token_address"], q["pairAddress"], cand["symbol"], cand.get("name"), q.get("dexId"), ts,
             mid, b["fill_price"], b["qty"], budget, b["fee_usd"], b["slippage_usd"],
             mid * (1 - config.STOP_LOSS_PCT), mid * (1 + config.TAKE_PROFIT_PCT), score, q["_source_url"],
             mid, q["_fetched_at"], q["_source_url"]))
        pid = cur.lastrowid
        cur.execute("""INSERT INTO fills(position_id,run_id,side,ts,token_address,pair_address,mid_price,fill_price,qty,
            gross_usd,net_usd,fee_usd,slippage_usd,price_fetched_at,source_url,raw_pair) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, run_id, "BUY", ts, cand["token_address"], q["pairAddress"], mid, b["fill_price"], b["qty"],
             budget, -budget, b["fee_usd"], b["slippage_usd"], q["_fetched_at"], q["_source_url"], json.dumps(dict(q))))
        con.commit()
        return pid, b
    except Exception:
        con.rollback(); raise

def close_position(con, run_id, pos, q, reason):
    mid = float(q["priceUsd"]); liq = (q.get("liquidity") or {}).get("usd")
    s = risk.simulate_sell(pos["qty"], mid, liq)
    ts = db.now_iso(); pnl = s["proceeds_usd"] - pos["cost_usd"]
    cur = con.cursor()
    try:
        cur.execute("""UPDATE positions SET status='closed', exit_time=?, exit_mid_price=?, exit_fill_price=?, proceeds_usd=?,
            exit_fee_usd=?, exit_slippage_usd=?, realized_pnl_usd=?, exit_reason=?, exit_source_url=?, exit_run_id=?,
            last_price=?, last_price_time=?, last_price_source=? WHERE id=? AND status='open'""",
            (ts, mid, s["fill_price"], s["proceeds_usd"], s["fee_usd"], s["slippage_usd"], pnl, reason, q["_source_url"],
             run_id, mid, q["_fetched_at"], q["_source_url"], pos["id"]))
        if cur.rowcount != 1:
            raise RuntimeError(f"position {pos['id']} not open (duplicate exit prevented)")
        cur.execute("UPDATE account SET cash_usd = cash_usd + ? WHERE id=1", (s["proceeds_usd"],))
        cur.execute("""INSERT INTO fills(position_id,run_id,side,ts,token_address,pair_address,mid_price,fill_price,qty,
            gross_usd,net_usd,fee_usd,slippage_usd,price_fetched_at,source_url,raw_pair) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos["id"], run_id, "SELL", ts, pos["token_address"], pos["pair_address"], mid, s["fill_price"], pos["qty"],
             s["gross_usd"], s["proceeds_usd"], s["fee_usd"], s["slippage_usd"], q["_fetched_at"], q["_source_url"],
             json.dumps(dict(q))))
        con.commit()
        return pnl, s
    except Exception:
        con.rollback(); raise

def mark(con, pos, q):
    con.execute("UPDATE positions SET last_price=?, last_price_time=?, last_price_source=? WHERE id=?",
                (float(q["priceUsd"]), q["_fetched_at"], q["_source_url"], pos["id"]))
    con.commit()
