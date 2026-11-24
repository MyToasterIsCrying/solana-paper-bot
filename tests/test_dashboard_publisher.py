"""Offline tests for read-only dashboard validation and feed-only publishing."""
import datetime
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

import db
from tools import build_site
from tools.publish_dashboard import DASHBOARD_FILES, find_git, publish_dashboard, validate_snapshot
from tools.validate_dashboard_feed import validate_site

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "publish-dashboard.yml")


class DashboardPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.ledger = os.path.join(self.temp, "local_ledger.db")
        con = db.connect(self.ledger)
        con.close()
        self.dashboard = os.path.join(self.temp, "local_dashboard.json")
        self.data = self.snapshot()
        self.write_snapshot()

    def snapshot(self):
        return {
            "publication_allowed": True,
            "mode": "PAPER TRADING - SIMULATION ONLY (no wallet, no real transactions)",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "start_balance_usd": 100.0,
            "cash_usd": 100.0,
            "positions_value_usd": 0.0,
            "portfolio_value_usd": 100.0,
            "open_positions": [],
            "closed_trades": 0,
            "total_fills": 0,
        }

    def write_snapshot(self):
        with open(self.dashboard, "w", encoding="utf-8") as output:
            json.dump(self.data, output)

    def build_site(self):
        docs = os.path.join(self.temp, "docs")
        build_site.build(self.dashboard, docs)
        return docs

    def test_validation_is_read_only_and_checks_ledger_accounting(self):
        with open(self.ledger, "rb") as ledger_file:
            before = hashlib.sha256(ledger_file.read()).hexdigest()
        validated = validate_snapshot(
            self.dashboard, self.ledger, self.dashboard, self.ledger)
        with open(self.ledger, "rb") as ledger_file:
            after = hashlib.sha256(ledger_file.read()).hexdigest()
        self.assertEqual(validated["portfolio_value_usd"], 100.0)
        self.assertEqual(before, after)

    def test_validation_rejects_wrong_source_synthetic_and_stale_data(self):
        with self.assertRaisesRegex(ValueError, "canonical local dashboard"):
            validate_snapshot(self.dashboard, self.ledger, "other.json", self.ledger)

        self.data["publication_allowed"] = False
        self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "not authorized"):
            validate_snapshot(self.dashboard, self.ledger, self.dashboard, self.ledger)

        self.data["publication_allowed"] = True
        self.data["recent_errors"] = [{"message": "TEST-FIXTURE://synthetic"}]
        self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "Synthetic or trial"):
            validate_snapshot(self.dashboard, self.ledger, self.dashboard, self.ledger)

        self.data["recent_errors"] = []
        self.data["generated_at"] = (
            datetime.datetime.now(datetime.timezone.utc) -
            datetime.timedelta(minutes=10)).isoformat()
        self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "freshness window"):
            validate_snapshot(self.dashboard, self.ledger, self.dashboard, self.ledger)

    def test_static_validator_rejects_synthetic_artifact(self):
        self.data["recent_errors"] = [{"message": "synthetic monitor fixture"}]
        self.write_snapshot()
        docs = self.build_site()
        with self.assertRaisesRegex(ValueError, "Synthetic or trial"):
            validate_site(docs)

    def _seed_main_branch(self, remote):
        git = find_git()
        subprocess.run([git, "init", "--bare", remote], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        seed = os.path.join(self.temp, "seed-main")
        os.makedirs(seed)
        subprocess.run([git, "-C", seed, "init"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run([git, "-C", seed, "checkout", "-b", "main"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run([git, "-C", seed, "config", "user.name", "Test"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run([git, "-C", seed, "config", "user.email", "test@example.invalid"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for path, content in (
            ("README.md", "paper bot source\n"),
            ("ledger/state.sql", "historical ledger must remain untouched\n"),
            ("docs/index.html", "old dashboard\n"),
            ("docs/dashboard.json", "{}\n"),
            ("docs/.nojekyll", "stale marker\n"),
        ):
            destination = os.path.join(seed, *path.split("/"))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "w", encoding="utf-8") as output:
                output.write(content)
        subprocess.run([git, "-C", seed, "add", "--", "."], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run([git, "-C", seed, "commit", "-m", "seed production main"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run([git, "-C", seed, "remote", "add", "origin", remote], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run([git, "-C", seed, "push", "-u", "origin", "main"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_main_push_changes_only_dashboard_assets_and_preserves_ledger(self):
        git = find_git()
        remote = os.path.join(self.temp, "remote.git")
        self._seed_main_branch(remote)
        docs = self.build_site()
        self.assertTrue(publish_dashboard(remote, docs, git=git))
        self.assertFalse(publish_dashboard(remote, docs, git=git))

        result = subprocess.run(
            [git, "--git-dir", remote, "show", "--pretty=format:", "--name-only",
             "refs/heads/main"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(set(result.stdout.splitlines()), DASHBOARD_FILES)
        ledger = subprocess.run(
            [git, "--git-dir", remote, "show", "refs/heads/main:ledger/state.sql"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(ledger.stdout, "historical ledger must remain untouched\n")

        with open(WORKFLOW, encoding="utf-8") as workflow_file:
            workflow = workflow_file.read()
        self.assertNotIn("contents: write", workflow)
        self.assertIn("- main", workflow)
        self.assertIn("docs/dashboard.json", workflow)
        self.assertIn("path: docs", workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
