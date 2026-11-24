"""Accounting / risk / lock tests. Uses a SEPARATE temp DB and SYNTHETIC TEST-FIXTURE prices.
Nothing here touches ledger.db and none of these prices are market data."""
import os, sys, time, json, tempfile, unittest, datetime, sqlite3, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, db, run_cycle, dashboard
from platform_lock import acquire_lock, release_lock
from agents import risk

FIXTURE_URL = "TEST-FIXTURE://synthetic"   # clearly not a real source

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

class FixtureMarketData:
    """TEST FIXTURE market data: synthetic prices, never real."""
    label = "TEST-FIXTURE"
    def __init__(self, tokens):
        self.tokens = tokens   # addr -> dict(price, liq, stale=False, rug_ok=True)
        self.rug_fail = set()
    def _pair(self, addr):
        t = self.tokens[addr]
        fetched = t.get("fetched_at") or now_iso()
        return {"chainId": "solana", "dexId": "fixture", "pairAddress": "PAIR_" + addr,
                "baseToken": {"address": addr, "symbol": "T" + addr[-2:], "name": "Fixture " + addr},
                "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
                "priceUsd": str(t["price"]), "liquidity": {"usd": t.get("liq", 500_000)},
                "volume": {"m5": 80_000, "h1": 500_000, "h24": 2_000_000},
                "priceChange": {"m5": 4, "h1": 30, "h24": 50},
                "txns": {"h1": {"buys": 700, "sells": 300}, "m5": {"buys": 70, "sells": 30}},
                "pairCreatedAt": int((time.time() - 3 * 3600) * 1000),
                "_source_url": FIXTURE_URL, "_fetched_at": fetched}
    def discover(self):
        return list(self.tokens), [{"url": FIXTURE_URL, "fetched_at": now_iso()}], []
    def pairs_for_tokens(self, addrs):
        return {a: self._pair(a) for a in addrs if a in self.tokens}
    def quote_pair(self, token, pair, retries=2):
        return self._pair(token) if token in self.tokens else None
    def rugcheck(self, mint):
        if mint in self.rug_fail:
            raise RuntimeError("fixture: rugcheck unavailable")
        return ({"mintAuthority": None, "freezeAuthority": None, "rugged": False, "risks": [], "score_normalised": 1,
                 "topHolders": [{"address": "h1", "owner": "o1", "pct": 3.0}, {"address": "h2", "owner": "o2", "pct": 2.0}],
                 "knownAccounts": {}, "markets": [], "totalHolders": 5000}, FIXTURE_URL, now_iso())

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dbp = os.path.join(self.tmp, "test_ledger.db")
        self.lock = os.path.join(self.tmp, "test.lock")
        self.assertNotEqual(os.path.abspath(self.dbp), os.path.abspath(config.DB_PATH))
    def run_cycle(self, md):
        return run_cycle.run(db_path=self.dbp, md=md, trigger="test", lock_path=self.lock)
    def con(self):
        return db.connect(self.dbp)
    def assert_equity_identity(self):
        c = self.con(); cash = db.get_cash(c)
        pv = sum(p["last_price"] * p["qty"] for p in db.open_positions(c))
        d = dashboard.build(self.dbp)
        self.assertAlmostEqual(d["cash_usd"] + d["positions_value_usd"], d["portfolio_value_usd"], places=9)
        self.assertAlmostEqual(d["cash_usd"], cash, places=9)
        self.assertAlmostEqual(d["positions_value_usd"], pv, places=9)
        # equity = start + realized + unrealized
        self.assertAlmostEqual(d["portfolio_value_usd"], 100 + d["realized_pnl_usd"] + d["unrealized_pnl_usd"], places=9)
        return d

class TestAccounting(Base):
    def test_entry_sizing_fees_and_identity(self):
        md = FixtureMarketData({"TOKEN_A1": {"price": 1.0}})
        out = self.run_cycle(md)
        self.assertEqual(out["status"], "ok"); self.assertEqual(len(out["entries"]), 1)
        e = out["entries"][0]
        self.assertAlmostEqual(e["cost_usd"], 5.0)                      # 5% of $100
        swap_in = 5.0 - config.NETWORK_FEE_USD
        self.assertAlmostEqual(e["fee_usd"], config.NETWORK_FEE_USD + swap_in * config.DEX_FEE_PCT)
        self.assertGreater(e["slippage_usd"], 0)
        self.assertGreater(e["fill_price"], 1.0)                        # paid above mid
        d = self.assert_equity_identity()
        self.assertAlmostEqual(d["cash_usd"], 95.0)
        self.assertLess(d["portfolio_value_usd"], 100.0)                # costs already reduce equity
        self.assertAlmostEqual(100 - d["portfolio_value_usd"], e["fee_usd"] + e["slippage_usd"], places=9)

    def test_stop_loss_exits_at_first_fetched_price_not_threshold(self):
        md = FixtureMarketData({"TOKEN_A1": {"price": 1.0}})
        self.run_cycle(md)
        c = self.con(); pos = db.open_positions(c)[0]
        md.tokens["TOKEN_A1"]["price"] = 0.97          # -3%: hold
        out = self.run_cycle(md); self.assertEqual(out["exits"], [])
        md.tokens["TOKEN_A1"]["price"] = 0.90          # gapped through -6% stop
        out = self.run_cycle(md)
        self.assertEqual(len(out["exits"]), 1); x = out["exits"][0]
        self.assertEqual(x["reason"], "stop_loss"); self.assertAlmostEqual(x["exit_mid"], 0.90)
        s = risk.simulate_sell(pos["qty"], 0.90, 500_000)
        self.assertAlmostEqual(x["pnl_usd"], round(s["proceeds_usd"] - 5.0, 4), places=4)
        self.assertLess(x["pnl_usd"], -0.5)            # worse than the -6% plan because of the gap + costs
        d = self.assert_equity_identity()
        self.assertEqual(d["closed_trades"], 1); self.assertEqual(d["win_rate_pct"], 0.0)
        self.assertAlmostEqual(d["cash_usd"], 95.0 + s["proceeds_usd"], places=9)
        self.assertGreater(d["max_drawdown_pct"], 0)

    def test_take_profit_and_win_rate(self):
        md = FixtureMarketData({"TOKEN_A1": {"price": 2.0}})
        self.run_cycle(md); pos = db.open_positions(self.con())[0]
        md.tokens["TOKEN_A1"]["price"] = 2.30          # +15%
        out = self.run_cycle(md); x = out["exits"][0]
        self.assertEqual(x["reason"], "take_profit")
        s = risk.simulate_sell(pos["qty"], 2.30, 500_000)
        pnl = s["proceeds_usd"] - 5.0
        self.assertGreater(pnl, 0); self.assertAlmostEqual(x["pnl_usd"], round(pnl, 4), places=4)
        d = self.assert_equity_identity()
        self.assertEqual(d["win_rate_pct"], 100.0)
        self.assertAlmostEqual(d["realized_pnl_usd"], pnl, places=9)
        fees = sum(t["fee_usd"] for t in d["trade_history"])
        self.assertAlmostEqual(d["total_fees_usd"], fees)

    def test_max_three_positions_and_no_reentry(self):
        md = FixtureMarketData({f"TOKEN_B{i}": {"price": 1.0} for i in range(5)})
        out = self.run_cycle(md)
        self.assertEqual(len(out["entries"]), 3)
        out = self.run_cycle(md)                         # all held / limit reached
        self.assertEqual(len(out["entries"]), 0)
        c = self.con()
        self.assertEqual(len(db.open_positions(c)), 3)
        reasons = [r[0] for r in c.execute("SELECT reason FROM skips WHERE stage='risk'")]
        self.assertTrue(any("max 3 open positions" in r for r in reasons))
        self.assertTrue(any("already holding" in r for r in reasons))
        self.assert_equity_identity()

    def test_position_size_tracks_current_equity(self):
        md = FixtureMarketData({"TOKEN_C1": {"price": 1.0}})
        self.run_cycle(md); md.tokens["TOKEN_C1"]["price"] = 0.5; self.run_cycle(md)   # stop out
        eq = dashboard.build(self.dbp)["portfolio_value_usd"]
        md.tokens["TOKEN_C2"] = {"price": 1.0}
        out = self.run_cycle(md)
        self.assertEqual(len(out["entries"]), 1)
        self.assertAlmostEqual(out["entries"][0]["cost_usd"], round(0.05 * eq, 6), places=6)

    def test_duplicate_open_blocked_by_db(self):
        md = FixtureMarketData({"TOKEN_D1": {"price": 1.0}}); self.run_cycle(md)
        c = self.con()
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO positions(token_address,pair_address,status) VALUES ('TOKEN_D1','X','open')")

    def test_stale_or_missing_data_skipped(self):
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).isoformat(timespec="seconds")
        md = FixtureMarketData({"TOKEN_E1": {"price": 1.0, "fetched_at": old}})
        out = self.run_cycle(md)
        self.assertEqual(out["entries"], [])
        c = self.con()
        self.assertTrue(c.execute("SELECT 1 FROM skips WHERE stage='executor' AND reason LIKE '%old%'").fetchone())
        self.assertAlmostEqual(db.get_cash(c), 100.0)

    def test_safety_unavailable_rejects(self):
        md = FixtureMarketData({"TOKEN_F1": {"price": 1.0}}); md.rug_fail.add("TOKEN_F1")
        out = self.run_cycle(md)
        self.assertEqual(out["entries"], [])
        self.assertEqual(out["evaluated"][0]["decision"], "reject_safety")

    def test_lock_prevents_overlap(self):
        fh = acquire_lock(self.lock)
        self.assertIsNotNone(fh)
        try:
            out = self.run_cycle(FixtureMarketData({"TOKEN_G1": {"price": 1.0}}))
            self.assertEqual(out["status"], "skipped_overlap")
            self.assertIsNone(out["run_id"])
            c = self.con()
            self.assertEqual(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
        finally:
            release_lock(fh)
        out = self.run_cycle(FixtureMarketData({"TOKEN_G1": {"price": 1.0}}))
        self.assertEqual(out["status"], "ok")

    def test_lock_across_processes(self):
        holder = subprocess.Popen([sys.executable, "-c",
            f"import sys,time;sys.path.insert(0,{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r});"
            f"from platform_lock import acquire_lock;f=acquire_lock({self.lock!r});"
            "print('held' if f else 'failed',flush=True);time.sleep(5)"],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            t = time.time(); out = self.run_cycle(FixtureMarketData({"TOKEN_H1": {"price": 1.0}}))
            self.assertEqual(out["status"], "skipped_overlap"); self.assertLess(time.time() - t, 2)
        finally:
            holder.terminate()
            holder.wait()
            holder.stdout.close()

class TestDedup(Base):
    def test_dedupe_helper(self):
        import marketdata
        order, by = marketdata.dedupe([("a", "TOKEN_X1"), ("b", "TOKEN_X1"), ("b", " TOKEN_X1 "), ("c", "TOKEN_Y1"),
                                       ("a", "So11111111111111111111111111111111111111112"), ("d", "token_x1")])
        self.assertEqual(order, ["TOKEN_X1", "TOKEN_Y1", "token_x1"])   # base58 is case-sensitive
        self.assertEqual(by["TOKEN_X1"], ["a", "b"])

    def test_duplicates_from_sources_checked_once(self):
        md = FixtureMarketData({"TOKEN_Z1": {"price": 1.0}, "TOKEN_Z2": {"price": 1.0}})
        calls = []
        orig = md.rugcheck
        def counting(mint):
            calls.append(mint); return orig(mint)
        md.rugcheck = counting
        md.discover = lambda: (["TOKEN_Z1", "TOKEN_Z1", " TOKEN_Z1", "TOKEN_Z2", "TOKEN_Z2"],
                               [{"source": "fx", "url": FIXTURE_URL, "fetched_at": now_iso(), "solana_tokens": 5}], [])
        out = self.run_cycle(md)
        self.assertEqual(out["tokens_seen_after_dedup"], 2)
        self.assertEqual(sorted(calls), ["TOKEN_Z1", "TOKEN_Z2"])
        self.assertEqual(len(out["evaluated"]), 2)
        self.assertEqual(len(out["entries"]), 2)
        self.assertEqual(out["safety"]["complete"], 2)

class TestLedgerIO(Base):
    def test_dump_restore_roundtrip(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import ledger_io
        md = FixtureMarketData({"TOKEN_R1": {"price": 1.0}})
        self.run_cycle(md); md.tokens["TOKEN_R1"]["price"] = 1.2; self.run_cycle(md)
        c = self.con()
        c.execute("UPDATE positions SET name=? WHERE id=1", ("Unicode \u03a9 name",))
        c.commit()
        out = os.path.join(self.tmp, "ledger"); db2 = os.path.join(self.tmp, "restored.db")
        ledger_io.dump(self.dbp, out); ledger_io.restore(out, db2)
        ledger_io.compare(self.dbp, db2)   # raises on any mismatch
        self.assertEqual(dashboard.build(db2)["realized_pnl_usd"], dashboard.build(self.dbp)["realized_pnl_usd"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
