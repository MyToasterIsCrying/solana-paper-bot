"""SCANNER agent: discover Solana tokens from DEX Screener and pre-filter/rank by liquidity, volume, momentum, txns."""
import math
import config

def metrics(q):
    liq = (q.get("liquidity") or {}).get("usd")
    vol = q.get("volume") or {}; pc = q.get("priceChange") or {}; tx = q.get("txns") or {}
    h1 = tx.get("h1") or {}; m5 = tx.get("m5") or {}
    return {
        "price_usd": float(q["priceUsd"]) if q.get("priceUsd") else None,
        "liquidity_usd": liq, "vol_m5": vol.get("m5"), "vol_h1": vol.get("h1"), "vol_h6": vol.get("h6"), "vol_h24": vol.get("h24"),
        "chg_m5": pc.get("m5"), "chg_h1": pc.get("h1"), "chg_h24": pc.get("h24"),
        "buys_h1": h1.get("buys"), "sells_h1": h1.get("sells"), "buys_m5": m5.get("buys"), "sells_m5": m5.get("sells"),
        "pair_created_at": q.get("pairCreatedAt"), "fdv": q.get("fdv"), "market_cap": q.get("marketCap"),
    }

def prefilter(q):
    """Return (ok, reason, metrics)."""
    m = metrics(q)
    if m["price_usd"] is None or m["price_usd"] <= 0:
        return False, "no priceUsd in DEX Screener response", m
    if not m["liquidity_usd"]:
        return False, "no liquidity data (e.g. bonding-curve pair)", m
    if m["liquidity_usd"] < config.MIN_LIQUIDITY_USD:
        return False, f"liquidity ${m['liquidity_usd']:,.0f} < ${config.MIN_LIQUIDITY_USD:,}", m
    if (m["vol_h1"] or 0) < config.MIN_VOLUME_H1_USD:
        return False, f"1h volume ${m['vol_h1'] or 0:,.0f} < ${config.MIN_VOLUME_H1_USD:,}", m
    tx = (m["buys_h1"] or 0) + (m["sells_h1"] or 0)
    if tx < config.MIN_TXNS_H1:
        return False, f"1h txns {tx} < {config.MIN_TXNS_H1}", m
    return True, "", m

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

def rank_score(m, holder=None):
    """Priority for the limited safety-check slots (0-100, higher first). Does NOT affect safety rules or entry score.
      liquidity  30 * clamp(log10(liq/25k)/log10(40))            verified DEX Screener liquidity, $25k->0, $1M->full
      vol accel  20 * clamp((vol_m5*12/vol_h1 - 0.5)/1.5)        5m pace vs 1h
               + 10 * clamp((vol_h1*6/vol_h6 - 0.8)/1.2)          1h pace vs 6h
      momentum   25 if chg_m5>0 and chg_h1>0, 10 if only one is positive, else 0
               +  5 * clamp(chg_h1/30)
      holders    +10 if a cached RugCheck report (<=30 min) shows holders within limits,
                 -50 if it shows a known holder-limit breach (would fail safety anyway), 0 if unknown
    """
    liq = m.get("liquidity_usd") or 0
    vh1 = m.get("vol_h1") or 0; vm5 = m.get("vol_m5") or 0; vh6 = m.get("vol_h6") or 0
    m5 = m.get("chg_m5") or 0; h1 = m.get("chg_h1") or 0
    sc = 30 * clamp(math.log10(max(liq, 1) / 25000) / math.log10(40))
    if vh1 > 0:
        sc += 20 * clamp((vm5 * 12 / vh1 - 0.5) / 1.5)
    if vh6 > 0:
        sc += 10 * clamp((vh1 * 6 / vh6 - 0.8) / 1.2)
    sc += 25 if (m5 > 0 and h1 > 0) else (10 if (m5 > 0 or h1 > 0) else 0)
    sc += 5 * clamp(h1 / 30)
    if holder is not None:
        sc += -50 if holder.get("breach") else 10
    return round(sc, 2)

def cached_holder_info(con, addr):
    """Cheap holder-distribution hint from a RugCheck report already cached (no API call)."""
    if con is None:
        return None
    import json, datetime
    row = con.execute("SELECT fetched_at, report FROM safety_cache WHERE token_address=?", (addr,)).fetchone()
    if not row:
        return None
    age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(row[0])).total_seconds() / 60
    if age > config.SAFETY_CACHE_MIN:
        return None
    rep = json.loads(row[1]) or {}
    holders = rep.get("topHolders") or []
    if not holders:
        return None
    known = rep.get("knownAccounts") or {}
    amm = {k for k, v in known.items() if (v or {}).get("type") in ("AMM", "LOCKER")}
    pcts = sorted([h.get("pct") or 0 for h in holders if h.get("owner") not in amm and h.get("address") not in amm], reverse=True)
    top1 = pcts[0] if pcts else 0; top10 = sum(pcts[:10])
    return {"top1": top1, "top10": top10,
            "breach": top1 > config.MAX_SINGLE_HOLDER_PCT or top10 > config.MAX_TOP10_HOLDERS_PCT}

def scan(md, con=None):
    disc = md.discover()
    addrs, sources, errors = disc[0], disc[1], disc[2]
    by_source = disc[3] if len(disc) > 3 else {}
    # dedupe by exact contract address before anything else (fixtures may return raw lists)
    seen, uniq = set(), []
    for a in addrs:
        a = (a or "").strip()
        if a and a not in seen and a not in config.QUOTE_WHITELIST:
            seen.add(a); uniq.append(a)
    addrs = uniq
    quotes = md.pairs_for_tokens(addrs) if addrs else {}
    passed, rejected = [], []
    for addr in addrs:
        q = quotes.get(addr)
        if q is None:
            rejected.append((addr, None, "no Solana pair with SOL/USDC/USDT quote on DEX Screener")); continue
        ok, reason, m = prefilter(q)
        sym = q.get("baseToken", {}).get("symbol")
        if ok and (sym or "").upper() in config.NON_MEME_SYMBOLS:
            ok, reason = False, f"not a memecoin (stablecoin/wrapped/LST symbol {sym})"
        if ok:
            holder = cached_holder_info(con, addr)
            passed.append({"token_address": addr, "symbol": sym, "name": q["baseToken"].get("name"),
                           "pair_address": q["pairAddress"], "dex_id": q.get("dexId"), "quote": q, "m": m,
                           "holder_hint": holder, "rank": rank_score(m, holder), "found_by": by_source.get(addr, [])})
        else:
            rejected.append((addr, sym, reason))
    passed.sort(key=lambda c: c["rank"], reverse=True)
    return {"tokens_seen": len(addrs), "sources": sources, "errors": errors, "passed": passed, "rejected": rejected}
