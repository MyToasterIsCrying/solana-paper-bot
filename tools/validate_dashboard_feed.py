#!/usr/bin/env python3
"""Validate a static dashboard artifact without opening or modifying a ledger."""
import argparse
import datetime
import json
import os
import re

SYNTHETIC_MARKERS = ("test-fixture://", "synthetic_trial_", "synthetic monitor")
MAX_SNAPSHOT_AGE_SEC = 300


def validate_site(docs_directory, now=None):
    dashboard_path = os.path.join(docs_directory, "dashboard.json")
    index_path = os.path.join(docs_directory, "index.html")
    marker_path = os.path.join(docs_directory, ".nojekyll")
    if not all(os.path.isfile(path) for path in (dashboard_path, index_path, marker_path)):
        raise ValueError("Pages artifact is incomplete")

    with open(dashboard_path, encoding="utf-8") as source:
        data = json.load(source)
    if data.get("publication_allowed") is not True:
        raise ValueError("Snapshot is not authorized for publication")
    if not isinstance(data.get("mode"), str) or not data["mode"].startswith("PAPER TRADING"):
        raise ValueError("Snapshot is not paper-trading data")
    generated = datetime.datetime.fromisoformat(
        str(data["generated_at"]).replace("Z", "+00:00"))
    if generated.tzinfo is None:
        raise ValueError("Snapshot timestamp must include a timezone")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    age = (now - generated.astimezone(datetime.timezone.utc)).total_seconds()
    if age < -60 or age > MAX_SNAPSHOT_AGE_SEC:
        raise ValueError(f"Snapshot is outside the freshness window: {age:.0f}s")

    with open(index_path, encoding="utf-8") as source:
        index = source.read()
    embedded = re.search(
        r'<script id="data" type="application/json">(.*?)</script>', index, re.DOTALL)
    if not embedded or json.loads(embedded.group(1)) != data:
        raise ValueError("Pages HTML does not contain the validated dashboard snapshot")
    published = (json.dumps(data, ensure_ascii=False) + index).casefold()
    if any(marker in published for marker in SYNTHETIC_MARKERS):
        raise ValueError("Synthetic or trial data cannot be published")
    return data


def main():
    parser = argparse.ArgumentParser(description="Validate a dashboard-only Pages artifact")
    parser.add_argument("--docs", default="docs")
    args = parser.parse_args()
    validate_site(args.docs)
    print("dashboard Pages artifact validated")


if __name__ == "__main__":
    main()
