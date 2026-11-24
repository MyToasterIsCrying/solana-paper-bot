#!/usr/bin/env python3
"""Solana Paper Bot 2.0 -- one full cycle: monitor exits -> scan -> safety -> analyst -> risk -> executor -> snapshot.
PAPER TRADING ONLY. Usage: python3 run_cycle.py [--trigger manual|cron]"""
import sys, os, json, argparse, traceback, time
import config, db
from platform_lock import acquire_lock as _acquire_lock, release_lock
from agents import scanner, safety, analyst, risk, executor

def acquire_lock(path):
    return _acquire_lock(path)

def monitor_positions(con, md, run_id, summary, quote_retries=None):
    for pos in db.open_positions(con):
        quote_started = time.monotonic()
        try:
            if quote_retries is None:
                q = md.quote_pair(pos["token_address"], pos["pair_address"])
            else:
                q = md.quote_pair(pos["token_address"], pos["pair_address"], retries=quote_retries)
        except Exception as e:
            latency = round(time.monotonic() - quote_started, 3)
            db.log_error(con, run_id, "monitor", f"{pos['symbol']} {pos['token_address']}: quote failed: {e}"); con.commit()
            summary["monitor"].append({
                "symbol": pos["symbol"], "action": "hold (no quote)",
                "quote_latency_sec": latency,
            }); continue
        latency = round(time.monotonic() - quote_started, 3)
        ok, why = executor.quote_is_fresh(q)
        if not ok:
            db.log_error(con, run_id, "monitor", f"{pos['symbol']}: no fresh verified price ({why}); holding, exit re-checked next cycle")
            con.commit(); summary["monitor"].append({
                "symbol": pos["symbol"], "action": f"hold ({why})",
                "quote_latency_sec": latency,
            }); continue
        sig, chg = risk.exit_signal(pos["entry_mid_price"], float(q["priceUsd"]))
        if sig:
            pnl, s = executor.close_position(con, run_id, pos, q, sig)
            summary["exits"].append({"symbol": pos["symbol"], "token": pos["token_address"], "reason": sig,
                                     "exit_mid": float(q["priceUsd"]), "chg_pct": round(chg * 100, 2), "pnl_usd": round(pnl, 4),
                                     "source": q["_source_url"], "fetched_at": q["_fetched_at"],
                                     "quote_latency_sec": latency})
        else:
            executor.mark(con, pos, q)
            summary["monitor"].append({
                "symbol": pos["symbol"], "action": "hold", "chg_pct": round(chg * 100, 2),
                "quote_latency_sec": latency,
            })

def scan_and_enter(con, md, run_id, summary):
    sc = scanner.scan(md, con)
    for e in sc["errors"]:
        db.log_error(con, run_id, "scanner", e)
    for addr, sym, reason in sc["rejected"]:
        db.log_skip(con, run_id, addr, sym, "scanner", reason)
    con.execute("INSERT INTO scans(run_id,ts,sources,tokens_seen,candidates_passed_prefilter,candidates) VALUES (?,?,?,?,?,?)",
                (run_id, db.now_iso(), db.j(sc["sources"]), sc["tokens_seen"], len(sc["passed"]),
                 db.j([{k: c.get(k) for k in ("token_address", "symbol", "pair_address", "dex_id", "m", "rank", "found_by")}
                       for c in sc["passed"]])))
    con.commit()
    summary.update(tokens_seen_after_dedup=sc["tokens_seen"], prefilter_passed=len(sc["passed"]),
                   per_source={x.get("source", x["url"]): x.get("solana_tokens", 0) for x in sc["sources"]},
                   raw_discovered=sum(x.get("solana_tokens", 0) for x in sc["sources"]))
    stats = {"slots": config.MAX_SAFETY_CHECKS_PER_RUN, "attempted": 0, "complete": 0, "from_cache": 0,
             "passed": 0, "failed_fetch": 0, "rate_limited": 0, "stopped_early": None}
    summary["safety"] = stats
    t_start = summary.get("_t0") or time.time()
    for c in sc["passed"]:
        if any(p["token_address"] == c["token_address"] for p in db.open_positions(con)):
            db.log_skip(con, run_id, c["token_address"], c["symbol"], "risk", "already holding"); continue
        if stats["attempted"] >= config.MAX_SAFETY_CHECKS_PER_RUN:
            db.log_skip(con, run_id, c["token_address"], c["symbol"], "scanner",
                        f"not safety-checked: outside top-{config.MAX_SAFETY_CHECKS_PER_RUN} slots (rank {c['rank']})"); continue
        if stats["stopped_early"]:
            db.log_skip(con, run_id, c["token_address"], c["symbol"], "safety",
                        f"not safety-checked ({stats['stopped_early']}) -> not eligible"); continue
        if time.time() - t_start > config.SAFETY_PHASE_DEADLINE_SEC:
            stats["stopped_early"] = f"cycle time budget {config.SAFETY_PHASE_DEADLINE_SEC}s reached"
            db.log_skip(con, run_id, c["token_address"], c["symbol"], "safety", f"not safety-checked ({stats['stopped_early']})"); continue
        stats["attempted"] += 1
        q = c["quote"]; ev = {"symbol": c["symbol"], "token": c["token_address"], "pair": c["pair_address"],
                              "price_usd": c["m"]["price_usd"], "liquidity_usd": c["m"]["liquidity_usd"]}
        if any(p["token_address"] == c["token_address"] for p in db.open_positions(con)):
            db.log_skip(con, run_id, c["token_address"], c["symbol"], "risk", "already holding"); continue
        passed, sd = safety.check(con, md, c); con.commit()
        if "mint_authority" in sd and "top10_pct_ex_pools" in sd:   # report fetched with authorities + holders
            stats["complete"] += 1; stats["from_cache"] += int(bool(sd.get("rugcheck_cached")))
        else:
            stats["failed_fetch"] += 1
            if "429" in " ".join(sd.get("reasons", [])):
                stats["rate_limited"] += 1
                if stats["rate_limited"] >= config.RUGCHECK_MAX_429:
                    stats["stopped_early"] = "RugCheck rate limit (429) hit repeatedly"
        stats["passed"] += int(passed)
        sc_total, entry, sdet = analyst.score(c["m"], sd) if passed else (None, False, {})
        decision = "reject_safety" if not passed else ("entry_candidate" if entry else "below_threshold_or_gate")
        if not passed:
            db.log_skip(con, run_id, c["token_address"], c["symbol"], "safety", "; ".join(sd["reasons"]))
        elif not entry:
            db.log_skip(con, run_id, c["token_address"], c["symbol"], "analyst",
                        f"score {sc_total} (threshold {config.ENTRY_SCORE_THRESHOLD}) gates={sdet['gates_failed']}")
        else:
            ok, why, budget = risk.approve_entry(con, c)
            if not ok:
                decision = "reject_risk"; db.log_skip(con, run_id, c["token_address"], c["symbol"], "risk", why)
            else:
                # fresh verified quote for the exact pair right before the simulated fill
                try:
                    fq = md.quote_pair(c["token_address"], c["pair_address"])
                except Exception as e:
                    fq = None; db.log_error(con, run_id, "executor", f"entry re-quote failed: {e}")
                fresh, fwhy = executor.quote_is_fresh(fq)
                if not fresh or not (fq.get("liquidity") or {}).get("usd"):
                    decision = "skip_stale"; db.log_skip(con, run_id, c["token_address"], c["symbol"], "executor",
                                                          f"no fresh verified quote: {fwhy or 'no liquidity'}")
                else:
                    try:
                        pid, b = executor.open_position(con, run_id, c, budget, fq, sc_total)
                        decision = "BUY"
                        summary["entries"].append({"symbol": c["symbol"], "token": c["token_address"],
                            "pair": fq["pairAddress"], "mid_price": float(fq["priceUsd"]), "fill_price": b["fill_price"],
                            "qty": b["qty"], "cost_usd": budget, "fee_usd": b["fee_usd"], "slippage_usd": b["slippage_usd"],
                            "score": sc_total, "source": fq["_source_url"], "fetched_at": fq["_fetched_at"]})
                    except Exception as e:  # e.g. UNIQUE index => duplicate prevented
                        decision = "skip_duplicate_or_error"
                        db.log_skip(con, run_id, c["token_address"], c["symbol"], "executor", f"entry not executed: {e}")
        con.execute("""INSERT INTO evaluations(run_id,ts,token_address,pair_address,symbol,price_usd,liquidity_usd,safety_passed,
            safety_detail,score,score_detail,decision,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, db.now_iso(), c["token_address"], c["pair_address"], c["symbol"], c["m"]["price_usd"],
             c["m"]["liquidity_usd"], int(passed), db.j(sd), sc_total, db.j(sdet), decision, q["_source_url"]))
        con.commit()
        ev.update(rank=c["rank"], found_by=c.get("found_by"), safety_passed=passed, safety_reasons=sd.get("reasons"), score=sc_total, score_detail=sdet, decision=decision)
        summary["evaluated"].append(ev)

def snapshot(con, run_id):
    eq, cash, pv = risk.equity(con)
    con.execute("INSERT INTO equity_snapshots(run_id,ts,cash_usd,positions_value_usd,equity_usd) VALUES (?,?,?,?,?)",
                (run_id, db.now_iso(), cash, pv, eq))
    con.commit()
    return eq, cash, pv

def run(db_path=None, md=None, trigger="manual", lock_path=None):
    lock = acquire_lock(lock_path or config.LOCK_PATH)
    if lock is None:
        return {"status": "skipped_overlap", "run_id": None}
    con = db.connect(db_path)
    if md is None:
        from marketdata import LiveMarketData
        md = LiveMarketData()
    cur = con.execute("INSERT INTO runs(started_at,status,trigger) VALUES (?,?,?)", (db.now_iso(), "running", trigger))
    run_id = cur.lastrowid
    import datetime as _dt
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).isoformat(timespec="seconds")
    con.execute("DELETE FROM safety_cache WHERE fetched_at < ?", (cutoff,))   # keep ledger small; cache is not history
    con.commit()
    summary = {"_t0": time.time(), "run_id": run_id, "data": getattr(md, "label", "?"), "exits": [], "monitor": [], "entries": [], "evaluated": []}
    status = "ok"
    try:
        monitor_positions(con, md, run_id, summary)
        scan_and_enter(con, md, run_id, summary)
    except Exception as e:
        status = "error"; db.log_error(con, run_id, "run", traceback.format_exc()); summary["error"] = str(e)
    try:
        eq, cash, pv = snapshot(con, run_id)
        summary.update(equity=eq, cash=cash, positions_value=pv)
    except Exception as e:
        status = "error"; db.log_error(con, run_id, "snapshot", str(e))
    summary["runtime_sec"] = round(time.time() - summary.pop("_t0"), 1)
    summary["api_stats"] = getattr(md, "stats", None)
    con.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?", (db.now_iso(), status, db.j(summary), run_id))
    con.commit(); con.close()
    release_lock(lock)
    summary["status"] = status
    return summary

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--trigger", default="manual"); a = ap.parse_args()
    out = run(trigger=a.trigger)
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out.get("status") in ("ok", "skipped_overlap") else 1)
