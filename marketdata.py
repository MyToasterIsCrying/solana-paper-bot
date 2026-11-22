"""Public, keyless market-data clients (DEX Screener + GeckoTerminal discovery + RugCheck).
Every response is stamped with its URL and fetch time. Per-run API stats (calls, 429s, errors) are kept in self.stats."""
import time, datetime, requests
import config

UA = {"User-Agent": "solana-paper-bot/2.0 (paper trading simulation)", "Accept": "application/json"}

class FetchError(Exception):
    pass

class RateLimited(FetchError):
    pass

class Quote(dict):
    """A pair snapshot from DEX Screener + provenance (source_url, fetched_at)."""

def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

def _host(url):
    return url.split("/")[2]

def dedupe(entries):
    """entries: iterable of (source_label, token_address). Returns (ordered unique addresses, {addr: [sources]}).
    Solana addresses are case-sensitive base58, so dedup is on the exact stripped string."""
    order, by = [], {}
    for src, a in entries:
        a = (a or "").strip()
        if not a or a in config.QUOTE_WHITELIST:
            continue
        if a not in by:
            by[a] = []; order.append(a)
        if src not in by[a]:
            by[a].append(src)
    return order, by

class LiveMarketData:
    label = "LIVE"

    def __init__(self):
        self.s = requests.Session(); self.s.headers.update(UA)
        self._last = {}
        self.stats = {}
        self.last_headers = {}

    def _stat(self, host, key):
        h = self.stats.setdefault(host, {"calls": 0, "ok": 0, "http_429": 0, "errors": 0})
        h[key] += 1

    def _pace(self, host, spacing):
        wait = spacing - (time.time() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)

    def _get(self, url, retries=2, spacing=0.0):
        host = _host(url); last = None
        for i in range(retries + 1):
            self._pace(host, spacing)
            self._stat(host, "calls")
            try:
                r = self.s.get(url, timeout=config.HTTP_TIMEOUT)
                self._last[host] = time.time(); self.last_headers = dict(r.headers)
                if r.status_code == 429:
                    self._stat(host, "http_429")
                    last = RateLimited(f"429 rate limited: {url}"); time.sleep(3 + 4 * i); continue
                if r.status_code != 200:
                    self._stat(host, "errors")
                    raise FetchError(f"HTTP {r.status_code} for {url}: {r.text[:200]}")
                data = r.json()
                self._stat(host, "ok")
                return data, _utcnow().isoformat(timespec="seconds")
            except (requests.RequestException, ValueError) as e:
                self._last[host] = time.time()
                self._stat(host, "errors")
                last = FetchError(f"{type(e).__name__}: {e} ({url})"); time.sleep(1 + i)
        raise last

    def discover(self):
        """Return (deduped solana token addresses, list of source dicts with per-source counts, errors, {addr: [sources]})."""
        entries, sources, errors = [], [], []

        def add(label, url, got, ts):
            sources.append({"source": label, "url": url, "fetched_at": ts, "solana_tokens": len(got)})
            entries.extend((label, a) for a in got)

        for label, path in [("ds_profiles_latest", "token-profiles/latest/v1"), ("ds_boosts_latest", "token-boosts/latest/v1"),
                            ("ds_boosts_top", "token-boosts/top/v1")]:
            url = f"{config.DEXSCREENER}/{path}"
            try:
                data, ts = self._get(url)
                add(label, url, [d["tokenAddress"] for d in data if d.get("chainId") == "solana" and d.get("tokenAddress")], ts)
            except FetchError as e:
                errors.append(f"{label}: {e}")
        for q in config.DS_SEARCH_QUERIES:
            url = f"{config.DEXSCREENER}/latest/dex/search?q={q}"
            try:
                data, ts = self._get(url)
                add(f"ds_search_{q}", url, [p["baseToken"]["address"] for p in (data.get("pairs") or [])
                     if p.get("chainId") == "solana" and p.get("quoteToken", {}).get("address") in config.QUOTE_WHITELIST], ts)
            except FetchError as e:
                errors.append(f"ds_search_{q}: {e}")
        rot = config.GECKO_ROTATING[(int(time.time()) // 900) % len(config.GECKO_ROTATING)]
        for label, path in config.GECKO_DISCOVERY + [rot]:
            url = f"https://api.geckoterminal.com/api/v2/{path}"
            try:
                data, ts = self._get(url, retries=0, spacing=config.GECKO_SPACING_SEC)  # no retry: a 429 just drops that source
                got = []
                for pool in data.get("data") or []:
                    base = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                    if base.startswith("solana_"):
                        got.append(base[len("solana_"):])
                add(label, url, got, ts)
            except FetchError as e:
                errors.append(f"{label}: {e}")
        uniq, by = dedupe(entries)
        return uniq, sources, errors, by

    def pairs_for_tokens(self, addrs):
        """Return {token_address: best Quote} using tokens/v1 (max 30 per call). Best = highest-liquidity whitelisted-quote pair."""
        out = {}
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            url = f"{config.DEXSCREENER}/tokens/v1/solana/{','.join(chunk)}"
            data, ts = self._get(url)
            for p in data or []:
                if p.get("chainId") != "solana":
                    continue
                base = p.get("baseToken", {}).get("address")
                if base not in chunk or p.get("quoteToken", {}).get("address") not in config.QUOTE_WHITELIST:
                    continue
                q = Quote(p); q["_source_url"] = url; q["_fetched_at"] = ts
                cur = out.get(base)
                if cur is None or (p.get("liquidity") or {}).get("usd", 0) > (cur.get("liquidity") or {}).get("usd", 0):
                    out[base] = q
        return out

    def quote_pair(self, token_address, pair_address):
        """Fresh quote for a specific pair (used for entries and exits)."""
        url = f"{config.DEXSCREENER}/tokens/v1/solana/{token_address}"
        data, ts = self._get(url)
        for p in data or []:
            if p.get("pairAddress") == pair_address:
                q = Quote(p); q["_source_url"] = url; q["_fetched_at"] = ts
                return q
        return None

    def rugcheck(self, mint):
        url = f"{config.RUGCHECK}/tokens/{mint}/report"
        data, ts = self._get(url, retries=1, spacing=config.RUGCHECK_SPACING_SEC)
        rem = self.last_headers.get("X-Rate-Limit-Remaining")
        if rem is not None and rem.isdigit() and int(rem) <= 2:
            time.sleep(3)   # back off before the window runs dry
        return data, url, ts
