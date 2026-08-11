#!/usr/bin/env python3
"""Health Console — collect, store history, analyze, and build the dashboard.
============================================================================

One command does everything:

  python3 health_program.py                  # fetch last 30 days, merge into history, build dashboard
  python3 health_program.py --days 90        # longer fetch window
  python3 health_program.py --skip-fetch     # rebuild dashboard from stored history only
  python3 health_program.py --plan-days 21   # longer day-by-day plan

Pipeline:
  1. COLLECT - pulls daily health metrics + activities + VO2max from Garmin
               Connect (same auth as demo.py: stored tokens, MFA fallback)
  2. STORE   - upserts into master history files, so data accumulates run
               over run and nothing is ever overwritten or lost:
                 health_data/daily_master.csv       (keyed by date)
                 health_data/activities_master.csv  (keyed by activity_id)
                 health_data/vo2max_master.csv      (keyed by date)
  3. ANALYZE - rule-based insights over sleep, HRV, RHR, SpO2, readiness,
               training load, VO2max
  4. PLAN    - generic day-by-day plan for the next N days (weekly training
               template + recovery logic), lamp-adaptive
  5. RENDER  - writes health_data/dashboard.html: fully offline, all history
               embedded, with a date-range selector (7/14/30/90 days, all,
               or custom from/to)

Dependencies: garminconnect (only when fetching). Everything else is stdlib.
"""

import argparse
import csv
import datetime
import json
import os
import statistics
import sys
import time
from pathlib import Path

# ============================================================================
# CONFIG - edit to taste
# ============================================================================
SLEEP_TARGET_H = 7.0
SLEEP_FLOOR_H = 5.5           # below this = flag
RHR_AMBER_OVER = 4            # bpm over baseline = flag
SPO2_AMBER = 93.0             # overnight avg below = flag
SPO2_RED = 90.0               # instant red
PLAN_DAYS_DEFAULT = 14

# Weekly plan template, day index 0=Mon .. 6=Sun. Edit to match your week.
WEEK_TEMPLATE = {
    0: ("Leg strength", "squats/step-ups/lunges + calf raises, 3-4 sets"),
    1: ("Incline walk / hike 60 min", "steady Z2, stairs or treadmill incline"),
    2: ("Easy spin 45-60 min", "Z1-Z2 on the trainer, high cadence"),
    3: ("Swim 30-40 min", "technique focus, relaxed breathing"),
    4: ("Strength (light) or easy spin 30 min", "keep it comfortable"),
    5: ("Long ride or hike 90-120 min", "longest session of the week, Z2 cap"),
    6: ("REST", "full recovery day - walk, stretch, nothing structured"),
}
RECOVERY_DAY = ("Recovery day", "easy walk 20-30 min + mobility only - readiness/HRV say back off")
DAILY_CONSTANTS = "Morning check (HRV/RHR/SpO2 vs lamp) · breathing/meditation · hydrate 2.5-3 L"


# ============================================================================
# COLLECTION (same auth + endpoints as before)
# ============================================================================
def init_api(tokenstore: str):
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )
    try:
        print(f"Logging in with stored tokens from: {tokenstore}")
        api = Garmin()
        api.login(tokenstore)
        print("Token login OK")
        return api
    except GarminConnectTooManyRequestsError as err:
        sys.exit(f"Rate limited by Garmin: {err}")
    except (FileNotFoundError, GarminConnectAuthenticationError, GarminConnectConnectionError):
        print("No valid tokens found - falling back to interactive login.")

    from getpass import getpass
    email = input("Email address: ").strip()
    password = getpass("Password: ")
    api = Garmin(email=email, password=password, is_cn=False, return_on_mfa=True)
    password = None
    result1, result2 = api.login()
    if result1 == "needs_mfa":
        mfa_code = input("MFA one-time code: ").strip()
        api.resume_login(result2, mfa_code)
    api.client.dump(tokenstore)
    print(f"Login OK - tokens saved to {tokenstore}")
    return api


def safe_call(report: dict, label: str, fn, *args, retries: int = 2, **kwargs):
    from garminconnect import GarminConnectTooManyRequestsError
    for attempt in range(retries + 1):
        try:
            data = fn(*args, **kwargs)
            report[label] = "ok"
            return data
        except GarminConnectTooManyRequestsError:
            wait = 30 * (attempt + 1)
            print(f"  Rate limited on {label}, waiting {wait}s...")
            time.sleep(wait)
        except Exception as err:  # noqa: BLE001
            report[label] = f"error: {type(err).__name__}: {err}"
            return None
    report[label] = "error: rate limited (retries exhausted)"
    return None


def g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if d is not None else default


def dump_raw(raw_dir: Path, name: str, data) -> None:
    if data is None:
        return
    (raw_dir / f"{name}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def parse_sleep_entry(sleep, day: str) -> dict:
    entry = None
    if isinstance(sleep, list):
        for e in sleep:
            if g(e, "calendarDate") == day:
                entry = e
                break
        if entry is None and sleep:
            entry = sleep[0]
    elif isinstance(sleep, dict):
        entry = sleep
    if not entry:
        return {}
    v = g(entry, "values", default={})
    secs = g(v, "totalSleepTimeInSeconds")

    def _min(key):
        s = g(v, key)
        return round(s / 60) if s is not None else None

    return {
        "sleep_hours": round(secs / 3600, 2) if secs else None,
        "sleep_score": g(v, "sleepScore"),
        "sleep_quality": g(v, "sleepScoreQuality"),
        "deep_sleep_min": _min("deepTime"),
        "light_sleep_min": _min("lightTime"),
        "rem_sleep_min": _min("remTime"),
        "awake_min": _min("awakeTime"),
        "sleep_need_min": g(v, "sleepNeed"),
        "sleep_avg_spo2": g(v, "spO2"),
        "sleep_avg_respiration": g(v, "respiration"),
        "sleep_avg_hrv": g(v, "avgOvernightHrv"),
        "sleep_resting_hr": g(v, "restingHeartRate"),
        "sleep_body_battery_change": g(v, "bodyBatteryChange"),
    }


def collect_day(api, day: str, raw_dir: Path, report: dict) -> dict:
    row = {"date": day}
    r = report.setdefault(day, {})

    summary = safe_call(r, "user_summary", api.get_user_summary, day)
    dump_raw(raw_dir, f"user_summary_{day}", summary)
    if summary:
        row.update(
            steps=g(summary, "totalSteps"),
            total_calories=g(summary, "totalKilocalories"),
            active_calories=g(summary, "activeKilocalories"),
            floors_up=g(summary, "floorsAscended"),
            intensity_min_moderate=g(summary, "moderateIntensityMinutes"),
            intensity_min_vigorous=g(summary, "vigorousIntensityMinutes"),
            resting_hr=g(summary, "restingHeartRate"),
            min_hr=g(summary, "minHeartRate"),
            max_hr=g(summary, "maxHeartRate"),
            avg_stress=g(summary, "averageStressLevel"),
            max_stress=g(summary, "maxStressLevel"),
            body_battery_charged=g(summary, "bodyBatteryChargedValue"),
            body_battery_drained=g(summary, "bodyBatteryDrainedValue"),
            body_battery_highest=g(summary, "bodyBatteryHighestValue"),
            body_battery_lowest=g(summary, "bodyBatteryLowestValue"),
            avg_spo2=g(summary, "averageSpo2"),
            lowest_spo2=g(summary, "lowestSpo2"),
        )

    sleep = safe_call(r, "sleep", api.get_sleep_daily, day, day)
    dump_raw(raw_dir, f"sleep_{day}", sleep)
    row.update(parse_sleep_entry(sleep, day))

    hrv = safe_call(r, "hrv", api.get_hrv_data_range, day, day)
    dump_raw(raw_dir, f"hrv_{day}", hrv)
    if hrv:
        entries = hrv if isinstance(hrv, list) else g(hrv, "hrvSummaries", default=[])
        for e in entries:
            summ = g(e, "hrvSummary", default=e if isinstance(e, dict) else {})
            if g(summ, "calendarDate") == day or len(entries) == 1:
                row.update(
                    hrv_last_night_avg=g(summ, "lastNightAvg"),
                    hrv_weekly_avg=g(summ, "weeklyAvg"),
                    hrv_status=g(summ, "status"),
                    hrv_baseline_low=g(summ, "baseline", "lowUpper"),
                    hrv_baseline_balanced_low=g(summ, "baseline", "balancedLow"),
                    hrv_baseline_balanced_high=g(summ, "baseline", "balancedUpper"),
                )
                break

    readiness = safe_call(r, "training_readiness", api.get_training_readiness, day)
    dump_raw(raw_dir, f"training_readiness_{day}", readiness)
    if readiness:
        tr = readiness[0] if isinstance(readiness, list) and readiness else readiness
        row.update(
            readiness_score=g(tr, "score"),
            readiness_level=g(tr, "level"),
            readiness_sleep_score_factor=g(tr, "sleepScoreFactorPercent"),
            readiness_recovery_time_factor=g(tr, "recoveryTimeFactorPercent"),
            readiness_hrv_factor=g(tr, "hrvFactorPercent"),
            readiness_acute_load=g(tr, "acuteLoad"),
        )

    status = safe_call(r, "training_status", api.get_training_status, day)
    dump_raw(raw_dir, f"training_status_{day}", status)
    if status:
        mm = g(status, "mostRecentVO2Max", "generic", default={})
        row["vo2max"] = g(mm, "vo2MaxPreciseValue") or g(mm, "vo2MaxValue")

    resp = safe_call(r, "respiration", api.get_respiration_data, day)
    dump_raw(raw_dir, f"respiration_{day}", resp)
    if resp:
        row.update(
            resp_waking_avg=g(resp, "avgWakingRespirationValue"),
            resp_sleep_avg=g(resp, "avgSleepRespirationValue"),
        )

    spo2 = safe_call(r, "spo2", api.get_spo2_data, day)
    dump_raw(raw_dir, f"spo2_{day}", spo2)
    if spo2:
        row["avg_spo2"] = row.get("avg_spo2") or g(spo2, "averageSpO2")
        row["lowest_spo2"] = row.get("lowest_spo2") or g(spo2, "lowestSpO2")
        row["sleep_avg_spo2"] = row.get("sleep_avg_spo2") or g(spo2, "avgSleepSpO2")

    weigh = safe_call(r, "weigh_ins", api.get_daily_weigh_ins, day)
    dump_raw(raw_dir, f"weigh_ins_{day}", weigh)
    if weigh:
        summaries = g(weigh, "dateWeightList", default=[])
        if summaries:
            latest = summaries[-1]
            w_grams = g(latest, "weight")
            row.update(
                weight_kg=round(w_grams / 1000, 2) if w_grams else None,
                body_fat_pct=g(latest, "bodyFat"),
            )
    return row


def collect_activities(api, start: str, end: str, raw_dir: Path, report: dict):
    r = report.setdefault("_activities", {})
    acts = safe_call(r, "activities_by_date", api.get_activities_by_date, start, end)
    dump_raw(raw_dir, "activities", acts)
    rows = []
    for a in acts or []:
        dur = g(a, "duration")
        dist = g(a, "distance")
        rows.append({
            "activity_id": g(a, "activityId"),
            "date": (g(a, "startTimeLocal") or "")[:10],
            "start_time": g(a, "startTimeLocal"),
            "name": g(a, "activityName"),
            "type": g(a, "activityType", "typeKey"),
            "duration_min": round(dur / 60, 1) if dur else None,
            "distance_km": round(dist / 1000, 2) if dist else None,
            "avg_hr": g(a, "averageHR"),
            "max_hr": g(a, "maxHR"),
            "calories": g(a, "calories"),
            "avg_power": g(a, "avgPower"),
            "norm_power": g(a, "normPower"),
            "training_load": g(a, "activityTrainingLoad"),
            "aerobic_te": g(a, "aerobicTrainingEffect"),
            "anaerobic_te": g(a, "anaerobicTrainingEffect"),
            "elevation_gain_m": g(a, "elevationGain"),
            "avg_speed_kmh": round((g(a, "averageSpeed") or 0) * 3.6, 2) or None,
        })
    return rows


def collect_vo2max_history(api, start: str, end: str, raw_dir: Path, report: dict):
    r = report.setdefault("_profile", {})
    data = safe_call(r, "max_metrics_range", api.get_max_metrics_range, start, end)
    dump_raw(raw_dir, "max_metrics_range", data)
    rows = []
    for entry in data or []:
        gen = g(entry, "generic", default={})
        if gen:
            rows.append({
                "date": g(gen, "calendarDate"),
                "vo2max": g(gen, "vo2MaxPreciseValue") or g(gen, "vo2MaxValue"),
                "fitness_age": g(gen, "fitnessAge"),
            })
        cyc = g(entry, "cycling", default={})
        if cyc and rows and rows[-1].get("date") == g(cyc, "calendarDate"):
            rows[-1]["vo2max_cycling"] = g(cyc, "vo2MaxPreciseValue") or g(cyc, "vo2MaxValue")
    return rows


# ============================================================================
# STORAGE - master history files with upsert
# ============================================================================
def read_csv(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            conv = {}
            for k, v in row.items():
                if v is None or v == "":
                    conv[k] = None
                else:
                    try:
                        fv = float(v)
                        conv[k] = int(fv) if fv.is_integer() and "." not in v else fv
                    except ValueError:
                        conv[k] = v
            out.append(conv)
    return out


def write_csv(path: Path, rows: list) -> None:
    if not rows:
        return
    keys = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in keys})


def upsert_master(path: Path, new_rows: list, key: str, sort_key: str) -> list:
    """Merge new rows into a master file by key. New values win, but never
    overwrite an existing value with None (protects history from partial
    fetches). Returns the merged, sorted rows."""
    master = {str(r[key]): r for r in read_csv(path) if r.get(key) is not None}
    added = updated = 0
    for row in new_rows:
        k = row.get(key)
        if k is None:
            continue
        k = str(k)
        if k in master:
            for col, val in row.items():
                if val is not None:
                    master[k][col] = val
            updated += 1
        else:
            master[k] = dict(row)
            added += 1
    merged = sorted(master.values(), key=lambda r: str(r.get(sort_key) or ""))
    write_csv(path, merged)
    print(f"  {path.name}: +{added} new, {updated} refreshed, {len(merged)} total")
    return merged


def seed_masters_from_legacy(out_dir: Path):
    """One-time migration: if old per-run CSVs exist but masters don't,
    seed the masters from them so no already-collected data is lost."""
    pairs = [
        ("daily_summary.csv", "daily_master.csv", "date", "date"),
        ("activities.csv", "activities_master.csv", "activity_id", "date"),
        ("vo2max_history.csv", "vo2max_master.csv", "date", "date"),
    ]
    for legacy, master, key, sort_key in pairs:
        lp, mp = out_dir / legacy, out_dir / master
        if lp.exists() and not mp.exists():
            rows = read_csv(lp)
            if rows:
                print(f"Migrating {legacy} -> {master} ({len(rows)} rows)")
                upsert_master(mp, rows, key, sort_key)


def backfill_sleep_from_raw(out_dir: Path):
    """Self-healing: parse every raw/sleep_*.json on disk and upsert the
    extracted sleep fields into daily_master.csv. Idempotent - repairs any
    day whose sleep columns are missing (e.g. collected before the sleep
    parser was fixed) without any API calls."""
    raw_dir = out_dir / "raw"
    master_path = out_dir / "daily_master.csv"
    if not raw_dir.exists() or not master_path.exists():
        return
    master_rows = read_csv(master_path)
    have_sleep = {str(r["date"]) for r in master_rows
                  if r.get("date") is not None and r.get("sleep_hours") not in (None, "")}
    patches = []
    for f in sorted(raw_dir.glob("sleep_*.json")):
        day = f.stem.replace("sleep_", "")
        if day in have_sleep:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        parsed = parse_sleep_entry(data, day)
        if parsed.get("sleep_hours") is not None or parsed.get("sleep_score") is not None:
            parsed["date"] = day
            patches.append(parsed)
    if patches:
        print(f"Backfilling sleep from raw JSON for {len(patches)} day(s):")
        upsert_master(master_path, patches, "date", "date")


# ============================================================================
# ANALYSIS
# ============================================================================
def fnum(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def fmedian(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def evaluate_latest(daily: list) -> dict:
    """Green/amber/red evaluation of the most recent day + slow-burn watches.
    Shared by the PC program, the server, and the monitor."""
    rows = sorted([r for r in daily if r.get("date")], key=lambda r: str(r["date"]))
    if not rows:
        return {"color": "red", "date": "?", "flags": ["no data in history"],
                "watch": [], "summary": ""}
    last = rows[-1]
    date = str(last["date"])
    rhr_base = fmedian([fnum(r.get("resting_hr")) for r in rows[-90:]])
    sleep = fnum(last.get("sleep_hours"))
    hrv = fnum(last.get("hrv_last_night_avg"))
    rhr = fnum(last.get("resting_hr"))
    spo2 = fnum(last.get("sleep_avg_spo2"))
    bal_low = fnum(last.get("hrv_baseline_balanced_low"))
    low = fnum(last.get("hrv_baseline_low"))
    readiness = fnum(last.get("readiness_score"))

    flags, hard = [], False
    if sleep is not None and sleep < SLEEP_FLOOR_H:
        flags.append(f"sleep {sleep:.1f}h (< {SLEEP_FLOOR_H}h)")
    if hrv is not None and bal_low is not None and hrv < bal_low:
        flags.append(f"HRV {hrv:.0f} below balanced ({bal_low:.0f})")
    if hrv is not None and low is not None and hrv < low:
        hard = True
    if rhr is not None and rhr_base is not None and rhr > rhr_base + RHR_AMBER_OVER:
        flags.append(f"RHR {rhr:.0f} (+{rhr - rhr_base:.0f} over baseline)")
    if spo2 is not None and spo2 < SPO2_AMBER:
        flags.append(f"overnight SpO2 {spo2:.1f}% (< {SPO2_AMBER}%)")
    if spo2 is not None and spo2 < SPO2_RED:
        hard = True
    color = "red" if (hard or len(flags) >= 2) else "amber" if flags else "green"

    watch = []
    sleep7 = fmean([fnum(r.get("sleep_hours")) for r in rows[-7:]])
    if sleep7 is not None and sleep7 < 6.0:
        watch.append(f"7d sleep avg {sleep7:.1f}h - chronically short")
    ready7 = fmean([fnum(r.get("readiness_score")) for r in rows[-7:]])
    if ready7 is not None and ready7 < 25:
        watch.append(f"7d readiness avg {ready7:.0f} - persistent under-recovery")
    rhr3 = [fnum(r.get("resting_hr")) for r in rows[-3:]]
    if (rhr_base is not None and len([v for v in rhr3 if v is not None]) == 3
            and all(v is not None and v > rhr_base + RHR_AMBER_OVER for v in rhr3)):
        watch.append(f"RHR elevated 3 days running (baseline {rhr_base:.0f})")

    bits = []
    if sleep is not None: bits.append(f"sleep {sleep:.1f}h")
    if hrv is not None: bits.append(f"HRV {hrv:.0f}ms")
    if rhr is not None: bits.append(f"RHR {rhr:.0f}")
    if spo2 is not None: bits.append(f"SpO2 {spo2:.1f}%")
    if readiness is not None: bits.append(f"readiness {readiness:.0f}")
    return {"color": color, "date": date, "flags": flags, "watch": watch,
            "summary": " / ".join(bits)}


def send_ntfy(url: str, title: str, body: str, priority: str, tags: str) -> None:
    import urllib.request
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", priority)
    req.add_header("Tags", tags)
    urllib.request.urlopen(req, timeout=15).read()


def notify_evaluation(ev: dict, ntfy_url: str, mode: str, dry: bool) -> None:
    color = ev["color"]
    if mode == "warnings" and color == "green" and not ev["watch"]:
        print("Lamp GREEN, no watch items - no notification sent.")
        return
    emoji = {"green": "\U0001F7E2", "amber": "\U0001F7E1", "red": "\U0001F534"}[color]
    title = f"{emoji} {color.upper()} - morning check {ev['date']}"
    lines = [ev["summary"]]
    if ev["flags"]: lines.append("Flags: " + "; ".join(ev["flags"]))
    if ev["watch"]: lines.append("Watch: " + "; ".join(ev["watch"]))
    if color == "red": lines.append("Suggested: full rest day regardless of plan.")
    elif color == "amber": lines.append("Suggested: downgrade today's session to easy.")
    body = "\n".join(lines)
    priority = {"green": "default", "amber": "high", "red": "urgent"}[color]
    tags = {"green": "white_check_mark", "amber": "warning", "red": "rotating_light"}[color]
    if dry:
        print(f"[dry-run ntfy] {title}\n{body}")
    else:
        send_ntfy(ntfy_url, title, body, priority, tags)
        print("Notification sent.")


def analyze(daily: list, acts: list, vo2: list):
    """Return (insights, actions) over the most recent data."""
    insights, actions = [], []
    rows = sorted([r for r in daily if r.get("date")], key=lambda r: str(r["date"]))
    if not rows:
        return [{"level": "bad", "title": "No data", "detail": "History is empty."}], []

    last = rows[-1]
    sleep = [fnum(r.get("sleep_hours")) for r in rows]
    sleep7 = fmean(sleep[-7:])
    sleep14 = fmean(sleep[-14:])
    sleep_prev14 = fmean(sleep[-28:-14]) if len(sleep) >= 28 else None
    short_nights7 = sum(1 for v in sleep[-7:] if v is not None and v < SLEEP_FLOOR_H)

    hrv = [fnum(r.get("hrv_last_night_avg")) for r in rows]
    hrv7 = fmean(hrv[-7:])
    bal_lo = fnum(last.get("hrv_baseline_balanced_low"))
    bal_hi = fnum(last.get("hrv_baseline_balanced_high"))

    rhr = [fnum(r.get("resting_hr")) for r in rows]
    rhr_base = fmedian(rhr[-90:])
    rhr7 = fmean(rhr[-7:])

    spo2 = [fnum(r.get("sleep_avg_spo2")) for r in rows]
    spo2_7 = fmean(spo2[-7:])
    spo2_low_days = sum(1 for v in spo2[-14:] if v is not None and v < SPO2_AMBER)

    ready = [fnum(r.get("readiness_score")) for r in rows]
    ready7 = fmean(ready[-7:])

    steps7 = fmean([fnum(r.get("steps")) for r in rows[-7:]])
    stress7 = fmean([fnum(r.get("avg_stress")) for r in rows[-7:]])

    # ---- Sleep ----
    if sleep7 is not None:
        if sleep7 >= SLEEP_TARGET_H:
            insights.append({"level": "good", "title": f"Sleep 7d avg {sleep7:.1f}h",
                             "detail": "At or above the 7h target - the foundation everything else builds on. Hold it."})
        elif sleep7 >= 6.0:
            insights.append({"level": "warn", "title": f"Sleep 7d avg {sleep7:.1f}h",
                             "detail": f"Below the 7h target, with {short_nights7} night(s) under {SLEEP_FLOOR_H}h this week. "
                                       "Chronic shortfall drags recovery, mood, and training quality."})
            actions.append("Move bedtime 30-45 min earlier; keep a fixed wake time. Target 7h minimum every night.")
        else:
            insights.append({"level": "bad", "title": f"Sleep 7d avg {sleep7:.1f}h",
                             "detail": "Chronically short - this is the highest-leverage fix available. Everything downstream (HRV, readiness, RHR) improves with it."})
            actions.append("Treat sleep as training: fixed lights-out, no screens 60 min before bed, cool dark room. 7-7.5h nightly.")
        if sleep14 is not None and sleep_prev14 is not None:
            delta = sleep14 - sleep_prev14
            if delta > 0.3:
                insights.append({"level": "good", "title": f"Sleep trending up (+{delta:.1f}h vs prior 2 wks)",
                                 "detail": "The routine change is working - keep it."})
            elif delta < -0.3:
                insights.append({"level": "warn", "title": f"Sleep trending down ({delta:.1f}h vs prior 2 wks)",
                                 "detail": "Slipping. Check what changed in the evening routine."})

    # ---- HRV ----
    if hrv7 is not None and bal_lo is not None:
        if hrv7 >= bal_lo:
            insights.append({"level": "good", "title": f"HRV 7d avg {hrv7:.0f} ms - in balanced range ({bal_lo:.0f}-{bal_hi:.0f})",
                             "detail": "Autonomic recovery is keeping up with life + training load."})
        else:
            insights.append({"level": "bad", "title": f"HRV 7d avg {hrv7:.0f} ms - below balanced ({bal_lo:.0f})",
                             "detail": "Accumulated strain. Reduce intensity until HRV returns to the band."})
            actions.append("Swap the next hard session for an easy one; re-check HRV each morning until back in the balanced band.")

    # ---- RHR ----
    if rhr7 is not None and rhr_base is not None:
        d = rhr7 - rhr_base
        if d <= 2:
            insights.append({"level": "good", "title": f"RHR steady ({rhr7:.0f} vs baseline {rhr_base:.0f})",
                             "detail": "No systemic fatigue signal."})
        elif d <= RHR_AMBER_OVER:
            insights.append({"level": "warn", "title": f"RHR slightly elevated (+{d:.0f} bpm over baseline)",
                             "detail": "Watch it - could be load, heat, alcohol, or short sleep."})
        else:
            insights.append({"level": "bad", "title": f"RHR elevated (+{d:.0f} bpm over baseline)",
                             "detail": "Persistent elevation suggests under-recovery or illness. Back off."})
            actions.append("Take 1-2 genuinely easy days; if RHR stays high alongside poor sleep, consider whether you're fighting something off.")

    # ---- SpO2 ----
    if spo2_7 is not None:
        if spo2_7 >= 95:
            insights.append({"level": "good", "title": f"Overnight SpO2 {spo2_7:.1f}% (7d avg)",
                             "detail": "Healthy baseline."})
        elif spo2_7 >= SPO2_AMBER:
            insights.append({"level": "warn", "title": f"Overnight SpO2 {spo2_7:.1f}% (7d avg)",
                             "detail": f"Acceptable, with {spo2_low_days} low night(s) in 14 days. Check watch fit if dips look like artifacts."})
        else:
            insights.append({"level": "bad", "title": f"Overnight SpO2 {spo2_7:.1f}% (7d avg)",
                             "detail": "Consistently below expected values. Verify sensor fit; if accurate and persistent, worth mentioning to a doctor."})

    # ---- Readiness ----
    if ready7 is not None:
        if ready7 >= 55:
            insights.append({"level": "good", "title": f"Training readiness {ready7:.0f} (7d avg)",
                             "detail": "Recovering well between sessions."})
        elif ready7 >= 30:
            insights.append({"level": "warn", "title": f"Training readiness {ready7:.0f} (7d avg)",
                             "detail": "Chronically mid-low - in Garmin's model this is usually the sleep-score factor. Fixing sleep lifts it."})
        else:
            insights.append({"level": "bad", "title": f"Training readiness {ready7:.0f} (7d avg)",
                             "detail": "Persistently poor recovery. Prioritize sleep and cut load this week."})
            actions.append("Drop one session this week entirely - recovery is where fitness actually consolidates.")

    # ---- Activity volume ----
    if steps7 is not None:
        lvl = "good" if steps7 >= 8000 else "warn" if steps7 >= 5000 else "bad"
        insights.append({"level": lvl, "title": f"Steps {steps7:,.0f}/day (7d avg)",
                         "detail": "Everyday movement outside structured sessions."})
        if lvl != "good":
            actions.append("Add 1-2 short walks on desk days - a 15 min walk after lunch is the easiest win.")

    if stress7 is not None:
        lvl = "good" if stress7 < 35 else "warn" if stress7 < 50 else "bad"
        insights.append({"level": lvl, "title": f"Avg stress {stress7:.0f} (7d avg)",
                         "detail": "Garmin all-day stress; sleep debt and training both push it up."})

    # ---- Training load trend ----
    if acts:
        by_week = {}
        for a in acts:
            tl = fnum(a.get("training_load"))
            if tl is None or not a.get("date"):
                continue
            dt = datetime.date.fromisoformat(str(a["date"]))
            monday = dt - datetime.timedelta(days=dt.weekday())
            by_week[monday] = by_week.get(monday, 0) + tl
        weeks = sorted(by_week)
        if len(weeks) >= 3:
            cur, prev = by_week[weeks[-2]], by_week[weeks[-3]]  # last full weeks
            if prev and cur / prev > 1.4:
                insights.append({"level": "warn", "title": f"Load ramped fast ({prev:.0f} -> {cur:.0f})",
                                 "detail": "Week-over-week jump >40% raises injury/illness risk. Absorb before adding more."})
                actions.append("Hold weekly volume flat for a week before the next increase.")
            else:
                recent = ", ".join(f"{by_week[w]:.0f}" for w in weeks[-4:])
                insights.append({"level": "good", "title": f"Weekly load: {recent}",
                                 "detail": "Last 4 weeks, oldest first. Steady progression or maintenance."})

    # ---- VO2max ----
    if vo2:
        vs = [fnum(r.get("vo2max")) for r in sorted(vo2, key=lambda r: str(r.get("date")))]
        vs = [v for v in vs if v is not None]
        if len(vs) >= 2:
            d = vs[-1] - vs[0]
            lvl = "good" if d >= 0 else "warn"
            insights.append({"level": lvl, "title": f"VO2max {vs[-1]:.1f} ({'+' if d >= 0 else ''}{d:.1f} over range)",
                             "detail": "Garmin's estimate skews low without recent running; trust the trend more than the number."})

    return insights, actions


# ============================================================================
# PLAN - generic day-by-day
# ============================================================================
def build_plan(daily: list, plan_days: int):
    rows = sorted([r for r in daily if r.get("date")], key=lambda r: str(r["date"]))
    last_date = datetime.date.fromisoformat(str(rows[-1]["date"]))
    ready7 = fmean([fnum(r.get("readiness_score")) for r in rows[-7:]])
    hrv7 = fmean([fnum(r.get("hrv_last_night_avg")) for r in rows[-7:]])
    bal_lo = fnum(rows[-1].get("hrv_baseline_balanced_low"))
    needs_recovery = ((ready7 is not None and ready7 < 30)
                      or (hrv7 is not None and bal_lo is not None and hrv7 < bal_lo))

    plan = []
    day = last_date + datetime.timedelta(days=1)
    for i in range(plan_days):
        session, detail = WEEK_TEMPLATE[day.weekday()]
        if needs_recovery and i < 2 and session != "REST":
            session, detail = RECOVERY_DAY
        plan.append({
            "date": day.isoformat(),
            "dow": day.strftime("%a"),
            "session": session,
            "detail": detail,
            "sleep_target": SLEEP_TARGET_H,
            "constants": DAILY_CONSTANTS,
        })
        day += datetime.timedelta(days=1)
    return plan


# ============================================================================
# Optional AI analysis (bring your own key: Anthropic or OpenAI)
# ============================================================================
AI_PROMPT = (
    "You are analyzing personal fitness-tracker statistics for the device owner. "
    "Write a concise narrative analysis (200-300 words): what the trends show, "
    "any patterns worth noticing across metrics, and 3 specific, practical "
    "recommendations. Be direct and specific to the numbers. Plain prose, no "
    "headers or bullet lists. End with one sentence noting this is not medical advice."
)


def build_ai_stats(daily: list, acts: list, vo2: list) -> dict:
    """Compact summary sent to the AI - aggregates only, never raw history."""
    rows = sorted([r for r in daily if r.get("date")], key=lambda r: str(r["date"]))[-30:]

    def series(key):
        return [fnum(r.get(key)) for r in rows]

    def agg(vals):
        v = [x for x in vals if x is not None]
        if not v:
            return None
        return {"avg": round(statistics.mean(v), 2), "min": min(v), "max": max(v),
                "first7_avg": round(statistics.mean(v[:7]), 2) if len(v) >= 14 else None,
                "last7_avg": round(statistics.mean(v[-7:]), 2)}

    weekly_load = {}
    for a in acts or []:
        tl = fnum(a.get("training_load"))
        if tl is None or not a.get("date"):
            continue
        dt = datetime.date.fromisoformat(str(a["date"]))
        monday = (dt - datetime.timedelta(days=dt.weekday())).isoformat()
        weekly_load[monday] = round(weekly_load.get(monday, 0) + tl)

    vo2s = [fnum(r.get("vo2max")) for r in sorted(vo2 or [], key=lambda r: str(r.get("date")))]
    vo2s = [v for v in vo2s if v is not None]

    return {
        "period": f"{rows[0]['date']} to {rows[-1]['date']}" if rows else "no data",
        "days": len(rows),
        "sleep_hours": agg(series("sleep_hours")),
        "sleep_score": agg(series("sleep_score")),
        "hrv_ms": agg(series("hrv_last_night_avg")),
        "hrv_balanced_range": [fnum(rows[-1].get("hrv_baseline_balanced_low")),
                                fnum(rows[-1].get("hrv_baseline_balanced_high"))] if rows else None,
        "resting_hr": agg(series("resting_hr")),
        "overnight_spo2_pct": agg(series("sleep_avg_spo2")),
        "readiness": agg(series("readiness_score")),
        "steps": agg(series("steps")),
        "stress": agg(series("avg_stress")),
        "weight_kg": agg(series("weight_kg")),
        "weekly_training_load": dict(sorted(weekly_load.items())[-5:]),
        "vo2max_first_last": [vo2s[0], vo2s[-1]] if len(vo2s) >= 2 else None,
        "current_lamp": evaluate_latest(daily),
    }


def _pick_gemini_model(api_key: str) -> str:
    """Query Google's model list and pick the newest generateContent flash
    model, so retired IDs don't 404."""
    import re
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key})
        with urllib.request.urlopen(req, timeout=30) as resp:
            models = json.loads(resp.read().decode("utf-8")).get("models", [])
        names = []
        for m in models:
            name = (m.get("name") or "").split("/")[-1]
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            low = name.lower()
            if "flash" not in low or any(x in low for x in (
                    "image", "live", "tts", "audio", "thinking", "exp", "preview", "8b")):
                continue
            names.append(name)
        if names:
            def ver(n):
                m2 = re.search(r"gemini-(\d+)(?:\.(\d+))?", n)
                return (int(m2.group(1)), int(m2.group(2) or 0)) if m2 else (0, 0)
            names.sort(key=ver, reverse=True)
            return names[0]
    except Exception:  # noqa: BLE001
        pass
    return "gemini-2.5-flash"


def ai_narrative(provider: str, api_key: str, model: str, stats: dict) -> str:
    """Call Anthropic or OpenAI with the compact stats; return the narrative."""
    import urllib.request
    user_msg = AI_PROMPT + "\n\nStatistics (JSON):\n" + json.dumps(stats, default=str)
    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        payload = {"model": model or "claude-sonnet-4-6", "max_tokens": 1024,
                   "messages": [{"role": "user", "content": user_msg}]}
        headers = {"Content-Type": "application/json", "x-api-key": api_key,
                   "anthropic-version": "2023-06-01"}
    elif provider == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        payload = {"model": model or "gpt-4o-mini", "max_tokens": 1024,
                   "messages": [{"role": "user", "content": user_msg}]}
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {api_key}"}
    elif provider == "gemini":
        gmodel = model or _pick_gemini_model(api_key)
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{gmodel}:generateContent")
        payload = {"contents": [{"parts": [{"text": user_msg}]}],
                   "generationConfig": {"maxOutputTokens": 3000}}
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    elif provider == "custom":
        # OpenAI-compatible endpoint: model must be "base_url|model_name"
        if "|" not in (model or ""):
            raise ValueError('custom provider needs --ai-model "BASE_URL|MODEL", '
                             'e.g. "https://api.groq.com/openai/v1|llama-3.3-70b-versatile"')
        base, cmodel = model.split("|", 1)
        url = base.rstrip("/") + "/chat/completions"
        payload = {"model": cmodel, "max_tokens": 1500,
                   "messages": [{"role": "user", "content": user_msg}]}
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {api_key}"}
    else:
        raise ValueError(f"Unknown provider: {provider}")
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if provider == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()
    if provider == "gemini":
        cands = data.get("candidates") or []
        if not cands:
            raise ValueError("Gemini returned no candidates (possibly safety-blocked)")
        parts = cands[0].get("content", {}).get("parts", [])
        text = "".join(pt.get("text", "") for pt in parts
                       if not pt.get("thought")).strip()
        if cands[0].get("finishReason") == "MAX_TOKENS":
            text += "\n\n[analysis truncated by token limit]"
        return text
    return data["choices"][0]["message"]["content"].strip()


# ---- AI keyring: per-provider keys saved in <out>/ai_config.json ----------
def _ai_cfg_path(out_dir: Path) -> Path:
    return out_dir / "ai_config.json"


def _load_ai_cfg(out_dir: Path) -> dict:
    try:
        cfg = json.loads(_ai_cfg_path(out_dir).read_text(encoding="utf-8"))
        if "providers" in cfg:
            return cfg
    except Exception:  # noqa: BLE001
        pass
    return {"active": "", "providers": {}}


def _save_ai_cfg(out_dir: Path, cfg: dict) -> None:
    path = _ai_cfg_path(out_dir)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    try:  # best-effort: owner-only permissions (no-op on Windows)
        import os as _os
        _os.chmod(path, 0o600)
    except OSError:
        pass


def resolve_ai(out_dir: Path, provider: str, cli_key: str, cli_model: str):
    """Returns (provider, key, model) using CLI > keyring > env var.
    Saves new keys/models into the keyring. Raises ValueError with a clear
    message when nothing usable is found."""
    import os as _os
    cfg = _load_ai_cfg(out_dir)
    provider = provider or cfg["active"]
    if not provider:
        raise ValueError("no provider given and none saved - use --ai-provider")
    entry = dict(cfg["providers"].get(provider, {}))

    env_var = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
               "gemini": "GEMINI_API_KEY", "custom": "CUSTOM_AI_KEY"}.get(provider)
    key = cli_key or entry.get("key") or (_os.getenv(env_var) if env_var else None)
    if not key:
        raise ValueError(f"no key for '{provider}' - pass --ai-key once to save it")

    model = cli_model or entry.get("model") or ""
    if provider == "custom" and "|" not in model:
        raise ValueError('custom provider needs --ai-model "BASE_URL|MODEL" (saved after first use)')

    # persist anything new
    changed = False
    if cli_key and entry.get("key") != cli_key:
        entry["key"] = cli_key
        changed = True
    if cli_model and entry.get("model") != cli_model:
        entry["model"] = cli_model
        changed = True
    if key and not entry.get("key"):
        entry["key"] = key  # env-sourced key gets saved for next time
        changed = True
    if changed or cfg["active"] != provider or provider not in cfg["providers"]:
        cfg["providers"][provider] = entry
        cfg["active"] = provider
        _save_ai_cfg(out_dir, cfg)
    return provider, key, model


def forget_ai_key(out_dir: Path, provider: str) -> str:
    cfg = _load_ai_cfg(out_dir)
    if provider not in cfg["providers"]:
        return f"no saved key for '{provider}'"
    cfg["providers"].pop(provider)
    if cfg["active"] == provider:
        cfg["active"] = next(iter(cfg["providers"]), "")
    _save_ai_cfg(out_dir, cfg)
    remaining = ", ".join(cfg["providers"]) or "none"
    return f"removed '{provider}' key (remaining: {remaining})"


# ============================================================================
# RENDER - offline dashboard with embedded history + range selector
# ============================================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Health Console</title>
<style>
  :root { --ink:#0e1520; --panel:#151f2d; --panel-2:#19243475; --line:#24344a;
    --snow:#e9f0f7; --mist:#8fa3ba; --dim:#5c7089; --oxy:#5bc8de;
    --green:#74c687; --amber:#e3ae5d; --red:#de6b5e; --violet:#9d8cff; }
  * { margin:0; padding:0; box-sizing:border-box; }
  html { background:var(--ink); }
  body { font-family:"Consolas","Cascadia Mono",ui-monospace,"Courier New",monospace;
    background:radial-gradient(1200px 500px at 80% -10%, #1a2a4022 0%, transparent 60%),var(--ink);
    color:var(--snow); min-height:100vh; padding:0 clamp(14px,3vw,40px) 60px; }
  header { padding-top:26px; }
  .masthead { display:flex; align-items:baseline; justify-content:space-between; flex-wrap:wrap; gap:8px; }
  .masthead h1 { font-family:"Bahnschrift","Arial Narrow",sans-serif; font-weight:700;
    font-size:clamp(28px,4vw,44px); letter-spacing:0.05em; text-transform:uppercase; font-stretch:condensed; }
  .masthead h1 .thin { color:var(--dim); font-weight:400; }
  .masthead .stamp { color:var(--mist); font-size:12px; letter-spacing:0.08em; }
  .pulse-wrap { margin:12px 0 4px; }
  .pulse-wrap svg { display:block; width:100%; height:44px; }
  /* range selector */
  .range-bar { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:14px 0 22px;
    border:1px solid var(--line); border-radius:10px; background:var(--panel-2); padding:10px 14px; }
  .range-bar .lbl { font-size:11px; color:var(--dim); letter-spacing:0.08em; text-transform:uppercase; margin-right:4px; }
  .range-bar button { font-family:inherit; font-size:12px; letter-spacing:0.05em; cursor:pointer;
    background:transparent; color:var(--mist); border:1px solid var(--line); border-radius:6px; padding:5px 12px; }
  .range-bar button:hover { border-color:var(--oxy); color:var(--oxy); }
  .range-bar button.on { background:#5bc8de1a; border-color:var(--oxy); color:var(--oxy); }
  .range-bar .custom { display:flex; align-items:center; gap:6px; margin-left:auto; font-size:11px; color:var(--dim); }
  .range-bar input[type=date] { background:var(--ink); color:var(--snow); border:1px solid var(--line);
    border-radius:5px; padding:4px 6px; font-family:inherit; font-size:11px; }
  .range-bar .n { font-size:11px; color:var(--dim); margin-left:6px; }
  .lamp-row { display:grid; grid-template-columns:minmax(220px,1.2fr) repeat(4,minmax(140px,1fr));
    gap:12px; margin:0 0 26px; }
  .lamp,.chip { border:1px solid var(--line); border-radius:10px; background:var(--panel); padding:14px 16px; }
  .lamp { display:flex; align-items:center; gap:16px; }
  .lamp .dot { width:46px; height:46px; border-radius:50%; flex:none;
    box-shadow:0 0 0 5px #ffffff08, 0 0 26px 2px var(--lampglow,transparent); }
  .lamp.green .dot { background:var(--green); --lampglow:#74c68766; }
  .lamp.amber .dot { background:var(--amber); --lampglow:#e3ae5d66; }
  .lamp.red .dot { background:var(--red); --lampglow:#de6b5e66; }
  .lamp .word { font-family:"Bahnschrift","Arial Narrow",sans-serif; font-weight:700; font-size:34px;
    letter-spacing:0.06em; text-transform:uppercase; line-height:1; }
  .lamp.green .word { color:var(--green); } .lamp.amber .word { color:var(--amber); } .lamp.red .word { color:var(--red); }
  .lamp .sub { font-size:11px; color:var(--mist); margin-top:5px; }
  .chip .k { font-size:11px; color:var(--dim); letter-spacing:0.08em; text-transform:uppercase; }
  .chip .v { font-family:"Bahnschrift","Arial Narrow",sans-serif; font-weight:600; font-size:30px;
    line-height:1.1; margin-top:4px; }
  .chip .v small { font-size:15px; color:var(--mist); font-weight:400; }
  .chip .d { font-size:11px; margin-top:3px; color:var(--dim); }
  .chip.ok .v { color:var(--green); } .chip.warn .v { color:var(--amber); } .chip.bad .v { color:var(--red); }
  h2.sect { font-family:"Bahnschrift","Arial Narrow",sans-serif; font-weight:600; font-size:19px;
    letter-spacing:0.1em; text-transform:uppercase; color:var(--mist); margin:30px 0 12px;
    display:flex; align-items:center; gap:12px; }
  h2.sect::after { content:""; flex:1; height:1px; background:var(--line); }
  .insight-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:12px; }
  .insight { border:1px solid var(--line); border-left:3px solid var(--dim); border-radius:8px;
    background:var(--panel); padding:12px 14px; }
  .insight.good { border-left-color:var(--green); }
  .insight.warn { border-left-color:var(--amber); }
  .insight.bad { border-left-color:var(--red); }
  .insight .t { font-size:13px; font-weight:700; margin-bottom:4px; }
  .insight.good .t { color:var(--green); } .insight.warn .t { color:var(--amber); } .insight.bad .t { color:var(--red); }
  .insight .x { font-size:11.5px; color:var(--mist); line-height:1.5; }
  .sect-note { font-size:10.5px; color:var(--dim); margin:-8px 0 12px; }
  .actions { border:1px solid var(--line); border-radius:10px; background:var(--panel); padding:6px 18px; }
  .actions ol { list-style:none; counter-reset:act; }
  .actions li { counter-increment:act; padding:11px 0 11px 38px; position:relative;
    font-size:12.5px; line-height:1.55; color:var(--snow); border-bottom:1px solid var(--line); }
  .actions li:last-child { border-bottom:none; }
  .actions li::before { content:counter(act,decimal-leading-zero); position:absolute; left:0; top:11px;
    font-family:"Bahnschrift","Arial Narrow",sans-serif; color:var(--oxy); font-weight:700; font-size:15px; }
  .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
  .card { border:1px solid var(--line); border-radius:10px; background:var(--panel);
    padding:16px 16px 10px; min-width:0; }
  .card.wide { grid-column:1/-1; }
  .card h3 { font-family:"Bahnschrift","Arial Narrow",sans-serif; font-weight:600; font-size:17px;
    letter-spacing:0.08em; text-transform:uppercase; display:flex; justify-content:space-between;
    align-items:baseline; gap:10px; flex-wrap:wrap; }
  .card h3 .note { font-size:10px; color:var(--dim); letter-spacing:0; text-transform:none; font-weight:400; }
  .ci { margin-top:6px; border-left:3px solid var(--dim); border-radius:4px;
    background:#0e152066; padding:6px 10px; font-size:11px; color:var(--mist); line-height:1.5; }
  .ci.good { border-left-color:var(--green); }
  .ci.warn { border-left-color:var(--amber); }
  .ci.bad { border-left-color:var(--red); }
  .chartbox { margin-top:8px; }
  .chartbox svg { display:block; width:100%; height:auto; }
  .legend { display:flex; flex-wrap:wrap; gap:12px; margin:6px 0 4px; font-size:10px; color:var(--mist); }
  .legend span { display:inline-flex; align-items:center; gap:5px; }
  .legend i { width:10px; height:10px; border-radius:2px; display:inline-block; }
  .legend span.toggle { cursor:pointer; user-select:none; }
  .legend span.toggle.off { opacity:0.35; text-decoration:line-through; }
  #tip { position:fixed; z-index:50; pointer-events:none; display:none;
    background:#0e1520f2; border:1px solid var(--line); border-radius:7px;
    padding:8px 10px; font-size:11px; color:var(--snow); max-width:240px;
    box-shadow:0 6px 18px #00000066; }
  #tip .th { color:var(--mist); margin-bottom:4px; font-weight:700; }
  #tip .tr { display:flex; align-items:center; gap:6px; color:var(--mist); }
  #tip .tr i { width:8px; height:8px; border-radius:2px; display:inline-block; flex:none; }
  #tip .tr b { color:var(--snow); margin-left:auto; padding-left:10px; }
  .chartbox { position:relative; }
  .chartbox svg { cursor:crosshair; }
  .zoom-hint { font-size:9.5px; color:var(--dim); text-align:right; margin-top:2px; }
  .plan { border:1px solid var(--line); border-radius:10px; background:var(--panel); overflow:hidden; }
  .plan-row { display:grid; grid-template-columns:96px 1fr 80px; border-bottom:1px solid var(--line); }
  .plan-row:last-child { border-bottom:none; }
  .plan-row > div { padding:10px 12px; font-size:12px; }
  .p-date { color:var(--snow); font-weight:700; }
  .p-date small { display:block; color:var(--dim); font-weight:400; font-size:10px; }
  .p-body .s { color:var(--snow); font-weight:700; }
  .p-body .dt { color:var(--mist); font-size:11px; margin-top:2px; line-height:1.5; }
  .p-body .cn { color:var(--dim); font-size:10px; margin-top:4px; }
  .p-sleep { color:var(--oxy); align-self:center; text-align:right; font-size:11px; }
  details.thresholds { margin:0 0 20px; border:1px solid var(--line); border-radius:10px;
    background:var(--panel-2); font-size:12px; }
  details.thresholds summary { cursor:pointer; padding:10px 16px; color:var(--mist);
    letter-spacing:0.06em; text-transform:uppercase; font-size:11px; }
  details.thresholds .rows { padding:4px 16px 14px; display:flex; flex-wrap:wrap; gap:16px; }
  details.thresholds label { color:var(--mist); display:flex; align-items:center; gap:8px; }
  details.thresholds input { width:74px; background:var(--ink); color:var(--snow);
    border:1px solid var(--line); border-radius:5px; padding:4px 6px; font-family:inherit; font-size:12px; }
  :focus-visible { outline:2px solid var(--oxy); outline-offset:2px; }
  footer { margin-top:26px; font-size:11px; color:var(--dim); }
  @media (max-width:900px) {
    .lamp-row { grid-template-columns:1fr 1fr; } .lamp { grid-column:1/-1; }
    .grid { grid-template-columns:1fr; }
    .range-bar .custom { margin-left:0; }
  }
</style>
</head>
<body>

<header>
  <div class="masthead">
    <h1>Health <span class="thin">Console</span></h1>
    <div class="stamp" id="stamp"></div>
  </div>
  <div class="pulse-wrap" aria-hidden="true">
    <svg viewBox="0 0 1000 44" preserveAspectRatio="none">
      <path d="M0,26 L120,26 140,26 150,12 160,38 170,8 182,40 192,22 205,26 380,26 400,26 410,14 420,36 430,10 442,38 452,22 465,26 640,26 660,26 670,13 680,37 690,9 702,39 712,22 725,26 1000,26"
        fill="none" stroke="#3d5578" stroke-width="1.5"/>
      <path d="M140,26 150,12 160,38 170,8 182,40 192,22 205,26" fill="none" stroke="#5bc8de" stroke-width="1.5" opacity="0.9"/>
    </svg>
  </div>
</header>

<main>
<div id="tip"></div>
  <div class="range-bar" role="group" aria-label="Date range">
    <span class="lbl">Range</span>
    <button data-days="7">7D</button>
    <button data-days="14">14D</button>
    <button data-days="30" class="on">30D</button>
    <button data-days="90">90D</button>
    <button data-days="365">1Y</button>
    <button data-days="0">ALL</button>
    <span class="custom">
      from <input type="date" id="rangeFrom"> to <input type="date" id="rangeTo">
      <button id="rangeApply">Apply</button>
    </span>
    <span class="n" id="rangeInfo"></span>
  </div>

  <div class="lamp-row">
    <div class="lamp" id="lamp"><div class="dot"></div>
      <div><div class="word" id="lampWord">–</div><div class="sub" id="lampSub">latest morning check</div></div>
    </div>
    <div class="chip" id="chipSleep"><div class="k">Sleep last night</div><div class="v">–</div><div class="d"></div></div>
    <div class="chip" id="chipHrv"><div class="k">Overnight HRV</div><div class="v">–</div><div class="d"></div></div>
    <div class="chip" id="chipRhr"><div class="k">Resting HR</div><div class="v">–</div><div class="d"></div></div>
    <div class="chip" id="chipSpo2"><div class="k">Overnight SpO2</div><div class="v">–</div><div class="d"></div></div>
  </div>

  <details class="thresholds">
    <summary>Signal thresholds (green / amber / red rules)</summary>
    <div class="rows">
      <label>Min sleep h <input type="number" step="0.1" id="thSleep" value="__TH_SLEEP__"></label>
      <label>RHR over baseline <input type="number" step="1" id="thRhr" value="__TH_RHR__"></label>
      <label>Min overnight SpO2 <input type="number" step="0.5" id="thSpo2" value="__TH_SPO2__"></label>
      <label>Hard-red SpO2 <input type="number" step="0.5" id="thSpo2Red" value="__TH_SPO2_RED__"></label>
    </div>
  </details>

  <h2 class="sect">Insights</h2>
  <div class="sect-note">Computed on the most recent data at build time (not the selected range).</div>
  <div class="insight-grid" id="insights"></div>

  <div id="aiWrap" hidden>
    <h2 class="sect">AI analysis <span style="font-size:11px;color:var(--dim);letter-spacing:0;text-transform:none;" id="aiMeta"></span></h2>
    <div class="actions" style="padding:16px 18px;"><div id="aiText" style="font-size:12.5px;line-height:1.7;color:var(--snow);white-space:pre-wrap;"></div></div>
  </div>

  <h2 class="sect">Recommended actions</h2>
  <div class="actions"><ol id="actions"></ol></div>

  <h2 class="sect">Trends <span style="font-size:11px;color:var(--dim);letter-spacing:0;text-transform:none;">— follow the selected range</span></h2>
  <div class="grid">
    <div class="card wide"><h3>Sleep vs 7 h target <span class="note">bars colored by Garmin score · cyan = 7-day avg · dashed = target</span></h3><div class="chartbox" id="cSleep"></div></div>
    <div class="card"><h3>Overnight HRV <span class="note">band = balanced range · dashed = 7d avg</span></h3><div class="chartbox" id="cHrv"></div></div>
    <div class="card"><h3>Resting heart rate <span class="note">dashed = baseline · dotted = amber line</span></h3><div class="chartbox" id="cRhr"></div></div>
    <div class="card"><h3>Overnight &amp; lowest SpO2 <span class="note">grey = daily low (brief dips)</span></h3><div class="chartbox" id="cSpo2"></div></div>
    <div class="card"><h3>Training readiness <span class="note">Garmin 0–100</span></h3><div class="chartbox" id="cReady"></div></div>
    <div class="card"><h3>Steps <span class="note">daily total · cyan = 7-day avg</span></h3><div class="chartbox" id="cSteps"></div></div>
    <div class="card"><h3>Active hours <span class="note">bars = intensity time (moderate+vigorous) · cyan line = logged sessions</span></h3><div class="legend" id="activeLegend"></div><div class="chartbox" id="cActive"></div></div>
    <div class="card" id="stagesCard" hidden><h3>Sleep stages <span class="note">minutes per night</span></h3><div class="legend" id="stagesLegend"></div><div class="chartbox" id="cStages"></div></div>
    <div class="card" id="weightCard" hidden><h3>Weight <span class="note">kg, weigh-in days only</span></h3><div class="chartbox" id="cWeight"></div></div>
    <div class="card wide" id="loadCard" hidden><h3>Weekly training load <span class="note">stacked by activity type</span></h3><div class="legend" id="loadLegend"></div><div class="chartbox" id="cLoad"></div></div>
    <div class="card" id="vo2Card" hidden><h3>VO2max trend <span class="note">solid = overall · dashed = cycling</span></h3><div class="chartbox" id="cVo2"></div></div>
  </div>

  <h2 class="sect">Day-by-day plan</h2>
  <div class="sect-note">Lamp-adaptive: amber morning → downgrade the day to easy; red morning → full rest, regardless of what's written.</div>
  <div class="plan" id="plan"></div>

  <footer id="foot"></footer>
</main>

<script>
"use strict";
const EMBED = __EMBED_JSON__;

const C = { oxy:"#5bc8de", green:"#74c687", amber:"#e3ae5d", red:"#de6b5e",
  mist:"#8fa3ba", dim:"#5c7089", line:"#24344a", violet:"#9d8cff", snow:"#e9f0f7", blue:"#3f6ea8" };

const fmt1 = v => v == null || isNaN(v) ? '–' : String(Math.round(v * 10) / 10);
const mean = a => a.length ? a.reduce((s,v)=>s+v,0)/a.length : null;
const median = a => { if(!a.length) return null; const s=[...a].sort((x,y)=>x-y);
  const m=Math.floor(s.length/2); return s.length%2 ? s[m] : (s[m-1]+s[m])/2; };
const rolling = (arr,n) => arr.map((_,i)=>{ const w=arr.slice(Math.max(0,i-n+1),i+1)
  .filter(v=>v!=null&&!isNaN(v)); return w.length?mean(w):null; });
const num = v => (v==null||v===''||isNaN(v)) ? null : +v;

const NS='http://www.w3.org/2000/svg';
function el(tag,attrs,parent){ const n=document.createElementNS(NS,tag);
  for(const k in attrs) n.setAttribute(k,attrs[k]); if(parent) parent.appendChild(n); return n; }
function niceTicks(min,max,count){ const span=max-min||1; const step0=span/count;
  const mag=Math.pow(10,Math.floor(Math.log10(step0)));
  const step=[1,2,2.5,5,10].map(m=>m*mag).find(s=>span/s<=count)||mag*10;
  const t=[]; for(let x=Math.ceil(min/step)*step;x<=max+1e-9;x+=step) t.push(Math.round(x*100)/100); return t; }

function svgChart(containerId,{labels,series=[],area=null,hlines=[],yMin=null,yMax=null,height=230,stacked=false,fullLabels=null,chartKey=null}){
  const box=document.getElementById(containerId); box.innerHTML='';
  const hiddenSet=(chartKey&&HIDDEN[chartKey])||new Set();
  const vis=series.filter(s=>!(s.label&&hiddenSet.has(s.label)));
  const W=720,H=height,padL=44,padR=8,padT=10,padB=24, iw=W-padL-padR, ih=H-padT-padB;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'},box);
  let lo=yMin,hi=yMax; const vals=[];
  if(stacked){ for(let i=0;i<labels.length;i++){ let s=0;
    vis.forEach(d=>{const v=num(d.data[i]); if(v!=null)s+=v;}); vals.push(s);} }
  else { vis.forEach(d=>d.data.forEach(v=>{const n=num(v); if(n!=null)vals.push(n);}));
    if(area)[...area.low,...area.high].forEach(v=>{const n=num(v); if(n!=null)vals.push(n);}); }
  hlines.forEach(h=>vals.push(h.y));
  if(!vals.length){ el('text',{x:W/2,y:H/2,'text-anchor':'middle','font-size':11,fill:C.dim},svg)
    .textContent='no data in range'; return; }
  if(lo==null)lo=Math.min(...vals); if(hi==null)hi=Math.max(...vals);
  if(lo===hi){lo-=1;hi+=1;} const pad=(hi-lo)*0.08;
  if(yMin==null)lo-=pad; if(yMax==null)hi+=pad;
  const n=labels.length;
  const X=i=>padL+(n<=1?iw/2:i*(iw/(n-1)));
  const Xb=i=>padL+(i+0.5)*(iw/n);
  const bw=Math.max(1.5,(iw/n)*0.66);
  const Y=v=>padT+ih-((v-lo)/(hi-lo))*ih;
  const hasBars=vis.some(s=>s.type==='bar'); const xOf=hasBars?Xb:X;
  niceTicks(lo,hi,5).forEach(t=>{ el('line',{x1:padL,x2:W-padR,y1:Y(t),y2:Y(t),
    stroke:C.line,'stroke-opacity':0.5,'stroke-width':1},svg);
    el('text',{x:padL-6,y:Y(t)+3,'text-anchor':'end','font-size':9,fill:C.dim},svg).textContent=t; });
  const every=Math.ceil(n/10);
  labels.forEach((l,i)=>{ if(i%every)return;
    el('text',{x:xOf(i),y:H-8,'text-anchor':'middle','font-size':9,fill:C.dim},svg).textContent=l; });
  if(area){ let d=''; labels.forEach((_,i)=>{const v=num(area.high[i]); if(v==null)return;
      d+=(d?'L':'M')+xOf(i)+','+Y(v)+' ';});
    for(let i=n-1;i>=0;i--){const v=num(area.low[i]); if(v==null)continue;
      d+='L'+xOf(i)+','+Y(v)+' ';}
    if(d) el('path',{d:d+'Z',fill:area.color,stroke:'none'},svg); }
  const acc=new Array(n).fill(0);
  vis.filter(s=>s.type==='bar').forEach(s=>{ labels.forEach((lab,i)=>{
    const v=num(s.data[i]); if(v==null)return;
    const base=stacked?acc[i]:0, y0=Y(base), y1=Y(base+v);
    el('rect',{x:xOf(i)-bw/2,y:Math.min(y0,y1),width:bw,
      height:Math.max(1,Math.abs(y0-y1)),fill:s.colors?s.colors[i]:s.color,rx:1.5},svg);
    if(stacked)acc[i]=base+v; }); });
  hlines.forEach(h=>{ el('line',{x1:padL,x2:W-padR,y1:Y(h.y),y2:Y(h.y),stroke:h.color,
    'stroke-width':1,'stroke-dasharray':h.dash||'6 4','stroke-opacity':0.8},svg);
    if(h.label) el('text',{x:W-padR-4,y:Y(h.y)-4,'text-anchor':'end','font-size':9,fill:h.color},svg).textContent=h.label; });
  vis.filter(s=>s.type==='line').forEach(s=>{ let d='',on=false;
    labels.forEach((_,i)=>{ const v=num(s.data[i]);
      if(v==null){on=false;return;} d+=(on?'L':'M')+xOf(i)+','+Y(v)+' '; on=true; });
    if(d) el('path',{d,fill:'none',stroke:s.color,'stroke-width':s.width||2,
      'stroke-dasharray':s.dash||'none','stroke-linejoin':'round','stroke-linecap':'round'},svg);
    if(s.points&&n<=120) labels.forEach((_,i)=>{ const v=num(s.data[i]); if(v==null)return;
      el('circle',{cx:xOf(i),cy:Y(v),r:2.4,fill:s.color},svg); }); });

  // ---------- interaction layer: crosshair + tooltip + drag-zoom ----------
  const cross=el('line',{x1:0,x2:0,y1:padT,y2:padT+ih,stroke:C.mist,
    'stroke-width':1,'stroke-dasharray':'3 3',opacity:0},svg);
  const marks=vis.filter(s=>s.type==='line').map(s=>
    el('circle',{r:3.4,fill:'none',stroke:s.color,'stroke-width':2,opacity:0},svg));
  const selRect=el('rect',{x:0,y:padT,width:0,height:ih,fill:'#5bc8de22',
    stroke:'#5bc8de55','stroke-width':1,opacity:0},svg);
  const tip=document.getElementById('tip');
  const toVX=clientX=>{ const r=svg.getBoundingClientRect();
    return (clientX-r.left)/r.width*W; };
  const idxAt=vx=>{
    let i = hasBars ? Math.floor((vx-padL)/(iw/n)) : Math.round((vx-padL)/(iw/(Math.max(1,n-1))));
    return Math.max(0,Math.min(n-1,i)); };
  let dragFrom=null;
  function showTip(evt){
    const vx=toVX(evt.clientX); if(vx<padL-6||vx>W-padR+6){ hideTip(); return; }
    const i=idxAt(vx), cx=xOf(i);
    cross.setAttribute('x1',cx); cross.setAttribute('x2',cx); cross.setAttribute('opacity',0.6);
    let mi=0;
    let html='<div class="th">'+((fullLabels&&fullLabels[i])||labels[i])+'</div>';
    vis.forEach(s=>{
      const v=num(s.data[i]);
      if(s.type==='line'&&marks[mi]){ const m=marks[mi++];
        if(v!=null){ m.setAttribute('cx',cx); m.setAttribute('cy',Y(v)); m.setAttribute('opacity',1); }
        else m.setAttribute('opacity',0); }
      if(v==null||!s.label) return;
      const col=s.colors?s.colors[i]:s.color;
      html+='<div class="tr"><i style="background:'+col+'"></i>'+s.label+'<b>'+fmt1(v)+'</b></div>';
    });
    tip.innerHTML=html; tip.style.display='block';
    const tw=tip.offsetWidth||160, th_=tip.offsetHeight||40;
    let tx=evt.clientX+14, ty=evt.clientY-th_-10;
    if(tx+tw>window.innerWidth-8) tx=evt.clientX-tw-14;
    if(ty<8) ty=evt.clientY+16;
    tip.style.left=tx+'px'; tip.style.top=ty+'px';
    if(dragFrom!=null){ const a=Math.min(dragFrom,vx),b=Math.max(dragFrom,vx);
      selRect.setAttribute('x',a); selRect.setAttribute('width',b-a); selRect.setAttribute('opacity',1); }
  }
  function hideTip(){ tip.style.display='none'; cross.setAttribute('opacity',0);
    marks.forEach(m=>m.setAttribute('opacity',0)); }
  svg.addEventListener('mousemove',showTip);
  svg.addEventListener('mouseleave',()=>{ hideTip();
    if(dragFrom!=null){ dragFrom=null; selRect.setAttribute('opacity',0);} });
  if(fullLabels){
    svg.addEventListener('mousedown',e=>{ dragFrom=toVX(e.clientX); e.preventDefault(); });
    svg.addEventListener('mouseup',e=>{
      if(dragFrom==null) return;
      const a=idxAt(Math.min(dragFrom,toVX(e.clientX)));
      const b=idxAt(Math.max(dragFrom,toVX(e.clientX)));
      dragFrom=null; selRect.setAttribute('opacity',0);
      if(b-a>=1) setCustomRange(String(fullLabels[a]),String(fullLabels[b]));
    });
    svg.addEventListener('dblclick',()=>setCustomRange(null,null));
    const hint=document.createElement('div'); hint.className='zoom-hint';
    hint.textContent='drag to zoom · double-click to reset';
    box.appendChild(hint);
  }
}
function legend(id,items,chartKey){ const box=document.getElementById(id); box.innerHTML='';
  const hid=chartKey?(HIDDEN[chartKey]??=new Set()):null;
  items.forEach(([name,color])=>{ const s=document.createElement('span');
    const i=document.createElement('i'); i.style.background=color;
    s.appendChild(i); s.appendChild(document.createTextNode(name));
    if(hid){ s.className='toggle'+(hid.has(name)?' off':'');
      s.setAttribute('role','button'); s.setAttribute('tabindex','0');
      const flip=()=>{ hid.has(name)?hid.delete(name):hid.add(name); render(); };
      s.addEventListener('click',flip);
      s.addEventListener('keydown',e=>{ if(e.key==='Enter'||e.key===' '){e.preventDefault();flip();} }); }
    box.appendChild(s); }); }

function setChartInsight(containerId, level, text){
  const box=document.getElementById(containerId); if(!box) return;
  const card=box.closest?box.closest('.card'):null; if(!card) return;
  let ci=card.querySelector('.ci');
  if(!text){ if(ci) ci.remove(); return; }
  if(!ci){ ci=document.createElement('div');
    const h3=card.querySelector('h3');
    h3&&h3.nextSibling?card.insertBefore(ci,h3.nextSibling):card.appendChild(ci); }
  ci.className='ci '+level; ci.textContent=text;
}
function trendOf(vals){ // second half avg minus first half avg
  const v=vals.filter(x=>x!=null&&!isNaN(x));
  if(v.length<6) return null;
  const h=Math.floor(v.length/2);
  return mean(v.slice(h))-mean(v.slice(0,h));
}
function arrow(d,dec=1){ if(d==null) return '\u2013';
  const r=Math.round(d*Math.pow(10,dec))/Math.pow(10,dec);
  return r>0?('\u2197 +'+r):r<0?('\u2198 '+r):'\u2192 flat'; }

function evalDay(row,baseRhr,th){
  const flags=[]; let hard=false;
  const sleep=num(row.sleep_hours), hrv=num(row.hrv_last_night_avg),
    rhr=num(row.resting_hr), spo2=num(row.sleep_avg_spo2),
    balLow=num(row.hrv_baseline_balanced_low), low=num(row.hrv_baseline_low);
  if(sleep!=null&&sleep<th.sleep) flags.push('sleep '+fmt1(sleep)+'h');
  if(hrv!=null&&balLow!=null&&hrv<balLow) flags.push('HRV '+hrv+' below balanced');
  if(hrv!=null&&low!=null&&hrv<low) hard=true;
  if(rhr!=null&&baseRhr!=null&&rhr>baseRhr+th.rhr) flags.push('RHR +'+(rhr-baseRhr).toFixed(0));
  if(spo2!=null&&spo2<th.spo2) flags.push('SpO2 '+fmt1(spo2)+'%');
  if(spo2!=null&&spo2<th.spo2Red) hard=true;
  return { color: hard||flags.length>=2 ? 'red' : flags.length===1 ? 'amber' : 'green', flags };
}
function setChip(id,val,unit,classify,detail){ const e=document.getElementById(id);
  e.className='chip '+(val==null?'':classify(val));
  e.querySelector('.v').innerHTML=val==null?'–':fmt1(val)+'<small>'+unit+'</small>';
  e.querySelector('.d').textContent=detail||''; }

// ---------- range state ----------
const ALL = EMBED.daily.filter(r=>r.date).sort((a,b)=>String(a.date).localeCompare(String(b.date)));
const firstDate = ALL.length ? String(ALL[0].date) : null;
const lastDate = ALL.length ? String(ALL[ALL.length-1].date) : null;
let range = { from:null, to:null };  // ISO strings or null
const HIDDEN = {};   // chartKey -> Set of hidden series labels
function setCustomRange(from,to){
  range={from,to};
  document.querySelectorAll('.range-bar button[data-days]').forEach(x=>x.classList.remove('on'));
  document.getElementById('rangeFrom').value=from||firstDate||'';
  document.getElementById('rangeTo').value=to||lastDate||'';
  render();
}

function setRangeDays(days){
  if(!lastDate) return;
  if(days===0){ range={from:null,to:null}; }
  else {
    const end=new Date(lastDate+'T00:00:00');
    const start=new Date(end); start.setDate(end.getDate()-(days-1));
    range={from:start.toISOString().slice(0,10), to:lastDate};
  }
  document.getElementById('rangeFrom').value=range.from||firstDate||'';
  document.getElementById('rangeTo').value=range.to||lastDate||'';
  render();
}
document.querySelectorAll('.range-bar button[data-days]').forEach(b=>{
  b.addEventListener('click',()=>{
    document.querySelectorAll('.range-bar button[data-days]').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    setRangeDays(+b.dataset.days);
  });
});
document.getElementById('rangeApply').addEventListener('click',()=>{
  const f=document.getElementById('rangeFrom').value, t=document.getElementById('rangeTo').value;
  if(f&&t&&f<=t){ range={from:f,to:t};
    document.querySelectorAll('.range-bar button[data-days]').forEach(x=>x.classList.remove('on'));
    render(); }
});
['thSleep','thRhr','thSpo2','thSpo2Red'].forEach(id=>
  document.getElementById(id).addEventListener('change',render));
window.addEventListener('error',e=>{ document.getElementById('foot').textContent =
  'Dashboard error: '+(e.message||e.type)+' — press F12 for details.'; });

function inRange(d){
  if(range.from&&d<range.from) return false;
  if(range.to&&d>range.to) return false;
  return true;
}

// ---------- render ----------
function render(){
  const th={ sleep:+document.getElementById('thSleep').value,
    rhr:+document.getElementById('thRhr').value,
    spo2:+document.getElementById('thSpo2').value,
    spo2Red:+document.getElementById('thSpo2Red').value };

  const rows=ALL.filter(r=>inRange(String(r.date)));
  const dates=rows.map(r=>String(r.date).slice(5));
  document.getElementById('rangeInfo').textContent =
    rows.length ? rows[0].date+' → '+rows[rows.length-1].date+' · '+rows.length+' days' : 'no data in range';
  document.getElementById('stamp').textContent =
    'HISTORY: '+(firstDate||'–')+' → '+(lastDate||'–')+' · '+ALL.length+' DAYS STORED · built '+EMBED.built;

  // lamp/chips always use the latest stored day ("today"), not the range
  const last = ALL[ALL.length-1] || {};
  const baseRhrAll = median(ALL.slice(-90).map(r=>num(r.resting_hr)).filter(v=>v!=null));
  const t=evalDay(last,baseRhrAll,th);
  const lamp=document.getElementById('lamp');
  lamp.className='lamp '+t.color;
  document.getElementById('lampWord').textContent=t.color;
  document.getElementById('lampSub').textContent=(t.flags.length?'flags: '+t.flags.join(' · '):'all clear')+' — '+(last.date||'');

  setChip('chipSleep',num(last.sleep_hours),'h',
    v=>v>=7?'ok':v>=th.sleep?'warn':'bad',
    last.sleep_score!=null?'score '+last.sleep_score+' '+(last.sleep_quality||''):'');
  const balLo=num(last.hrv_baseline_balanced_low), balHi=num(last.hrv_baseline_balanced_high);
  setChip('chipHrv',num(last.hrv_last_night_avg),' ms',
    v=>balLo!=null&&v<balLo?'bad':'ok',
    balLo!=null?'balanced '+balLo+'–'+balHi+' · 7d '+last.hrv_weekly_avg:'');
  setChip('chipRhr',num(last.resting_hr),' bpm',
    v=>v>baseRhrAll+th.rhr?'bad':v>baseRhrAll+2?'warn':'ok','baseline '+fmt1(baseRhrAll));
  setChip('chipSpo2',num(last.sleep_avg_spo2),'%',
    v=>v<th.spo2Red?'bad':v<th.spo2?'warn':'ok',
    last.lowest_spo2!=null?'day low '+last.lowest_spo2+'%':'');

  // insights + actions (precomputed at build time)
  const ig=document.getElementById('insights'); ig.innerHTML='';
  EMBED.insights.forEach(ins=>{ const d=document.createElement('div');
    d.className='insight '+ins.level;
    const t1=document.createElement('div'); t1.className='t'; t1.textContent=ins.title;
    const t2=document.createElement('div'); t2.className='x'; t2.textContent=ins.detail;
    d.appendChild(t1); d.appendChild(t2); ig.appendChild(d); });
  if(EMBED.ai&&EMBED.ai.text){ document.getElementById('aiWrap').hidden=false;
    document.getElementById('aiText').textContent=EMBED.ai.text;
    document.getElementById('aiMeta').textContent='— '+(EMBED.ai.provider||'')+' · '+(EMBED.ai.when||''); }
  const ao=document.getElementById('actions'); ao.innerHTML='';
  (EMBED.actions.length?EMBED.actions:['No corrective actions needed — hold the current routine.'])
    .forEach(a=>{ const li=document.createElement('li'); li.textContent=a; ao.appendChild(li); });

  // baseline for RHR chart uses the selected range
  const baseRhr=median(rows.map(r=>num(r.resting_hr)).filter(v=>v!=null));

  const fullDates=rows.map(r=>String(r.date));
  const avgOf=a=>{ const v=a.filter(x=>x!=null&&!isNaN(x)); return v.length?mean(v):null; };
  const avgLine=(vals,dec=1)=>{ const m=avgOf(vals); return m==null?[]:
    [{y:m,color:'#e9f0f7',dash:'2 3',label:'avg '+(Math.round(m*Math.pow(10,dec))/Math.pow(10,dec))}]; };
  const sleepH=rows.map(r=>num(r.sleep_hours));
  const scores=rows.map(r=>num(r.sleep_score));
  svgChart('cSleep',{ labels:dates, fullLabels:fullDates, series:[
      {type:'bar',label:'sleep h',data:sleepH,
        colors:scores.map(s=>s==null?C.dim:s>=80?C.green:s>=60?C.amber:C.red)},
      {type:'line',label:'7d avg',data:rolling(sleepH,7),color:C.oxy,width:2}],
    hlines:[{y:7,color:C.mist,label:'7h target'},...avgLine(sleepH)], yMin:0,yMax:10,height:250 });

  const hasStages=rows.some(r=>num(r.deep_sleep_min)!=null);
  document.getElementById('stagesCard').hidden=!hasStages;
  if(hasStages){ legend('stagesLegend',[['Deep',C.blue],['Light',C.oxy],['REM',C.violet],['Awake','#e3ae5d88']],'stages');
    svgChart('cStages',{ labels:dates, fullLabels:fullDates, chartKey:'stages', stacked:true, series:[
      {type:'bar',label:'Deep',data:rows.map(r=>num(r.deep_sleep_min)),color:C.blue},
      {type:'bar',label:'Light',data:rows.map(r=>num(r.light_sleep_min)),color:C.oxy},
      {type:'bar',label:'REM',data:rows.map(r=>num(r.rem_sleep_min)),color:C.violet},
      {type:'bar',label:'Awake',data:rows.map(r=>num(r.awake_min)),color:'#e3ae5d88'}],
      hlines:avgLine(rows.map(r=>{ const t=(num(r.deep_sleep_min)||0)+(num(r.light_sleep_min)||0)
        +(num(r.rem_sleep_min)||0)+(num(r.awake_min)||0); return t||null; }),0), yMin:0 }); }

  svgChart('cHrv',{ labels:dates, fullLabels:fullDates,
    area:{ low:rows.map(r=>num(r.hrv_baseline_balanced_low)),
      high:rows.map(r=>num(r.hrv_baseline_balanced_high)), color:'#74c68715' },
    series:[
      {type:'line',label:'HRV',data:rows.map(r=>num(r.hrv_last_night_avg)),color:C.oxy,points:true},
      {type:'line',label:'7d avg',data:rows.map(r=>num(r.hrv_weekly_avg)),color:C.mist,dash:'4 4',width:1.5}],
    hlines:avgLine(rows.map(r=>num(r.hrv_last_night_avg)),0) });

  svgChart('cRhr',{ labels:dates, fullLabels:fullDates,
    series:[{type:'line',label:'RHR',data:rows.map(r=>num(r.resting_hr)),color:C.amber,points:true}],
    hlines: (baseRhr==null?[]:[{y:baseRhr,color:C.dim,dash:'4 4',label:'baseline'},
      {y:baseRhr+th.rhr,color:C.red,dash:'2 4'}]).concat(avgLine(rows.map(r=>num(r.resting_hr)),0)) });

  svgChart('cSpo2',{ labels:dates, fullLabels:fullDates, series:[
      {type:'line',label:'overnight avg',data:rows.map(r=>num(r.sleep_avg_spo2)),color:C.oxy,points:true},
      {type:'line',label:'daily low',data:rows.map(r=>num(r.lowest_spo2)),color:C.dim,dash:'3 3',width:1.5}],
    hlines:[{y:th.spo2,color:C.amber,dash:'2 4'},{y:th.spo2Red,color:C.red,dash:'2 4'},
      ...avgLine(rows.map(r=>num(r.sleep_avg_spo2)))],
    yMin:70,yMax:100 });

  const ready=rows.map(r=>num(r.readiness_score));
  svgChart('cReady',{ labels:dates, fullLabels:fullDates, series:[{type:'bar',label:'readiness',data:ready,
    colors:ready.map(v=>v==null?C.dim:v>=60?C.green:v>=30?C.amber:C.red)}],
    hlines:avgLine(ready,0), yMin:0,yMax:100 });

  const steps=rows.map(r=>num(r.steps));
  svgChart('cSteps',{ labels:dates, fullLabels:fullDates, series:[
      {type:'bar',label:'steps',data:steps,color:'#3f6ea8'},
      {type:'line',label:'7d avg',data:rolling(steps,7),color:C.oxy,width:2}],
    hlines:avgLine(steps,0), yMin:0 });

  // active hours: intensity minutes (stacked, in hours) + logged session hours
  const modH=rows.map(r=>{ const v=num(r.intensity_min_moderate); return v==null?null:v/60; });
  const vigH=rows.map(r=>{ const v=num(r.intensity_min_vigorous); return v==null?null:v/60; });
  const sessByDate={};
  (EMBED.acts||[]).forEach(a=>{ const d=String(a.date||''); const m=num(a.duration_min);
    if(d&&m!=null) sessByDate[d]=(sessByDate[d]||0)+m; });
  const sessH=rows.map(r=>{ const m=sessByDate[String(r.date)]; return m==null?null:m/60; });
  const totalActiveH=rows.map((_,i)=>{ const t=(modH[i]||0)+(vigH[i]||0); return t||null; });
  legend('activeLegend',[['Moderate','#3f6ea8'],['Vigorous',C.amber],['Sessions (line)',C.oxy]],'active');
  svgChart('cActive',{ labels:dates, fullLabels:fullDates, chartKey:'active', stacked:true, series:[
      {type:'bar',label:'Moderate',data:modH,color:'#3f6ea8'},
      {type:'bar',label:'Vigorous',data:vigH,color:C.amber},
      {type:'line',label:'Sessions',data:sessH,color:C.oxy,width:2,points:true}],
    hlines:avgLine(totalActiveH), yMin:0 });

  const weights=rows.map(r=>num(r.weight_kg));
  const hasW=weights.some(v=>v!=null);
  document.getElementById('weightCard').hidden=!hasW;
  if(hasW) svgChart('cWeight',{ labels:dates, fullLabels:fullDates, series:[
    {type:'line',label:'kg',data:weights,color:C.violet,points:true}],
    hlines:avgLine(weights) });

  const actsAll=(EMBED.acts||[]).filter(a=>a.date&&num(a.training_load)!=null&&num(a.training_load)>0
    &&inRange(String(a.date)));
  document.getElementById('loadCard').hidden=!actsAll.length;
  if(actsAll.length){
    const wk=d=>{ const dt=new Date(String(d)+'T00:00:00');
      const m=new Date(dt); m.setDate(dt.getDate()-((dt.getDay()+6)%7));
      return m.toISOString().slice(0,10); };
    const totals={}; actsAll.forEach(a=>{ totals[a.type]=(totals[a.type]||0)+num(a.training_load); });
    const types=Object.keys(totals).sort((a,b)=>totals[b]-totals[a]);
    const weeks=[...new Set(actsAll.map(a=>wk(a.date)))].sort();
    const pal=[C.oxy,C.green,C.amber,C.violet,C.red,C.blue,'#c98fd6','#8ad0b0'];
    legend('loadLegend',types.map((t2,i)=>[t2,pal[i%pal.length]]),'load');
    svgChart('cLoad',{ labels:weeks.map(w=>'wk '+w.slice(5)), fullLabels:weeks, chartKey:'load', stacked:true, height:250,
      series:types.map((t2,i)=>({ type:'bar',label:t2,color:pal[i%pal.length],
        data:weeks.map(w=>{ const s=actsAll.filter(a=>a.type===t2&&wk(a.date)===w)
          .reduce((sum,a)=>sum+num(a.training_load),0); return s||null; }) })),
      hlines:avgLine(weeks.map(w=>actsAll.filter(a=>wk(a.date)===w)
        .reduce((s,a)=>s+num(a.training_load),0)||null),0), yMin:0 });
  }

  const vo2All=(EMBED.vo2||[]).filter(r=>r.date&&inRange(String(r.date)))
    .sort((a,b)=>String(a.date).localeCompare(String(b.date)));
  document.getElementById('vo2Card').hidden=!vo2All.length;
  if(vo2All.length){
    svgChart('cVo2',{ labels:vo2All.map(r=>String(r.date).slice(5)), fullLabels:vo2All.map(r=>String(r.date)), series:[
      {type:'line',label:'VO2max',data:vo2All.map(r=>num(r.vo2max)),color:C.green,points:true},
      {type:'line',label:'cycling',data:vo2All.map(r=>num(r.vo2max_cycling)),color:C.oxy,dash:'4 4',width:1.5}],
      hlines:avgLine(vo2All.map(r=>num(r.vo2max))) });
  }

  // plan
  const pl=document.getElementById('plan'); pl.innerHTML='';
  EMBED.plan.forEach(p=>{ const row=document.createElement('div');
    row.className='plan-row';
    row.innerHTML =
      '<div class="p-date">'+p.date.slice(5)+'<small>'+p.dow+'</small></div>'+
      '<div class="p-body"><div class="s"></div><div class="dt"></div>'+(p.constants?'<div class="cn"></div>':'')+'</div>'+
      '<div class="p-sleep">sleep ≥ '+p.sleep_target+'h</div>';
    row.querySelector('.s').textContent=p.session;
    row.querySelector('.dt').textContent=p.detail;
    if(p.constants) row.querySelector('.cn').textContent=p.constants;
    pl.appendChild(row); });

  // ---- per-chart insights (computed over the selected range) ----
  (function(){
    const lvl=(good,warn,v)=>v==null?'':(good(v)?'good':warn(v)?'warn':'bad');

    const sAvg=avgOf(sleepH), sTrend=trendOf(sleepH);
    const short=sleepH.filter(v=>v!=null&&v<th.sleep).length;
    setChartInsight('cSleep', lvl(v=>v>=7,v=>v>=6,sAvg), sAvg==null?null:
      'Avg '+fmt1(sAvg)+'h vs 7h target \u00b7 '+short+' night(s) under '+th.sleep+'h \u00b7 trend '+arrow(sTrend)+'h');

    if(hasStages){
      const tot=rows.map(r=>{const t=(num(r.deep_sleep_min)||0)+(num(r.light_sleep_min)||0)
        +(num(r.rem_sleep_min)||0); return t||null;});
      const dPct=avgOf(rows.map((r,i)=>{const d=num(r.deep_sleep_min),t=tot[i];
        return d!=null&&t?d/t*100:null;}));
      const rPct=avgOf(rows.map((r,i)=>{const d=num(r.rem_sleep_min),t=tot[i];
        return d!=null&&t?d/t*100:null;}));
      setChartInsight('cStages', dPct==null?'':(dPct>=13?'good':'warn'), dPct==null?null:
        'Deep '+fmt1(dPct)+'% \u00b7 REM '+fmt1(rPct)+'% of sleep (typical ~13-23% deep, ~20-25% REM)');
    }

    const hAvg=avgOf(rows.map(r=>num(r.hrv_last_night_avg))), hTrend=trendOf(rows.map(r=>num(r.hrv_last_night_avg)));
    const bl=num(last.hrv_baseline_balanced_low);
    setChartInsight('cHrv', hAvg==null?'':(bl==null||hAvg>=bl?'good':'bad'), hAvg==null?null:
      'Avg '+fmt1(hAvg)+' ms'+(bl!=null?' vs balanced floor '+bl:'')+' \u00b7 trend '+arrow(hTrend)+' ms');

    const rAvg=avgOf(rows.map(r=>num(r.resting_hr))), rTrend=trendOf(rows.map(r=>num(r.resting_hr)));
    const rDelta=(rAvg!=null&&baseRhr!=null)?rAvg-baseRhr:null;
    setChartInsight('cRhr', rDelta==null?'':(rDelta<=2?'good':rDelta<=th.rhr?'warn':'bad'), rAvg==null?null:
      'Avg '+fmt1(rAvg)+' bpm ('+(rDelta>=0?'+':'')+fmt1(rDelta)+' vs baseline) \u00b7 trend '+arrow(rTrend)+' bpm');

    const oAvg=avgOf(rows.map(r=>num(r.sleep_avg_spo2)));
    const lowN=rows.filter(r=>{const v=num(r.sleep_avg_spo2);return v!=null&&v<th.spo2;}).length;
    setChartInsight('cSpo2', oAvg==null?'':(oAvg>=95?'good':oAvg>=th.spo2?'warn':'bad'), oAvg==null?null:
      'Avg overnight '+fmt1(oAvg)+'% \u00b7 '+lowN+' night(s) below '+th.spo2+'%');

    const rdAvg=avgOf(ready), lowR=ready.filter(v=>v!=null&&v<30).length;
    setChartInsight('cReady', rdAvg==null?'':(rdAvg>=55?'good':rdAvg>=30?'warn':'bad'), rdAvg==null?null:
      'Avg '+fmt1(rdAvg)+'/100 \u00b7 '+lowR+' day(s) below 30 \u00b7 trend '+arrow(trendOf(ready),0));

    const stAvg=avgOf(steps), best=Math.max(...steps.filter(v=>v!=null),0);
    setChartInsight('cSteps', stAvg==null?'':(stAvg>=8000?'good':stAvg>=5000?'warn':'bad'), stAvg==null?null:
      'Avg '+Math.round(stAvg).toLocaleString()+'/day \u00b7 best '+Math.round(best).toLocaleString()+' \u00b7 trend '+arrow(trendOf(steps),0));

    const aAvg=avgOf(totalActiveH);
    const vShare=avgOf(rows.map((r,i)=>{const m=modH[i],v=vigH[i];
      const t=(m||0)+(v||0); return t?(v||0)/t*100:null;}));
    setChartInsight('cActive', aAvg==null?'':(aAvg>=1?'good':aAvg>=0.5?'warn':'bad'), aAvg==null?null:
      'Avg '+fmt1(aAvg)+' h/day intensity time \u00b7 vigorous share '+fmt1(vShare)+'%');

    if(hasW){ const wv=weights.filter(v=>v!=null);
      setChartInsight('cWeight','good', wv.length<2?null:
        'From '+fmt1(wv[0])+' to '+fmt1(wv[wv.length-1])+' kg ('+arrow(wv[wv.length-1]-wv[0])+' kg over range)'); }

    if(actsAll.length){
      const wk2=d=>{const dt=new Date(String(d)+'T00:00:00');const m=new Date(dt);
        m.setDate(dt.getDate()-((dt.getDay()+6)%7));return m.toISOString().slice(0,10);};
      const totals2={}; actsAll.forEach(a=>{const w=wk2(a.date);
        totals2[w]=(totals2[w]||0)+num(a.training_load);});
      const ws=Object.keys(totals2).sort();
      const lastW=totals2[ws[ws.length-1]], prevW=ws.length>1?totals2[ws[ws.length-2]]:null;
      setChartInsight('cLoad', prevW==null?'':(lastW/prevW>1.4?'warn':'good'),
        'Latest week '+fmt1(lastW)+(prevW!=null?' vs prior '+fmt1(prevW)+' ('+arrow((lastW/prevW-1)*100,0)+'%)':''));
    }

    if(vo2All.length>=2){
      const vv=vo2All.map(r=>num(r.vo2max)).filter(v=>v!=null);
      if(vv.length>=2) setChartInsight('cVo2', vv[vv.length-1]>=vv[0]?'good':'warn',
        'From '+fmt1(vv[0])+' to '+fmt1(vv[vv.length-1])+' ('+arrow(vv[vv.length-1]-vv[0])+' over range)');
    }
  })();

  // footer stats over the selected range
  const s=rows.map(r=>num(r.sleep_hours)).filter(v=>v!=null);
  const lc=rows.map(r=>evalDay(r,baseRhr,th).color)
    .reduce((m,c)=>{m[c]=(m[c]||0)+1;return m;},{});
  document.getElementById('foot').textContent =
    'Selected range: avg sleep '+fmt1(mean(s))+'h · lamp record '+(lc.green||0)+' green / '+
    (lc.amber||0)+' amber / '+(lc.red||0)+' red'+
    (baseRhr!=null?' · RHR baseline (range) '+fmt1(baseRhr)+' bpm':'');
}

setRangeDays(30);
</script>
</body>
</html>
"""


def render_dashboard(out_path: Path, daily, acts, vo2, insights, actions, plan, ai=None):
    embed = {
        "daily": daily, "acts": acts, "vo2": vo2,
        "insights": insights, "actions": actions, "plan": plan, "ai": ai,
        "built": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    html = (HTML_TEMPLATE
            .replace("__EMBED_JSON__", json.dumps(embed, ensure_ascii=False, default=str))
            .replace("__TH_SLEEP__", str(SLEEP_FLOOR_H))
            .replace("__TH_RHR__", str(RHR_AMBER_OVER))
            .replace("__TH_SPO2__", str(SPO2_AMBER))
            .replace("__TH_SPO2_RED__", str(SPO2_RED)))
    out_path.write_text(html, encoding="utf-8")


# ============================================================================
# MAIN
# ============================================================================
def main() -> None:
    p = argparse.ArgumentParser(description="Health Console: collect + store + analyze + dashboard")
    p.add_argument("--days", type=int, default=30, help="Fetch window (default 30)")
    p.add_argument("--start", help="YYYY-MM-DD (overrides --days)")
    p.add_argument("--end", help="YYYY-MM-DD (default today)")
    p.add_argument("--out", default="./health_data")
    p.add_argument("--plan-days", type=int, default=PLAN_DAYS_DEFAULT)
    p.add_argument("--skip-fetch", action="store_true",
                   help="Skip Garmin fetch; rebuild dashboard from stored history")
    p.add_argument("--tokenstore", default=None)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--ntfy", help="ntfy topic URL for push warnings after each run")
    p.add_argument("--notify-mode", choices=["warnings", "always"], default="warnings")
    p.add_argument("--notify-dry", action="store_true", help="print notification instead of sending")
    p.add_argument("--ai-provider", choices=["anthropic", "openai", "gemini", "custom"],
                   help="AI narrative provider; keys are remembered per provider after first use")
    p.add_argument("--ai", action="store_true",
                   help="run AI analysis with the saved active provider")
    p.add_argument("--ai-key", help="API key - saved for the provider; omit to reuse the stored one")
    p.add_argument("--ai-model", help="model override; for custom: \"BASE_URL|MODEL\" (also remembered)")
    p.add_argument("--ai-forget", metavar="PROVIDER",
                   help="remove the saved key for a provider and exit")
    args = p.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.ai_forget:
        print(forget_ai_key(out_dir, args.ai_forget))
        return
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(exist_ok=True)

    # one-time migration from old per-run CSVs
    seed_masters_from_legacy(out_dir)
    # self-heal any days missing sleep using raw JSON already on disk
    backfill_sleep_from_raw(out_dir)

    if not args.skip_fetch:
        tokenstore = args.tokenstore or os.getenv("GARMINTOKENS") or "~/.garminconnect"
        end = datetime.date.fromisoformat(args.end) if args.end else datetime.date.today()
        start = (datetime.date.fromisoformat(args.start) if args.start
                 else end - datetime.timedelta(days=args.days - 1))
        api = init_api(tokenstore)
        report = {"range": f"{start} to {end}"}

        n = (end - start).days + 1
        print(f"\nCollecting {n} days: {start} -> {end}\n")

        print("[1/3] VO2max history...")
        vo2_new = collect_vo2max_history(api, start.isoformat(), end.isoformat(), raw_dir, report)

        print("[2/3] Activities...")
        acts_new = collect_activities(api, start.isoformat(), end.isoformat(), raw_dir, report)
        print(f"      {len(acts_new)} activities")

        print("[3/3] Daily metrics...")
        daily_new = []
        day = start
        while day <= end:
            print(f"  {day.isoformat()}")
            daily_new.append(collect_day(api, day.isoformat(), raw_dir, report))
            time.sleep(args.delay)
            day += datetime.timedelta(days=1)

        (out_dir / "collection_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

        print("\nMerging into history:")
        daily = upsert_master(out_dir / "daily_master.csv", daily_new, "date", "date")
        acts = upsert_master(out_dir / "activities_master.csv", acts_new, "activity_id", "date")
        vo2 = upsert_master(out_dir / "vo2max_master.csv", vo2_new, "date", "date")
    else:
        print("Skipping fetch - reading stored history from", out_dir)
        daily = read_csv(out_dir / "daily_master.csv")
        acts = read_csv(out_dir / "activities_master.csv")
        vo2 = read_csv(out_dir / "vo2max_master.csv")
        if not daily:
            sys.exit("No daily_master.csv found - run without --skip-fetch first.")

    # daily lamp evaluation + optional push warning
    ev = evaluate_latest(daily)
    print(f"\nLamp {ev['color'].upper()} for {ev['date']}"
          + (f" - {'; '.join(ev['flags'])}" if ev["flags"] else " - all clear")
          + (f" | watch: {'; '.join(ev['watch'])}" if ev["watch"] else ""))
    if args.ntfy or args.notify_dry:
        notify_evaluation(ev, args.ntfy or "", args.notify_mode, args.notify_dry or not args.ntfy)

    print("\nAnalyzing...")
    insights, actions = analyze(daily, acts, vo2)
    for i in insights:
        print(f"  [{i['level'].upper():4}] {i['title']}")

    plan = build_plan(daily, args.plan_days)
    print(f"\nPlan: {len(plan)} days ({plan[0]['date']} -> {plan[-1]['date']})")

    ai = None
    if args.ai_provider or args.ai:
        try:
            provider, key, model = resolve_ai(out_dir, args.ai_provider or "",
                                              args.ai_key or "", args.ai_model or "")
            print(f"Requesting AI analysis from {provider}...")
            stats = build_ai_stats(daily, acts, vo2)
            text = ai_narrative(provider, key, model, stats)
            ai = {"text": text, "provider": provider,
                  "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
            print("AI analysis received.")
        except ValueError as err:
            print(f"AI analysis skipped: {err}")
        except Exception as err:  # noqa: BLE001
            print(f"AI analysis failed (dashboard still built): {type(err).__name__}: {err}")

    dash = out_dir / "dashboard.html"
    render_dashboard(dash, daily, acts, vo2, insights, actions, plan, ai)
    print(f"\nDashboard written: {dash.resolve()}")
    print("Open it in any browser - fully offline, all history embedded.")


if __name__ == "__main__":
    main()
