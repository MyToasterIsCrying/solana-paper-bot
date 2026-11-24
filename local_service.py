#!/usr/bin/env python3
"""Windows-local paper service with isolated discovery and position monitoring."""
import argparse
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
import queue
import sqlite3
import threading
import tempfile
import time
import traceback

import config
import dashboard
import db
import run_cycle
from agents import executor, risk
from agents.local_discovery import discover_worker
from platform_lock import acquire_lock, release_lock


def _dashboard_publishable(db_path, data):
    canonical = os.path.normcase(os.path.realpath(db_path)) == os.path.normcase(
        os.path.realpath(config.CANONICAL_LOCAL_DB_PATH))
    payload = json.dumps(data, ensure_ascii=False).casefold()
    has_fixture_data = any(marker in payload for marker in (
        "test-fixture://", "synthetic_trial_", "synthetic monitor",
    ))
    return canonical and not has_fixture_data


class IntervalSchedule:
    """Monotonic schedule that runs once when late and counts missed slots."""

    def __init__(self, interval_sec, first_due=None):
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        self.interval_sec = interval_sec
        self.next_due = time.monotonic() if first_due is None else first_due

    def poll(self, now):
        if now < self.next_due:
            return False, 0
        skipped = int((now - self.next_due) // self.interval_sec)
        self.next_due += (skipped + 1) * self.interval_sec
        return True, skipped


class LocalPaperService:
    """Only this process writes the local ledger; discovery receives snapshots only."""

    def __init__(self, db_path, lock_path, log_path, dashboard_path,
                 marketdata_factory=None, process_context=None, clock=time.monotonic,
                 wall_clock=db.now_iso, allow_entries=False):
        self.db_path = os.path.abspath(db_path)
        self.lock_path = lock_path
        self.log_path = log_path
        self.dashboard_path = dashboard_path
        self.marketdata_factory = marketdata_factory
        self.allow_entries = allow_entries
        self.context = process_context or multiprocessing.get_context("spawn")
        self.clock = clock
        self.wall_clock = wall_clock
        self.logger = logging.getLogger("paperbot.local")
        self.discovery_process = None
        self.discovery_queue = None
        self.discovery_message = None
        self.discovery_run_id = None
        self.last_monitor_started = None

    def _connect(self):
        return db.connect(self.db_path)

    def _log(self, message, *args):
        self.logger.info(message, *args)

    def _log_error(self, message, *args):
        self.logger.error(message, *args)

    def monitor_once(self, md=None):
        started = self.clock()
        interval = None if self.last_monitor_started is None else started - self.last_monitor_started
        self.last_monitor_started = started
        con = self._connect()
        cur = con.execute(
            "INSERT INTO runs(started_at,status,trigger,event) VALUES (?,?,?,?)",
            (self.wall_clock(), "running", "local-monitor", "local"))
        run_id = cur.lastrowid
        con.commit()
        summary = {"run_id": run_id, "monitor": [], "exits": [], "entries": [],
                   "evaluated": [], "actual_interval_sec": interval}
        status = "ok"
        try:
            md = md or (self.marketdata_factory or _live_marketdata)()
            run_cycle.monitor_positions(con, md, run_id, summary, quote_retries=0)
        except Exception:
            status = "error"
            message = traceback.format_exc()
            db.log_error(con, run_id, "local_monitor", message)
            self._log_error("monitor run %s failed: %s", run_id, message)
        try:
            eq, cash, pv = run_cycle.snapshot(con, run_id)
            summary.update(equity=eq, cash=cash, positions_value=pv)
        except Exception:
            status = "error"
            message = traceback.format_exc()
            db.log_error(con, run_id, "local_monitor_snapshot", message)
            self._log_error("monitor snapshot for run %s failed: %s", run_id, message)
        summary["runtime_sec"] = round(self.clock() - started, 3)
        summary["status"] = status
        con.execute("UPDATE runs SET finished_at=?,status=?,summary=? WHERE id=?",
                    (self.wall_clock(), status, db.j(summary), run_id))
        con.commit()
        con.close()
        stale = sum(1 for item in summary["monitor"] if str(item.get("action", "")).startswith("hold ("))
        failed = sum(1 for item in summary["monitor"] if item.get("action") == "hold (no quote)")
        quote_items = summary["monitor"] + summary["exits"]
        quote_latencies = [item["quote_latency_sec"] for item in quote_items
                           if item.get("quote_latency_sec") is not None]
        latency_mean = sum(quote_latencies) / len(quote_latencies) if quote_latencies else 0.0
        latency_max = max(quote_latencies) if quote_latencies else 0.0
        self._log(
            "monitor run=%s actual_interval_sec=%s duration_sec=%.3f positions=%d exits=%d "
            "stale_or_missing=%d quote_failures=%d quote_calls=%d quote_latency_mean_sec=%.3f "
            "quote_latency_max_sec=%.3f",
            run_id, "startup" if interval is None else f"{interval:.3f}", summary["runtime_sec"],
            len(summary["monitor"]) + len(summary["exits"]), len(summary["exits"]), stale, failed,
            len(quote_latencies), latency_mean, latency_max)
        self._refresh_dashboard()
        return summary

    def start_discovery(self, scheduled_at=None):
        if self.discovery_process is not None and self.discovery_process.is_alive():
            return False
        con = self._connect()
        cutoff = (time.time() - 24 * 60 * 60)
        cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(cutoff))
        con.execute("DELETE FROM safety_cache WHERE fetched_at < ?", (cutoff_iso,))
        cached_reports = [tuple(row) for row in con.execute(
            "SELECT token_address,fetched_at,source_url,report FROM safety_cache")]
        open_tokens = [row[0] for row in con.execute(
            "SELECT token_address FROM positions WHERE status='open'")]
        cur = con.execute(
            "INSERT INTO runs(started_at,status,trigger,event) VALUES (?,?,?,?)",
            (self.wall_clock(), "running", "local-discovery", "local"))
        self.discovery_run_id = cur.lastrowid
        con.commit()
        con.close()

        try:
            self.discovery_queue = self.context.Queue()
            self.discovery_process = self.context.Process(
                target=discover_worker,
                args=(self.discovery_queue, cached_reports, open_tokens),
                name="paperbot-discovery",
            )
            self.discovery_process.start()
        except Exception as exc:
            self._finish_discovery_error(f"could not start discovery worker: {exc}")
            return False
        self._log("discovery started run=%s scheduled_at=%s", self.discovery_run_id, scheduled_at)
        return True

    def _finish_discovery_error(self, message):
        if self.discovery_run_id is not None:
            con = self._connect()
            db.log_error(con, self.discovery_run_id, "local_discovery", message)
            con.execute("UPDATE runs SET finished_at=?,status='error',summary=? WHERE id=?",
                        (self.wall_clock(), db.j({"error": message}), self.discovery_run_id))
            con.commit()
            con.close()
        self._log_error("discovery run=%s failed: %s", self.discovery_run_id, message)
        self._reset_discovery()

    def _reset_discovery(self):
        if self.discovery_queue is not None:
            self.discovery_queue.close()
            self.discovery_queue.join_thread()
        self.discovery_queue = None
        self.discovery_message = None
        self.discovery_process = None
        self.discovery_run_id = None

    def harvest_discovery(self):
        process = self.discovery_process
        if process is None:
            return False
        message = self.discovery_message
        if message is None:
            try:
                message = self.discovery_queue.get_nowait()
            except queue.Empty:
                if process.is_alive():
                    return False
                process.join()
                try:
                    message = self.discovery_queue.get(timeout=1)
                except queue.Empty:
                    message = {"error": f"discovery worker exited with code {process.exitcode} without a result"}
        process.join(timeout=1)
        if process.is_alive():
            self.discovery_message = message
            return False
        if "error" in message:
            self._finish_discovery_error(message["error"])
            return True
        try:
            self._apply_discovery(message["result"])
        except Exception:
            self._finish_discovery_error(traceback.format_exc())
            return True
        self._reset_discovery()
        return True

    def _apply_discovery(self, batch):
        run_id = self.discovery_run_id
        con = self._connect()
        suppressed_entries = 0
        try:
            for message in batch["errors"]:
                db.log_error(con, run_id, "scanner", message)
                self._log_error("discovery run=%s API/scanner failure: %s", run_id, message)
            for failure in batch["api_failures"]:
                db.log_error(con, run_id, failure["stage"], failure["message"])
                self._log_error("discovery run=%s %s API failure: %s",
                                run_id, failure["stage"], failure["message"])
            for addr, symbol, reason in batch["rejected"]:
                db.log_skip(con, run_id, addr, symbol, "scanner", reason)
            candidates_json = db.j([
                {key: item["candidate"].get(key) for key in (
                    "token_address", "symbol", "pair_address", "dex_id", "m", "rank", "found_by")}
                for item in batch["candidates"]
            ])
            con.execute(
                "INSERT INTO scans(run_id,ts,sources,tokens_seen,candidates_passed_prefilter,candidates) VALUES (?,?,?,?,?,?)",
                (run_id, self.wall_clock(), db.j(batch["sources"]), batch["tokens_seen"],
                 batch["passed_count"], candidates_json),
            )
            for address, fetched_at, source_url, report in batch["cache_updates"]:
                con.execute("INSERT OR REPLACE INTO safety_cache VALUES (?,?,?,?)",
                            (address, fetched_at, source_url, report))

            for item in batch["candidates"]:
                candidate = item["candidate"]
                symbol = candidate.get("symbol")
                decision = item["decision"]
                safety_detail = item["safety_detail"]
                score = item["score"]
                score_detail = item["score_detail"]
                if decision == "already_holding":
                    db.log_skip(con, run_id, candidate["token_address"], symbol, "risk", "already holding")
                elif decision == "outside_safety_slots":
                    db.log_skip(con, run_id, candidate["token_address"], symbol, "scanner",
                                f"not safety-checked: outside top-{config.MAX_SAFETY_CHECKS_PER_RUN} slots")
                elif decision == "safety_stopped":
                    db.log_skip(con, run_id, candidate["token_address"], symbol, "safety",
                                batch["safety"]["stopped_early"] or "safety phase stopped")
                elif decision == "reject_safety":
                    db.log_skip(con, run_id, candidate["token_address"], symbol, "safety",
                                "; ".join(safety_detail.get("reasons", [])))
                elif decision == "below_threshold_or_gate":
                    db.log_skip(con, run_id, candidate["token_address"], symbol, "analyst",
                                f"score {score} (threshold {config.ENTRY_SCORE_THRESHOLD}) "
                                f"gates={score_detail.get('gates_failed', [])}")
                elif decision in ("skip_quote_failure", "skip_stale"):
                    db.log_skip(con, run_id, candidate["token_address"], symbol, "executor",
                                f"no fresh verified quote: {item.get('quote_error', 'quote unavailable')}")
                elif decision == "entry_candidate":
                    if not self.allow_entries:
                        db.log_skip(con, run_id, candidate["token_address"], symbol, "risk",
                                    "new entries disabled")
                        decision = "entries_disabled"
                        suppressed_entries += 1
                    else:
                        approved, reason, budget = risk.approve_entry(con, candidate)
                        if not approved:
                            db.log_skip(con, run_id, candidate["token_address"], symbol, "risk", reason)
                        else:
                            quote = item["entry_quote"]
                            fresh, why = executor.quote_is_fresh(quote)
                            if not fresh or not (quote.get("liquidity") or {}).get("usd"):
                                db.log_skip(con, run_id, candidate["token_address"], symbol, "executor",
                                            f"no fresh verified quote: {why or 'no liquidity'}")
                            else:
                                try:
                                    _, fill = executor.open_position(
                                        con, run_id, candidate, budget, quote, score)
                                    self._log(
                                        "discovery run=%s paper entry symbol=%s cost_usd=%.6f fee_usd=%.6f",
                                        run_id, symbol, budget, fill["fee_usd"])
                                except Exception as exc:
                                    db.log_skip(con, run_id, candidate["token_address"], symbol, "executor",
                                                f"entry not executed: {exc}")
                con.execute(
                    """INSERT INTO evaluations(run_id,ts,token_address,pair_address,symbol,price_usd,
                    liquidity_usd,safety_passed,safety_detail,score,score_detail,decision,source_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, self.wall_clock(), candidate["token_address"], candidate["pair_address"], symbol,
                     candidate["m"]["price_usd"], candidate["m"]["liquidity_usd"],
                     int(item["safety_passed"]), db.j(safety_detail), score, db.j(score_detail),
                     decision, candidate["quote"].get("_source_url")))
            eq, cash, pv = run_cycle.snapshot(con, run_id)
            summary = {
                "tokens_seen_after_dedup": batch["tokens_seen"],
                "prefilter_passed": batch["passed_count"],
                "entries_suppressed": suppressed_entries,
                "safety": batch["safety"],
                "api_stats": batch.get("api_stats"),
                "equity": eq, "cash": cash, "positions_value": pv,
            }
            con.execute("UPDATE runs SET finished_at=?,status='ok',summary=? WHERE id=?",
                        (self.wall_clock(), db.j(summary), run_id))
            con.commit()
            self._log("discovery complete run=%s tokens=%d prefilter=%d entries_suppressed=%d",
                      run_id, batch["tokens_seen"], batch["passed_count"], suppressed_entries)
        finally:
            con.close()
        self._refresh_dashboard()

    def _refresh_dashboard(self):
        data = dashboard.build(self.db_path)
        data["schedule"] = {
            "active": True,
            "cadence": "Local position monitoring every 60 seconds; discovery every 15 minutes",
            "note": "Paper-only Windows-local service. No real transactions.",
        }
        data["publication_allowed"] = _dashboard_publishable(self.db_path, data)
        directory = os.path.dirname(os.path.abspath(self.dashboard_path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".paperbot-dashboard-", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, indent=2, default=str)
            os.replace(tmp, self.dashboard_path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def run_forever(self, stop_event=None):
        stop_event = stop_event or multiprocessing.Event()
        lock = acquire_lock(self.lock_path)
        if lock is None:
            raise RuntimeError(f"another local paper-bot writer holds {self.lock_path}")
        monitor_schedule = IntervalSchedule(config.POSITION_MONITOR_INTERVAL_SEC, self.clock())
        discovery_schedule = IntervalSchedule(config.TOKEN_DISCOVERY_INTERVAL_SEC, self.clock())
        last_wall_check = time.time()
        last_monotonic_check = self.clock()
        self._log("local paper service started; startup monitoring and discovery are due immediately")
        try:
            while not stop_event.is_set():
                now = self.clock()
                wall_now = time.time()
                wall_gap = wall_now - last_wall_check
                monotonic_gap = now - last_monotonic_check
                if wall_gap - monotonic_gap > 2:
                    self._log_error(
                        "resume or clock gap detected wall_gap_sec=%.3f monotonic_gap_sec=%.3f; "
                        "position monitoring is due immediately", wall_gap, monotonic_gap)
                    monitor_schedule.next_due = now
                last_wall_check = wall_now
                last_monotonic_check = now
                monitor_due, monitor_skipped = monitor_schedule.poll(now)
                if monitor_due:
                    if monitor_skipped:
                        self._log_error("monitor skipped_checks=%d due to scheduler delay", monitor_skipped)
                    try:
                        self.monitor_once()
                    except Exception:
                        self._log_error("monitor scheduler failure: %s", traceback.format_exc())
                self.harvest_discovery()
                if stop_event.is_set():
                    break
                now = self.clock()
                discovery_due, discovery_skipped = discovery_schedule.poll(now)
                if discovery_due:
                    if discovery_skipped:
                        self._log_error("discovery skipped_slots=%d due to scheduler delay", discovery_skipped)
                    if self.discovery_process is not None and self.discovery_process.is_alive():
                        self._log_error("discovery check skipped; previous API-bound job is still running")
                    else:
                        if stop_event.is_set():
                            break
                        self.start_discovery(scheduled_at=self.wall_clock())
                deadlines = [monitor_schedule.next_due, discovery_schedule.next_due]
                wait = max(0.0, min(deadlines) - self.clock())
                stop_event.wait(min(wait, 1.0))
        except KeyboardInterrupt:
            self._log("stop requested; no new paper jobs will be started")
        finally:
            worker = self.discovery_process
            deadline = time.monotonic() + 5
            while worker is not None and worker.is_alive() and time.monotonic() < deadline:
                self.harvest_discovery()
                if self.discovery_process is None:
                    break
                time.sleep(0.05)
            if self.discovery_process is not None:
                if worker is not None and worker.is_alive():
                    worker.terminate()
                    worker.join()
                    self._finish_discovery_error("service stopped before discovery completed")
                else:
                    self.harvest_discovery()
            release_lock(lock)
            self._log("local paper service stopped")


def _live_marketdata():
    from marketdata import LiveMarketData
    return LiveMarketData()


class SyntheticMonitorMarketData:
    """Deterministic monitor-only quote fixture for isolated timed trials."""

    def __init__(self):
        self.calls = 0

    def quote_pair(self, token_address, pair_address, retries=2):
        self.calls += 1
        if self.calls % 3 == 0:
            raise TimeoutError("synthetic monitor API failure (trial fixture)")
        fetched_at = datetime.datetime.now(datetime.timezone.utc)
        if self.calls % 3 == 2:
            fetched_at -= datetime.timedelta(seconds=config.MAX_DATA_AGE_SEC + 60)
        return {
            "priceUsd": "1.0",
            "pairAddress": pair_address,
            "_fetched_at": fetched_at.isoformat(timespec="seconds"),
            "_source_url": "TEST-FIXTURE://synthetic-monitor",
        }


def _validate_local_ledger(path):
    uri = "file:" + path.replace("\\", "/").replace(" ", "%20") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"account", "positions", "fills", "runs", "equity_snapshots"}.issubset(tables):
            raise RuntimeError("local ledger is missing required paper-account tables")
        account = con.execute(
            "SELECT cash_usd,start_balance_usd,mode FROM account WHERE id=1").fetchone()
        if not account or account[2] != "PAPER" or account[1] != config.START_BALANCE_USD:
            raise RuntimeError("local ledger is not the expected $100 PAPER account")
        return account
    finally:
        if "con" in locals():
            con.close()


def main():
    parser = argparse.ArgumentParser(description="Windows-local paper trading only")
    parser.add_argument("--db", default=config.LOCAL_DB_PATH)
    parser.add_argument("--duration-sec", type=float)
    parser.add_argument("--synthetic-monitor", action="store_true")
    parser.add_argument("--allow-entries", action="store_true")
    args = parser.parse_args()
    db_path = os.path.abspath(args.db)
    if args.duration_sec is not None and args.duration_sec <= 0:
        parser.error("--duration-sec must be positive")
    if args.synthetic_monitor and args.duration_sec is None:
        parser.error("--synthetic-monitor requires --duration-sec")
    if args.synthetic_monitor and args.allow_entries:
        parser.error("--synthetic-monitor cannot be combined with --allow-entries")
    if args.synthetic_monitor and db_path in {
        os.path.abspath(config.DB_PATH), os.path.abspath(config.LOCAL_DB_PATH),
    }:
        parser.error("--synthetic-monitor requires a separate trial ledger")
    if not os.path.isfile(db_path):
        raise SystemExit(
            f"Local ledger not found: {db_path}. Create it from a verified ledger backup first; "
            "the service never initializes a new trading account.")
    if db_path == os.path.abspath(config.DB_PATH):
        raise SystemExit("Refusing to use the GitHub/production ledger path as the local service ledger.")
    if os.environ.get("PAPERBOT_LOCAL_MIGRATION_CONFIRMED") != "YES":
        raise SystemExit(
            "Refusing to start until GitHub ledger-writing workflows are stopped and the local ledger is migrated. "
            "Set PAPERBOT_LOCAL_MIGRATION_CONFIRMED=YES only after that migration.")
    try:
        _validate_local_ledger(db_path)
    except Exception as exc:
        raise SystemExit(f"Refusing to start with an unverified local ledger: {exc}") from exc

    os.makedirs(os.path.dirname(os.path.abspath(config.LOCAL_LOG_PATH)), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(config.LOCAL_LOG_PATH, maxBytes=2_000_000,
                                      backupCount=5, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    if not args.allow_entries:
        logging.info("new paper entries are disabled")
    synthetic_md = SyntheticMonitorMarketData() if args.synthetic_monitor else None
    service = LocalPaperService(db_path, config.LOCAL_LOCK_PATH,
                                config.LOCAL_LOG_PATH, config.LOCAL_DASHBOARD_PATH,
                                marketdata_factory=(lambda: synthetic_md) if synthetic_md else None,
                                allow_entries=args.allow_entries)
    if args.duration_sec is None:
        service.run_forever()
    else:
        stop_event = multiprocessing.Event()
        timer = threading.Timer(args.duration_sec, stop_event.set)
        timer.daemon = True
        timer.start()
        try:
            service.run_forever(stop_event=stop_event)
        finally:
            timer.cancel()


if __name__ == "__main__":
    main()
