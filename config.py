"""Solana Paper Bot 2.0 -- configuration. PAPER TRADING ONLY: no wallets, keys or real transactions."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PAPERBOT_DB", os.path.join(BASE_DIR, "ledger.db"))
LOCK_PATH = os.environ.get("PAPERBOT_LOCK", os.path.join(BASE_DIR, "run.lock"))

# --- Virtual account ---
START_BALANCE_USD = 100.0
MAX_POSITION_PCT = 0.05        # of current equity, all-in (incl. fees)
MAX_OPEN_POSITIONS = 3
STOP_LOSS_PCT = 0.06           # measured on verified market (mid) price vs entry mid price
TAKE_PROFIT_PCT = 0.12
MIN_POSITION_USD = 1.00        # below this, fixed fees dominate -> skip
REENTRY_COOLDOWN_MIN = 120     # no re-entry into a token within 2h of exiting it
LEVERAGE = 1.0                 # never changed

# --- Cost model (applied to every simulated swap, recorded per fill) ---
NETWORK_FEE_USD = 0.02         # Solana base + priority fee estimate per swap
DEX_FEE_PCT = 0.0030           # LP/protocol swap fee (typical 0.25-0.30%; some pools higher)
HALF_SPREAD_PCT = 0.0025       # half of an assumed bid/ask spread vs quoted mid price
BASE_SLIPPAGE_PCT = 0.0050     # baseline execution slippage (latency, MEV, routing)
# price impact for constant-product pool: ~ trade_usd / (liquidity_usd/2)
IMPACT_MULTIPLIER = 2.0

# --- Data freshness ---
MAX_DATA_AGE_SEC = 180         # quotes older than this (by fetch time) are stale -> skip
HTTP_TIMEOUT = 15

# --- Scanner pre-filters ---
MIN_LIQUIDITY_USD = 25_000
MIN_VOLUME_H1_USD = 10_000
MIN_TXNS_H1 = 100
MAX_SAFETY_CHECKS_PER_RUN = 20 # complete safety checks per cycle (RugCheck; cached reports count as checks)
RUGCHECK_SPACING_SEC = 1.2     # RugCheck returns X-Rate-Limit-Limit: 15; 25 calls at 1.2s gave 0 x 429 (tested 2026-11-22)
RUGCHECK_MAX_429 = 2           # after this many 429s in one cycle, stop safety checks (remaining = skipped, never passed)
GECKO_SPACING_SEC = 4.0        # GeckoTerminal public API: 2.2s spacing still drew 429s (2026-11-22) -> <=10 calls/min
SAFETY_PHASE_DEADLINE_SEC = 150  # stop starting new safety checks after this many seconds into the cycle
DS_SEARCH_QUERIES = ["SOL", "pump", "bonk", "meme"]  # DEX Screener search (300 req/min limit)
GECKO_DISCOVERY = [            # (source label, path) -- discovery only; prices still come from DEX Screener
    ("gt_trending_24h_p1", "networks/solana/trending_pools?page=1"),
    ("gt_new_pools_p1", "networks/solana/new_pools?page=1"),
    ("gt_top_volume_p1", "networks/solana/pools?page=1&sort=h24_volume_usd_desc"),
]
# GeckoTerminal served only ~4 calls per cycle before 429s in testing, so one extra endpoint rotates each cycle
GECKO_ROTATING = [
    ("gt_trending_1h_p1", "networks/solana/trending_pools?page=1&duration=1h"),
    ("gt_new_pools_p2", "networks/solana/new_pools?page=2"),
    ("gt_top_txcount_p1", "networks/solana/pools?page=1&sort=h24_tx_count_desc"),
]
SAFETY_CACHE_MIN = 30          # reuse a RugCheck report fetched within 30 min

# --- Safety rules ---
MIN_TOKEN_AGE_MIN = 30
MAX_SINGLE_HOLDER_PCT = 10.0   # excluding known AMM/pool accounts
MAX_TOP10_HOLDERS_PCT = 30.0   # excluding known AMM/pool accounts
MAX_RUGCHECK_SCORE_NORM = 50   # RugCheck normalised risk score (higher = riskier)

# --- Analyst ---
ENTRY_SCORE_THRESHOLD = 65

DEXSCREENER = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"
QUOTE_WHITELIST = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# Not memecoins: stablecoins, wrapped assets, liquid-staking SOL. Excluded at the scanner so they don't use safety slots.
NON_MEME_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "USD1", "PYUSD", "USDS", "USDE", "USDG", "FDUSD", "DAI", "EURC",
                    "WETH", "ETH", "CBBTC", "WBTC", "BTC", "ZBTC", "TBTC", "JITOSOL", "MSOL", "BSOL", "JUPSOL", "INF",
                    "BBSOL", "HSOL", "DSOL", "JSOL", "VSOL", "BNSOL", "XAUT", "PAXG"}
