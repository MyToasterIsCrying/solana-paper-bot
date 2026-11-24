"""Network-only discovery worker for the Windows-local paper service."""
import sqlite3
import time

import config
from agents import analyst, safety, scanner
from agents.executor import quote_is_fresh
from marketdata import LiveMarketData


def discover_batch(cached_reports, open_tokens, md=None, now=None):
    """Fetch and evaluate candidates without opening a production ledger connection."""
    md = md or LiveMarketData()
    now = now or time.monotonic
    cache = sqlite3.connect(":memory:")
    cache.row_factory = sqlite3.Row
    cache.execute("""CREATE TABLE safety_cache (
        token_address TEXT PRIMARY KEY, fetched_at TEXT, source_url TEXT, report TEXT)""")
    cache.executemany("INSERT INTO safety_cache VALUES (?,?,?,?)", cached_reports)

    started = now()
    discovered = scanner.scan(md, cache)
    stats = {"slots": config.MAX_SAFETY_CHECKS_PER_RUN, "attempted": 0, "complete": 0,
             "from_cache": 0, "passed": 0, "failed_fetch": 0, "rate_limited": 0,
             "stopped_early": None}
    evaluations = []
    api_failures = []
    open_tokens = set(open_tokens)

    for candidate in discovered["passed"]:
        addr = candidate["token_address"]
        row = {"candidate": candidate, "safety_passed": False, "safety_detail": {},
               "score": None, "score_detail": {}, "decision": None, "entry_quote": None}
        if addr in open_tokens:
            row["decision"] = "already_holding"
        elif stats["attempted"] >= config.MAX_SAFETY_CHECKS_PER_RUN:
            row["decision"] = "outside_safety_slots"
        elif stats["stopped_early"]:
            row["decision"] = "safety_stopped"
        elif now() - started > config.SAFETY_PHASE_DEADLINE_SEC:
            stats["stopped_early"] = f"cycle time budget {config.SAFETY_PHASE_DEADLINE_SEC}s reached"
            row["decision"] = "safety_stopped"
        else:
            stats["attempted"] += 1
            passed, detail = safety.check(cache, md, candidate)
            for reason in detail.get("reasons", []):
                if "report unavailable" in reason:
                    api_failures.append({
                        "stage": "safety",
                        "message": f"{candidate['symbol']} {addr}: {reason}",
                    })
            row["safety_passed"] = passed
            row["safety_detail"] = detail
            if "mint_authority" in detail and "top10_pct_ex_pools" in detail:
                stats["complete"] += 1
                stats["from_cache"] += int(bool(detail.get("rugcheck_cached")))
            else:
                stats["failed_fetch"] += 1
                if "429" in " ".join(detail.get("reasons", [])):
                    stats["rate_limited"] += 1
                    if stats["rate_limited"] >= config.RUGCHECK_MAX_429:
                        stats["stopped_early"] = "RugCheck rate limit (429) hit repeatedly"
            stats["passed"] += int(passed)
            if passed:
                score, entry, score_detail = analyst.score(candidate["m"], detail)
                row["score"] = score
                row["score_detail"] = score_detail
                row["decision"] = "entry_candidate" if entry else "below_threshold_or_gate"
                if entry:
                    try:
                        quote = md.quote_pair(addr, candidate["pair_address"], retries=0)
                    except Exception as exc:
                        row["decision"] = "skip_quote_failure"
                        row["quote_error"] = str(exc)
                        api_failures.append({
                            "stage": "executor",
                            "message": f"{candidate['symbol']} {addr}: entry quote failed: {exc}",
                        })
                    else:
                        fresh, reason = quote_is_fresh(quote)
                        if not fresh or not (quote.get("liquidity") or {}).get("usd"):
                            row["decision"] = "skip_stale"
                            row["quote_error"] = reason or "no liquidity"
                        else:
                            row["entry_quote"] = dict(quote)
            else:
                row["decision"] = "reject_safety"
        evaluations.append(row)

    cache_updates = [tuple(row) for row in cache.execute(
        "SELECT token_address,fetched_at,source_url,report FROM safety_cache")]
    cache.close()
    return {
        "sources": discovered["sources"],
        "tokens_seen": discovered["tokens_seen"],
        "passed_count": len(discovered["passed"]),
        "candidates": evaluations,
        "rejected": discovered["rejected"],
        "errors": discovered["errors"],
        "api_failures": api_failures,
        "safety": stats,
        "cache_updates": cache_updates,
        "api_stats": getattr(md, "stats", None),
    }


def discover_worker(result_queue, cached_reports, open_tokens):
    try:
        result_queue.put({"result": discover_batch(cached_reports, open_tokens)})
    except Exception as exc:
        result_queue.put({"error": f"{type(exc).__name__}: {exc}"})
