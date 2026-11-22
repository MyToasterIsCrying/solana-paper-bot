"""ANALYST agent: 0-100 score.

score = momentum(30) + volume(25) + liquidity(20) + order_flow(15) + safety(10)
  momentum  = 15*clamp(chg_m5/5) * (0.5 if chg_m5>20 else 1)        # rising now, penalise blow-off spikes
            + 15*clamp(chg_h1/30) * (0.5 if chg_h1>150 else 1)
  volume    = 12.5*clamp(log10(vol_h1/5000)/2)                        # $5k ->0, $500k -> full
            + 12.5*clamp((vol_m5*12/vol_h1 - 0.5)/1.5)                 # 5m volume acceleration vs 1h pace
  liquidity = 20*clamp(log10(liq/25000)/log10(20))                    # $25k ->0, $500k -> full
  order_flow= 15*clamp((buys_h1/(buys_h1+sells_h1) - 0.45)/0.20)      # 45% buys ->0, 65% -> full
  safety    = 10 - 2*warn_flags - 3*(top10>20%) - 3*(vol_h24/liq>50, wash-trading hint), floor 0
Hard gates (no entry regardless of score): chg_m5 <= 0 or chg_h1 <= 0.
Entry when score >= ENTRY_SCORE_THRESHOLD (65).
"""
import math
import config

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

def score(m, safety):
    m5 = m.get("chg_m5") or 0; h1 = m.get("chg_h1") or 0
    vol_h1 = m.get("vol_h1") or 0; vol_m5 = m.get("vol_m5") or 0; vol_h24 = m.get("vol_h24") or 0
    liq = m.get("liquidity_usd") or 0
    buys = m.get("buys_h1") or 0; sells = m.get("sells_h1") or 0
    mom = 15 * clamp(m5 / 5) * (0.5 if m5 > 20 else 1) + 15 * clamp(h1 / 30) * (0.5 if h1 > 150 else 1)
    vol = 12.5 * clamp(math.log10(max(vol_h1, 1) / 5000) / 2)
    if vol_h1 > 0:
        vol += 12.5 * clamp((vol_m5 * 12 / vol_h1 - 0.5) / 1.5)
    liqs = 20 * clamp(math.log10(max(liq, 1) / 25000) / math.log10(20))
    flow = 0.0
    if buys + sells:
        flow = 15 * clamp((buys / (buys + sells) - 0.45) / 0.20)
    saf = 10 - 2 * (safety.get("warn_count") or 0) - 3 * ((safety.get("top10_pct_ex_pools") or 0) > 20) \
        - 3 * (liq > 0 and vol_h24 / liq > 50)
    saf = max(0, saf)
    total = round(mom + vol + liqs + flow + saf, 1)
    gates = []
    if m5 <= 0: gates.append("5m price change not positive")
    if h1 <= 0: gates.append("1h price change not positive")
    detail = {"momentum": round(mom, 1), "volume": round(vol, 1), "liquidity": round(liqs, 1),
              "order_flow": round(flow, 1), "safety": saf, "total": total, "gates_failed": gates,
              "threshold": config.ENTRY_SCORE_THRESHOLD}
    entry = total >= config.ENTRY_SCORE_THRESHOLD and not gates
    return total, entry, detail
