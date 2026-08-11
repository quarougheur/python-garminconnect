#!/usr/bin/env python3
"""Backfill sleep columns in daily_summary.csv from raw/sleep_*.json.

No API calls, no third-party packages - stdlib only.

Usage:
  python3 rebuild_sleep.py                    # assumes ./health_data
  python3 rebuild_sleep.py --dir ./health_data
"""

import argparse
import csv
import json
from pathlib import Path

SLEEP_COLS = [
    "sleep_hours",
    "sleep_score",
    "sleep_quality",
    "deep_sleep_min",
    "light_sleep_min",
    "rem_sleep_min",
    "awake_min",
    "sleep_need_min",
    "sleep_avg_spo2",
    "sleep_avg_respiration",
    "sleep_avg_hrv",
    "sleep_resting_hr",
    "sleep_body_battery_change",
]


def parse_sleep_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    day = path.stem.replace("sleep_", "")
    entry = None
    if isinstance(data, list):
        for e in data:
            if isinstance(e, dict) and e.get("calendarDate") == day:
                entry = e
                break
        if entry is None and data:
            entry = data[0]
    elif isinstance(data, dict):
        entry = data
    if not entry:
        return {}

    v = entry.get("values") or {}
    secs = v.get("totalSleepTimeInSeconds")

    def _min(key):
        s = v.get(key)
        return round(s / 60) if s is not None else None

    return {
        "date": day,
        "sleep_hours": round(secs / 3600, 2) if secs else None,
        "sleep_score": v.get("sleepScore"),
        "sleep_quality": v.get("sleepScoreQuality"),
        "deep_sleep_min": _min("deepTime"),
        "light_sleep_min": _min("lightTime"),
        "rem_sleep_min": _min("remTime"),
        "awake_min": _min("awakeTime"),
        "sleep_need_min": v.get("sleepNeed"),
        "sleep_avg_spo2": v.get("spO2"),
        "sleep_avg_respiration": v.get("respiration"),
        "sleep_avg_hrv": v.get("avgOvernightHrv"),
        "sleep_resting_hr": v.get("restingHeartRate"),
        "sleep_body_battery_change": v.get("bodyBatteryChange"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="./health_data")
    args = parser.parse_args()

    base = Path(args.dir)
    csv_path = base / "daily_summary.csv"
    raw_dir = base / "raw"
    if not csv_path.exists():
        raise SystemExit(f"Not found: {csv_path}")

    # Parse all raw sleep files into {date: {col: value}}
    sleep_by_date = {}
    for f in sorted(raw_dir.glob("sleep_*.json")):
        try:
            parsed = parse_sleep_file(f)
        except (json.JSONDecodeError, OSError) as err:
            print(f"Skipping {f.name}: {err}")
            continue
        if parsed.get("sleep_hours") is not None or parsed.get("sleep_score") is not None:
            sleep_by_date[parsed["date"]] = parsed
    if not sleep_by_date:
        raise SystemExit("No sleep data found in raw/sleep_*.json")

    # Read existing CSV
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    # Ensure all sleep columns exist in the header
    for col in SLEEP_COLS:
        if col not in fieldnames:
            fieldnames.append(col)

    # Merge (sleep file values win when present)
    filled = 0
    for row in rows:
        parsed = sleep_by_date.get(row.get("date", ""))
        if not parsed:
            continue
        for col in SLEEP_COLS:
            val = parsed.get(col)
            if val is not None:
                row[col] = val
        if parsed.get("sleep_hours") is not None:
            filled += 1

    # Backup then write
    backup = csv_path.with_suffix(".csv.bak")
    if backup.exists():
        backup.unlink()
    csv_path.rename(backup)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k, "")) for k in fieldnames})

    print(f"Updated {csv_path}  ({filled}/{len(rows)} days now have sleep data)")
    print(f"Backup of previous version: {backup}")


if __name__ == "__main__":
    main()
