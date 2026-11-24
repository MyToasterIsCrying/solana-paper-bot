#!/usr/bin/env python3
"""GitHub Actions entry point: run one paper-trading cycle and stamp the runs row with GitHub metadata.
PAPER TRADING ONLY. Exit code 1 if the cycle errored or crashed (the ledger row is still written so it can be committed)."""
import os, sys, json, datetime, traceback
import config, db

def parse(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None

def iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds") if dt else None

def schedule_slot(t, minutes=15):
    """Nominal cron slot for a `*/15` schedule: the latest 15-minute boundary at or before the time GitHub queued the run.
    (If GitHub delays a run by more than 15 min this attributes it to a later slot, so delays are a lower bound.)"""
    return t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)

def main():
    env = os.environ
    event = env.get("GITHUB_EVENT_NAME", "local")
    run_id = env.get("GITHUB_RUN_ID")
    url = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env.get('GITHUB_REPOSITORY')}/actions/runs/{run_id}" if run_id else None
    queued = parse(env.get("QUEUED_AT")); started = parse(env.get("JOB_STARTED_AT")) or datetime.datetime.now(datetime.timezone.utc)
    scheduled = schedule_slot(queued or started) if event == "schedule" else None
    delay = (started - scheduled).total_seconds() if scheduled else None
    meta = {"event": event, "github_run_id": run_id, "github_run_attempt": int(env.get("GITHUB_RUN_ATTEMPT", "1") or 1),
            "github_run_url": url, "scheduled_for": iso(scheduled), "queued_at": iso(queued),
            "job_started_at": iso(started), "start_delay_sec": delay}
    status, rid, err = "crashed", None, None
    try:
        import run_cycle
        out = run_cycle.run(trigger="github-actions")
        status, rid = out.get("status"), out.get("run_id")
        err = out.get("error")
        print(json.dumps({k: out.get(k) for k in ("run_id", "status", "tokens_seen_after_dedup", "prefilter_passed",
                                                   "safety", "entries", "exits", "runtime_sec", "api_stats", "error")},
                         indent=2, default=str))
    except Exception:
        err = traceback.format_exc(); print(err, file=sys.stderr)
    con = db.connect()
    if rid is None and status != "skipped_overlap":   # crash before/outside the cycle: still leave a record
        cur = con.execute("INSERT INTO runs(started_at,finished_at,status,trigger,summary) VALUES (?,?,?,?,?)",
                          (iso(started), db.now_iso(), "crashed", "github-actions", None))
        rid = cur.lastrowid
        db.log_error(con, rid, "gh_run", err or "unknown crash")
    if rid is not None:
        sets = ", ".join(f"{k}=?" for k in meta) + ", error_summary=?"
        con.execute(f"UPDATE runs SET {sets} WHERE id=?", (*meta.values(), (err or "")[:1000] or None, rid))
        con.commit()
    con.close()
    print(f"ledger run {rid}: status={status} event={event} delay_sec={delay}")
    return 0 if status in ("ok", "skipped_overlap") else 1

if __name__ == "__main__":
    sys.exit(main())
