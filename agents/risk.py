"""RISK agent: position sizing, limits, cost model (fees + spread + slippage), exit thresholds."""
import datetime
import config, db

def slippage_pct(trade_usd, liquidity_usd):
    """Half-spread + baseline slippage + constant-product price impact (trade / (liq/2))."""
    impact = config.IMPACT_MULTIPLIER * trade_usd / liquidity_usd if liquidity_usd else 1.0
    return config.HALF_SPREAD_PCT + config.BASE_SLIPPAGE_PCT + impact

def simulate_buy(budget_usd, mid_price, liquidity_usd):
    """All-in spend budget_usd. Returns dict with qty, fill price, fee and slippage cost (USD)."""
    swap_in = budget_usd - config.NETWORK_FEE_USD
    dex_fee = swap_in * config.DEX_FEE_PCT
    after_fee = swap_in - dex_fee
    slip = slippage_pct(after_fee, liquidity_usd)
    fill = mid_price * (1 + slip)
    qty = after_fee / fill
    slippage_usd = after_fee - qty * mid_price          # value lost vs mid
    return {"qty": qty, "fill_price": fill, "fee_usd": config.NETWORK_FEE_USD + dex_fee,
            "slippage_usd": slippage_usd, "slippage_pct": slip, "cost_usd": budget_usd}

def simulate_sell(qty, mid_price, liquidity_usd):
    value_mid = qty * mid_price
    slip = slippage_pct(value_mid, liquidity_usd)
    fill = mid_price * (1 - slip)
    gross = qty * fill
    dex_fee = gross * config.DEX_FEE_PCT
    proceeds = gross - dex_fee - config.NETWORK_FEE_USD
    return {"fill_price": fill, "gross_usd": gross, "proceeds_usd": proceeds,
            "fee_usd": dex_fee + config.NETWORK_FEE_USD, "slippage_usd": value_mid - gross, "slippage_pct": slip}

def equity(con):
    cash = db.get_cash(con)
    pv = sum((p["last_price"] or p["entry_mid_price"]) * p["qty"] for p in db.open_positions(con))
    return cash + pv, cash, pv

def approve_entry(con, cand, now=None):
    """Return (approved, reason, budget_usd)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    opens = db.open_positions(con)
    if len(opens) >= config.MAX_OPEN_POSITIONS:
        return False, f"max {config.MAX_OPEN_POSITIONS} open positions reached", 0
    if any(p["token_address"] == cand["token_address"] for p in opens):
        return False, "already holding this token (no averaging down / no adding)", 0
    cutoff = (now - datetime.timedelta(minutes=config.REENTRY_COOLDOWN_MIN)).isoformat(timespec="seconds")
    if con.execute("SELECT 1 FROM positions WHERE token_address=? AND status='closed' AND exit_time>=?",
                   (cand["token_address"], cutoff)).fetchone():
        return False, f"re-entry cooldown ({config.REENTRY_COOLDOWN_MIN}m) after recent exit", 0
    eq, cash, _ = equity(con)
    budget = round(min(config.MAX_POSITION_PCT * eq, cash), 6)   # no leverage: never more than cash
    if budget < config.MIN_POSITION_USD:
        return False, f"position budget ${budget:.2f} < min ${config.MIN_POSITION_USD:.2f}", 0
    return True, "", budget

def exit_signal(entry_mid, current_mid):
    chg = current_mid / entry_mid - 1
    if chg <= -config.STOP_LOSS_PCT:
        return "stop_loss", chg
    if chg >= config.TAKE_PROFIT_PCT:
        return "take_profit", chg
    return None, chg
