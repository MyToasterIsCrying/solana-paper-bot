"""SAFETY agent: token age, liquidity, mint/freeze authority, holder concentration, RugCheck risk flags.
If verifiable safety data can't be fetched -> REJECT."""
import json, time, datetime
import config, db

def _age_min(pair_created_ms, now_s=None):
    if not pair_created_ms:
        return None
    return ((now_s or time.time()) - pair_created_ms / 1000.0) / 60.0

def get_report(con, md, mint, now=None):
    row = con.execute("SELECT fetched_at, source_url, report FROM safety_cache WHERE token_address=?", (mint,)).fetchone()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if row:
        age = (now - datetime.datetime.fromisoformat(row["fetched_at"])).total_seconds() / 60
        if age <= config.SAFETY_CACHE_MIN:
            return json.loads(row["report"]), row["source_url"], row["fetched_at"], True
    rep, url, ts = md.rugcheck(mint)
    con.execute("INSERT OR REPLACE INTO safety_cache VALUES (?,?,?,?)", (mint, ts, url, json.dumps(rep)))
    return rep, url, ts, False

def check(con, md, cand, now_s=None):
    """Return (passed: bool, detail: dict). detail['reasons'] lists every failed rule."""
    m = cand["m"]; reasons = []; d = {"reasons": reasons}
    age = _age_min(m.get("pair_created_at"), now_s)
    d["pair_age_min"] = round(age, 1) if age is not None else None
    if age is None:
        reasons.append("pair creation time unknown")
    elif age < config.MIN_TOKEN_AGE_MIN:
        reasons.append(f"pair age {age:.0f}m < {config.MIN_TOKEN_AGE_MIN}m")
    if (m.get("liquidity_usd") or 0) < config.MIN_LIQUIDITY_USD:
        reasons.append("liquidity below floor")
    try:
        rep, url, ts, cached = get_report(con, md, cand["token_address"])
    except Exception as e:
        reasons.append(f"RugCheck report unavailable ({e}) -> insufficient verifiable safety info")
        return False, d
    d.update({"rugcheck_url": url, "rugcheck_fetched_at": ts, "rugcheck_cached": cached})
    if not isinstance(rep, dict) or "mintAuthority" not in rep or "freezeAuthority" not in rep:
        reasons.append("RugCheck report missing authority fields -> insufficient info")
        return False, d
    d["mint_authority"] = rep.get("mintAuthority"); d["freeze_authority"] = rep.get("freezeAuthority")
    if rep.get("mintAuthority"):
        reasons.append("mint authority NOT revoked")
    if rep.get("freezeAuthority"):
        reasons.append("freeze authority NOT revoked")
    if rep.get("rugged"):
        reasons.append("RugCheck marks token as rugged")
    risks = rep.get("risks") or []
    d["risks"] = [f"{r.get('level')}:{r.get('name')}" for r in risks]
    for r in risks:
        if r.get("level") == "danger":
            reasons.append(f"RugCheck danger: {r.get('name')}")
    d["rugcheck_score_norm"] = rep.get("score_normalised")
    if rep.get("score_normalised") is not None and rep["score_normalised"] > config.MAX_RUGCHECK_SCORE_NORM:
        reasons.append(f"RugCheck normalised risk score {rep['score_normalised']} > {config.MAX_RUGCHECK_SCORE_NORM}")
    tf = (rep.get("transferFee") or {}).get("pct") or 0
    if tf > 0:
        reasons.append(f"transfer fee {tf}%")
    holders = rep.get("topHolders")
    if not holders:
        reasons.append("top-holder data unavailable -> insufficient info")
    else:
        known = rep.get("knownAccounts") or {}
        amm = {k for k, v in known.items() if (v or {}).get("type") in ("AMM", "LOCKER")}
        market_accts = set()
        for mk in rep.get("markets") or []:
            for k in ("pubkey", "liquidityAAccount", "liquidityBAccount"):
                if isinstance(mk.get(k), str):
                    market_accts.add(mk[k])
        non_pool = [h for h in holders if h.get("owner") not in amm and h.get("address") not in amm
                    and h.get("address") not in market_accts and h.get("owner") not in market_accts]
        top1 = max([h.get("pct") or 0 for h in non_pool], default=0)
        top10 = sum(sorted([h.get("pct") or 0 for h in non_pool], reverse=True)[:10])
        d["top_holder_pct_ex_pools"] = round(top1, 2); d["top10_pct_ex_pools"] = round(top10, 2)
        d["insider_holders"] = sum(1 for h in non_pool if h.get("insider"))
        if top1 > config.MAX_SINGLE_HOLDER_PCT:
            reasons.append(f"single holder {top1:.1f}% > {config.MAX_SINGLE_HOLDER_PCT}%")
        if top10 > config.MAX_TOP10_HOLDERS_PCT:
            reasons.append(f"top-10 holders {top10:.1f}% > {config.MAX_TOP10_HOLDERS_PCT}%")
    d["warn_count"] = sum(1 for r in risks if r.get("level") == "warn")
    d["total_holders"] = rep.get("totalHolders")
    return len(reasons) == 0, d
