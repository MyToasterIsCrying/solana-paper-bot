"""Offline tests for local scheduling, monitoring, discovery isolation, and accounting."""
import datetime
import json
import os
import queue
import tempfile
import unittest

import config
import db
import run_cycle
from agents import risk
from local_service import (
    IntervalSchedule, LocalPaperService, SyntheticMonitorMarketData, _dashboard_publishable,
    _validate_local_ledger,
)
from agents.local_discovery import discover_batch
from agents import executor
from tests.test_accounting import FixtureMarketData


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeProcess:
    def __init__(self, target, args, name=None):
        self.target = target
        self.args = args
        self.name = name
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


class FakeContext:
    def Queue(self):
        return queue.Queue()

    def Process(self, target, args, name=None):
        return FakeProcess(target, args, name)


class LocalServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "local-test.db")
        self.lock_path = os.path.join(self.tmp, "service.lock")
        self.dashboard_path = os.path.join(self.tmp, "dashboard.json")

    def service(self, clock=None, md_factory=None, context=None, allow_entries=False):
        return LocalPaperService(
            self.db_path, self.lock_path, os.path.join(self.tmp, "service.log"),
            self.dashboard_path, marketdata_factory=md_factory,
            process_context=context, clock=clock or FakeClock(), allow_entries=allow_entries,
        )

    def open_position(self, md):
        out = run_cycle.run(db_path=self.db_path, md=md, trigger="test",
                            lock_path=os.path.join(self.tmp, "cycle.lock"))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(out["entries"]), 1)
        con = db.connect(self.db_path)
        position = db.open_positions(con)[0]
        con.close()
        return position

    def test_schedule_runs_once_and_counts_missed_slots(self):
        self.assertEqual(config.POSITION_MONITOR_INTERVAL_SEC, 60)
        self.assertEqual(config.TOKEN_DISCOVERY_INTERVAL_SEC, 900)
        schedule = IntervalSchedule(60, first_due=100)
        self.assertEqual(schedule.poll(100), (True, 0))
        self.assertEqual(schedule.poll(159), (False, 0))
        self.assertEqual(schedule.poll(282), (True, 2))
        self.assertEqual(schedule.next_due, 340)

    def test_scheduler_logs_skipped_monitor_and_discovery_slots(self):
        clock = FakeClock(10)
        service = self.service(clock=clock, md_factory=lambda: FixtureMarketData({}))
        service.start_discovery = lambda scheduled_at=None: None
        service.harvest_discovery = lambda: None

        class AdvancingStopEvent:
            def __init__(self):
                self.waits = 0
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                self.waits += 1
                if self.waits == 1:
                    clock.value += 1800
                else:
                    self.stopped = True
                return self.stopped

        with self.assertLogs("paperbot.local", level="ERROR") as captured:
            service.run_forever(stop_event=AdvancingStopEvent())

        logs = "\n".join(captured.output)
        self.assertIn("monitor skipped_checks=29", logs)
        self.assertIn("discovery skipped_slots=1", logs)

    def test_shutdown_does_not_start_due_discovery(self):
        clock = FakeClock(10)
        service = self.service(clock=clock, md_factory=lambda: FixtureMarketData({}))
        discovery_starts = []
        stop_event = type("StopEvent", (), {
            "stopped": False,
            "is_set": lambda event: event.stopped,
            "set": lambda event: setattr(event, "stopped", True),
        })()
        original_monitor = service.monitor_once

        def monitor_then_stop():
            result = original_monitor()
            stop_event.set()
            return result

        service.monitor_once = monitor_then_stop
        service.start_discovery = lambda scheduled_at=None: discovery_starts.append(scheduled_at)
        service.run_forever(stop_event=stop_event)

        self.assertEqual(discovery_starts, [])
        con = db.connect(self.db_path)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM runs WHERE trigger='local-discovery'").fetchone()[0], 0)
        con.close()

    def test_restart_starts_with_immediate_check_without_replaying_history(self):
        resumed_at = 10_000
        schedule = IntervalSchedule(60, first_due=resumed_at)
        self.assertEqual(schedule.poll(resumed_at), (True, 0))
        self.assertEqual(schedule.poll(resumed_at + 1), (False, 0))

    def test_monitor_records_actual_interval(self):
        clock = FakeClock(10)
        service = self.service(clock=clock)
        service.monitor_once(FixtureMarketData({}))
        clock.value = 70
        result = service.monitor_once(FixtureMarketData({}))
        self.assertEqual(result["actual_interval_sec"], 60)

    def test_monitor_records_per_quote_latency(self):
        md = FixtureMarketData({"TOKEN_L1": {"price": 1.0}})
        self.open_position(md)
        result = self.service().monitor_once(md)
        latency = result["monitor"][0]["quote_latency_sec"]
        self.assertGreaterEqual(latency, 0)

    def test_only_canonical_nonfixture_dashboard_is_publishable(self):
        self.assertTrue(_dashboard_publishable(config.CANONICAL_LOCAL_DB_PATH, {"mode": "PAPER"}))
        self.assertFalse(_dashboard_publishable(self.db_path, {"mode": "PAPER"}))
        self.assertFalse(_dashboard_publishable(
            config.CANONICAL_LOCAL_DB_PATH, {"source": "TEST-FIXTURE://synthetic"}))

    def test_synthetic_monitor_fixture_cycles_fresh_stale_and_failure(self):
        md = SyntheticMonitorMarketData()
        fresh = md.quote_pair("SYNTHETIC", "PAIR")
        self.assertTrue(executor.quote_is_fresh(fresh)[0])
        stale = md.quote_pair("SYNTHETIC", "PAIR")
        self.assertFalse(executor.quote_is_fresh(stale)[0])
        with self.assertRaisesRegex(TimeoutError, "synthetic monitor API failure"):
            md.quote_pair("SYNTHETIC", "PAIR")

    def test_discovery_worker_does_not_block_monitor(self):
        service = self.service(context=FakeContext())
        service.start_discovery(scheduled_at="synthetic")
        self.assertTrue(service.discovery_process.started)
        md = FixtureMarketData({"TOKEN_M1": {"price": 1.0}})
        result = service.monitor_once(md)
        self.assertEqual(result["monitor"], [])
        self.assertEqual(result["status"], "ok")

    def test_synthetic_discovery_is_applied_by_ledger_owner(self):
        md = FixtureMarketData({"TOKEN_D1": {"price": 1.0}})
        batch = discover_batch([], [], md=md)
        self.assertEqual(batch["tokens_seen"], 1)
        self.assertEqual(len(batch["candidates"]), 1)
        self.assertEqual(batch["candidates"][0]["decision"], "entry_candidate")

        con = db.connect(self.db_path)
        cur = con.execute(
            "INSERT INTO runs(started_at,status,trigger,event) VALUES (?,?,?,?)",
            (db.now_iso(), "running", "local-discovery", "local"))
        run_id = cur.lastrowid
        con.commit()
        con.close()
        service = self.service(allow_entries=True)
        service.discovery_run_id = run_id
        service._apply_discovery(batch)

        con = db.connect(self.db_path)
        position = db.open_positions(con)[0]
        self.assertAlmostEqual(position["cost_usd"], 5.0)
        self.assertAlmostEqual(position["stop_price"], position["entry_mid_price"] * 0.94)
        self.assertAlmostEqual(position["target_price"], position["entry_mid_price"] * 1.12)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
        con.close()
        self.assertEqual((config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT,
                          config.MAX_POSITION_PCT, config.MAX_OPEN_POSITIONS),
                         (0.06, 0.12, 0.05, 3))

    def test_new_entries_are_disabled_by_default(self):
        md = FixtureMarketData({"TOKEN_D2": {"price": 1.0}})
        batch = discover_batch([], [], md=md)
        con = db.connect(self.db_path)
        cur = con.execute(
            "INSERT INTO runs(started_at,status,trigger,event) VALUES (?,?,?,?)",
            (db.now_iso(), "running", "local-discovery", "local"))
        run_id = cur.lastrowid
        con.commit()
        con.close()

        service = self.service()
        service.discovery_run_id = run_id
        service._apply_discovery(batch)

        con = db.connect(self.db_path)
        self.assertEqual(len(db.open_positions(con)), 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        self.assertEqual(db.get_cash(con), 100.0)
        self.assertEqual(con.execute(
            "SELECT decision FROM evaluations").fetchone()[0], "entries_disabled")
        self.assertTrue(con.execute(
            "SELECT 1 FROM skips WHERE stage='risk' AND reason='new entries disabled'").fetchone())
        con.close()

    def test_safety_api_failure_is_returned_for_logging(self):
        md = FixtureMarketData({"TOKEN_F1": {"price": 1.0}})
        md.rug_fail.add("TOKEN_F1")
        batch = discover_batch([], [], md=md)
        self.assertEqual(batch["candidates"][0]["decision"], "reject_safety")
        self.assertTrue(any("RugCheck report unavailable" in failure["message"]
                            for failure in batch["api_failures"]))
        con = db.connect(self.db_path)
        cur = con.execute(
            "INSERT INTO runs(started_at,status,trigger,event) VALUES (?,?,?,?)",
            (db.now_iso(), "running", "local-discovery", "local"))
        run_id = cur.lastrowid
        con.commit()
        con.close()
        service = self.service()
        service.discovery_run_id = run_id
        service._apply_discovery(batch)
        con = db.connect(self.db_path)
        self.assertTrue(con.execute(
            "SELECT 1 FROM errors WHERE stage='safety' AND message LIKE '%RugCheck report unavailable%'").fetchone())
        con.close()

    def test_uninitialized_local_ledger_is_refused_without_creating_tables(self):
        empty_path = os.path.join(self.tmp, "empty.db")
        with open(empty_path, "wb"):
            pass
        with self.assertRaises(RuntimeError):
            _validate_local_ledger(empty_path)
        import sqlite3
        con = sqlite3.connect(empty_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)
        con.close()

    def test_quote_failure_is_logged_and_position_is_held(self):
        initial = FixtureMarketData({"TOKEN_Q1": {"price": 1.0}})
        self.open_position(initial)

        class FailedQuote(FixtureMarketData):
            def quote_pair(self, _token, _pair, retries=2):
                raise TimeoutError(
                    f"synthetic timeout for {_token}/{_pair} after {retries} retries")

        service = self.service()
        result = service.monitor_once(FailedQuote(initial.tokens))
        self.assertEqual(result["monitor"][0]["action"], "hold (no quote)")
        con = db.connect(self.db_path)
        self.assertEqual(len(db.open_positions(con)), 1)
        self.assertTrue(con.execute(
            "SELECT 1 FROM errors WHERE stage='monitor' AND message LIKE '%synthetic timeout%'").fetchone())
        con.close()

    def test_stale_price_is_logged_without_fabricated_exit(self):
        md = FixtureMarketData({"TOKEN_S1": {"price": 1.0}})
        self.open_position(md)
        old = (datetime.datetime.now(datetime.timezone.utc) -
               datetime.timedelta(minutes=10)).isoformat(timespec="seconds")
        md.tokens["TOKEN_S1"]["fetched_at"] = old
        result = self.service().monitor_once(md)
        self.assertIn("old", result["monitor"][0]["action"])
        con = db.connect(self.db_path)
        self.assertEqual(len(db.open_positions(con)), 1)
        self.assertTrue(con.execute(
            "SELECT 1 FROM errors WHERE stage='monitor' AND message LIKE '%fresh verified price%'").fetchone())
        con.close()

    def test_gap_exit_and_accounting_identity(self):
        md = FixtureMarketData({"TOKEN_G1": {"price": 1.0}})
        position = self.open_position(md)
        md.tokens["TOKEN_G1"]["price"] = 0.90
        result = self.service().monitor_once(md)
        self.assertEqual(result["exits"][0]["reason"], "stop_loss")
        self.assertAlmostEqual(result["exits"][0]["exit_mid"], 0.90)
        expected = risk.simulate_sell(position["qty"], 0.90, 500_000)
        self.assertAlmostEqual(result["exits"][0]["pnl_usd"],
                               round(expected["proceeds_usd"] - position["cost_usd"], 4))
        with open(self.dashboard_path, encoding="utf-8") as dashboard_file:
            data = json.load(dashboard_file)
        self.assertAlmostEqual(data["cash_usd"] + data["positions_value_usd"],
                               data["portfolio_value_usd"], places=9)
        self.assertAlmostEqual(data["portfolio_value_usd"],
                               config.START_BALANCE_USD + data["realized_pnl_usd"] +
                               data["unrealized_pnl_usd"], places=9)
        self.assertEqual(data["closed_trades"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
