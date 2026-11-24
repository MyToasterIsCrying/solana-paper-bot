#!/usr/bin/env python3
"""Publish only a verified local dashboard snapshot to the dashboard-feed branch."""
import argparse
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from tools import build_site
from tools.validate_dashboard_feed import validate_site

FEED_BRANCH = "dashboard-feed"
COAUTHOR = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
ALLOWED_TRACKED = {
    ".github/workflows/publish-dashboard.yml",
    "docs/.nojekyll",
    "docs/dashboard.json",
    "docs/index.html",
    "tools/validate_dashboard_feed.py",
}
SYNTHETIC_MARKERS = ("test-fixture://", "synthetic_trial_", "synthetic monitor")
MAX_SNAPSHOT_AGE_SEC = 300


def _normalized(path):
    return os.path.normcase(os.path.realpath(path))


def _contains_synthetic_data(data):
    payload = json.dumps(data, ensure_ascii=False).casefold()
    return any(marker in payload for marker in SYNTHETIC_MARKERS)


def validate_snapshot(dashboard_path, ledger_path, canonical_dashboard_path,
                      canonical_ledger_path, now=None):
    if _normalized(dashboard_path) != _normalized(canonical_dashboard_path):
        raise ValueError("Only the canonical local dashboard may be published")
    if _normalized(ledger_path) != _normalized(canonical_ledger_path):
        raise ValueError("Only the canonical local ledger may authorize publication")
    if not os.path.isfile(dashboard_path) or not os.path.isfile(ledger_path):
        raise FileNotFoundError("The canonical local dashboard and ledger must both exist")

    with open(dashboard_path, encoding="utf-8") as source:
        data = json.load(source)
    if data.get("publication_allowed") is not True:
        raise ValueError("Dashboard source is not authorized for publication")
    if not isinstance(data.get("mode"), str) or not data["mode"].startswith("PAPER TRADING"):
        raise ValueError("Dashboard is not marked as paper-trading data")
    if _contains_synthetic_data(data):
        raise ValueError("Synthetic or trial data cannot be published")

    generated_at = datetime.datetime.fromisoformat(
        str(data["generated_at"]).replace("Z", "+00:00"))
    if generated_at.tzinfo is None:
        raise ValueError("Dashboard timestamp must include a timezone")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    age = (now - generated_at.astimezone(datetime.timezone.utc)).total_seconds()
    if age < -60 or age > MAX_SNAPSHOT_AGE_SEC:
        raise ValueError(f"Dashboard is outside the freshness window: {age:.0f}s")

    uri_path = quote(os.path.abspath(ledger_path).replace("\\", "/"), safe="/:")
    con = sqlite3.connect("file:" + uri_path + "?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        account = con.execute(
            "SELECT cash_usd,start_balance_usd,mode FROM account WHERE id=1").fetchone()
        if not account or account[2] != "PAPER" or account[1] != config.START_BALANCE_USD:
            raise ValueError("Canonical ledger is not the expected $100 PAPER account")
        open_count, positions_value = con.execute(
            """SELECT COUNT(*), COALESCE(SUM(
                 (CASE WHEN last_price IS NULL OR last_price=0
                       THEN entry_mid_price ELSE last_price END) * qty),0)
               FROM positions WHERE status='open'""").fetchone()
        closed_count = con.execute(
            "SELECT COUNT(*) FROM positions WHERE status='closed'").fetchone()[0]
        fill_count = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    finally:
        con.close()

    cash, start_balance = float(account[0]), float(account[1])
    if abs(float(data["cash_usd"]) - cash) > 1e-8:
        raise ValueError("Dashboard cash does not match the read-only ledger")
    if abs(float(data["start_balance_usd"]) - start_balance) > 1e-8:
        raise ValueError("Dashboard starting balance does not match the ledger")
    if abs(float(data["positions_value_usd"]) - positions_value) > 1e-6:
        raise ValueError("Dashboard position value does not match the ledger")
    if abs(float(data["portfolio_value_usd"]) - cash - positions_value) > 1e-6:
        raise ValueError("Dashboard equity does not match cash plus open positions")
    if len(data["open_positions"]) != open_count or data["closed_trades"] != closed_count:
        raise ValueError("Dashboard position counts do not match the ledger")
    if data["total_fills"] != fill_count:
        raise ValueError("Dashboard fill count does not match the ledger")
    return data


def find_git():
    git = shutil.which("git")
    if git:
        return git
    for candidate in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe"):
        if os.path.isfile(candidate):
            return candidate
    raise RuntimeError("Git was not found; install Git for Windows before dashboard publishing")


def _git(git, *args, cwd=None, check=True):
    result = subprocess.run(
        [git, *args], cwd=cwd, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise RuntimeError("Git operation failed: " + result.stderr.strip())
    return result


def publish_feed(remote_url, docs_directory, workflow_file, git=None):
    git = git or find_git()
    with tempfile.TemporaryDirectory(prefix="paperbot-dashboard-feed-") as temp_dir:
        worktree = os.path.join(temp_dir, "feed")
        branch_ref = "refs/heads/" + FEED_BRANCH
        remote_branch = _git(git, "ls-remote", "--heads", remote_url, branch_ref)
        if remote_branch.stdout.strip():
            _git(git, "clone", "--depth", "1", "--single-branch", "--branch",
                 FEED_BRANCH, remote_url, worktree)
        else:
            os.makedirs(worktree)
            _git(git, "init", worktree)
            _git(git, "-C", worktree, "checkout", "--orphan", FEED_BRANCH)
            _git(git, "-C", worktree, "remote", "add", "origin", remote_url)

        tracked = set(_git(git, "-C", worktree, "ls-files", "-z").stdout.split("\0"))
        tracked.discard("")
        unexpected = tracked - ALLOWED_TRACKED
        if unexpected:
            raise RuntimeError("Dashboard feed branch contains non-dashboard files: " +
                               ", ".join(sorted(unexpected)))

        for name in ("index.html", "dashboard.json", ".nojekyll"):
            destination = os.path.join(worktree, "docs", name)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copyfile(os.path.join(docs_directory, name), destination)
        validator_destination = os.path.join(
            worktree, "tools", "validate_dashboard_feed.py")
        os.makedirs(os.path.dirname(validator_destination), exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, "tools", "validate_dashboard_feed.py"),
                        validator_destination)
        workflow_destination = os.path.join(
            worktree, ".github", "workflows", "publish-dashboard.yml")
        os.makedirs(os.path.dirname(workflow_destination), exist_ok=True)
        shutil.copyfile(workflow_file, workflow_destination)

        paths = sorted(ALLOWED_TRACKED)
        _git(git, "-C", worktree, "add", "--", *paths)
        staged = _git(git, "-C", worktree, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return False
        if staged.returncode != 1:
            raise RuntimeError("Could not inspect staged dashboard files")

        _git(git, "-C", worktree, "config", "user.name", "Paper Bot Dashboard Publisher")
        _git(git, "-C", worktree, "config", "user.email",
             "paper-bot-dashboard@users.noreply.github.com")
        _git(git, "-C", worktree, "commit", "-m", "dashboard: publish verified snapshot",
             "-m", COAUTHOR)
        _git(git, "-C", worktree, "push", "-u", "origin", "HEAD:" + branch_ref)
        return True


def main():
    parser = argparse.ArgumentParser(
        description="Validate the canonical PAPER dashboard; --publish pushes only dashboard-feed")
    parser.add_argument("--publish", action="store_true",
                        help="push the dashboard-only feed branch (not enabled by default)")
    args = parser.parse_args()

    data = validate_snapshot(
        config.CANONICAL_LOCAL_DASHBOARD_PATH,
        config.CANONICAL_LOCAL_DB_PATH,
        config.CANONICAL_LOCAL_DASHBOARD_PATH,
        config.CANONICAL_LOCAL_DB_PATH,
    )
    with tempfile.TemporaryDirectory(prefix="paperbot-pages-build-") as temp_dir:
        docs_directory = os.path.join(temp_dir, "docs")
        build_site.build(config.CANONICAL_LOCAL_DASHBOARD_PATH, docs_directory)
        validate_site(docs_directory)
        if args.publish:
            git = find_git()
            remote = _git(git, "-C", ROOT, "remote", "get-url", "origin").stdout.strip()
            changed = publish_feed(
                remote, docs_directory,
                os.path.join(ROOT, ".github", "workflows", "publish-dashboard.yml"), git)
            print("dashboard feed pushed" if changed else "dashboard feed is already current")
        else:
            print("snapshot validated and site built locally; no push or deployment performed")


if __name__ == "__main__":
    main()
