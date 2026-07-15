#!/usr/bin/env python3
"""
Sub-hour predictive analysis (runs every 2 hours except the main hour).

Loads raw snapshots from the last 48 hours and computes:
  - Velocity metrics (per hour, 2h, 24h)
  - Acceleration (change in velocity between the last two intervals)
  - Predictions for the next main hour (08:00 Beijing)
  - Top movers (biggest download increases in the last 2h interval)
  - Velocity by content category
  - Anomaly detection (velocity spikes vs historical average)
  - Hourly pattern (downloads gained per 2h window over the last 24h)

Outputs:
  - data/{project_type}/analysis/{timestamp}.json  (analysis_type = "sub")
  - data/{project_type}/latest_sub_analysis.json
"""
import argparse
import sys
from datetime import datetime, timedelta

from utils import (
    load_json, save_json, ensure_dir, get_timestamp,
    get_current_date, get_project_type_dir, get_raw_dir, get_analysis_dir,
    BEIJING_TZ, list_snapshot_files,
)

CONTENT_CATEGORY_HEADER = "categories"
SNAPSHOT_WINDOW_HOURS = 48
MAIN_HOUR = 8  # 08:00 Beijing time = 00:00 UTC
ANOMALY_THRESHOLD = 3.0  # flag projects with velocity > 3x historical average


# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════


def parse_snapshot_time(timestamp_str):
    """Parse a snapshot timestamp 'YYYY-MM-DDTHH-MM-SS' as Beijing time."""
    try:
        dt = datetime.strptime(timestamp_str, "%Y-%m-%dT%H-%M-%S")
        return dt.replace(tzinfo=BEIJING_TZ)
    except (ValueError, TypeError):
        return None


def load_recent_snapshots(project_type, window_hours=SNAPSHOT_WINDOW_HOURS):
    """Load raw snapshots from the last `window_hours`, sorted chronologically.
    Handles both .json and .json.gz files.
    """
    raw_dir = get_raw_dir(project_type)
    snapshot_files = list_snapshot_files(raw_dir)
    cutoff = datetime.now(BEIJING_TZ) - timedelta(hours=window_hours)
    snapshots = []
    for f in snapshot_files:
        data = load_json(f)
        if not data:
            continue
        snap_time = parse_snapshot_time(data.get("timestamp", ""))
        if snap_time is None:
            continue
        if snap_time >= cutoff:
            snapshots.append(data)
    return snapshots


def load_content_categories(project_type):
    """Load content category names (header == 'categories', excluding loaders).

    Same logic as analyze.py: a category is a 'content category' if its header
    is 'categories' and its name is not a loader name.
    """
    type_dir = get_project_type_dir(project_type)
    loaders = load_json(f"{type_dir}/loaders.json") or []
    loader_set = set(loaders)
    categories = load_json(f"{type_dir}/categories.json") or []
    content_cats = set()
    for cat in categories:
        if cat.get("header", "") == CONTENT_CATEGORY_HEADER:
            name = cat.get("slug") or cat.get("name", "")
            if name and name not in loader_set:
                content_cats.add(name)
    return content_cats


def hours_until_next_main_hour(now=None):
    """Hours until the next 08:00 Beijing time (the next main hour)."""
    if now is None:
        now = datetime.now(BEIJING_TZ)
    next_main = now.replace(hour=MAIN_HOUR, minute=0, second=0, microsecond=0)
    if now >= next_main:
        next_main += timedelta(days=1)
    return max(0, int((next_main - now).total_seconds() // 3600))


def interval_hours(t1, t2):
    """Hours between two datetimes (always non-negative)."""
    return abs((t2 - t1).total_seconds()) / 3600.0


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Sub-hour predictive analysis")
    parser.add_argument(
        "--project-type", required=True,
        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "world"],
        help="Project type to analyze",
    )
    args = parser.parse_args()
    project_type = args.project_type

    print(f"=== Predictive Analyze ({project_type}) — Sub-hour ===")

    snapshots = load_recent_snapshots(project_type)
    if not snapshots:
        print(f"Error: no raw snapshots in the last {SNAPSHOT_WINDOW_HOURS}h "
              f"at {get_raw_dir(project_type)}/")
        return 1

    snap_count = len(snapshots)
    snap_times = [parse_snapshot_time(s.get("timestamp", "")) for s in snapshots]
    current_snap = snapshots[-1]
    current_time = snap_times[-1]
    current_total = current_snap.get("total_downloads", 0)
    current_date = current_snap.get("date", get_current_date())
    current_projects = current_snap.get("projects", [])
    baseline_total = snapshots[0].get("total_downloads", 0)

    print(f"  Snapshots (last {SNAPSHOT_WINDOW_HOURS}h): {snap_count}")
    print(f"  Current total downloads: {current_total:,}")
    print(f"  Baseline (window start) downloads: {baseline_total:,}")

    # ── Velocity metrics ─────────────────────────────────────────
    velocity_per_hour = 0.0
    velocity_2h = 0
    if snap_count >= 2:
        prev_total = snapshots[-2].get("total_downloads", 0)
        velocity_2h = current_total - prev_total
        h = interval_hours(snap_times[-2], current_time)
        velocity_per_hour = velocity_2h / h if h > 0 else 0.0

    velocity_24h = 0
    if snap_count >= 2:
        target_time = current_time - timedelta(hours=24)
        # Find the snapshot closest to (and not after) 24h ago; fall back to earliest
        prev_24_total = snapshots[0].get("total_downloads", 0)
        for i, s in enumerate(snapshots):
            if snap_times[i] <= target_time:
                prev_24_total = s.get("total_downloads", 0)
            else:
                break
        velocity_24h = current_total - prev_24_total
    avg_velocity_24h = velocity_24h / 24.0 if velocity_24h else 0.0

    velocity = {
        "velocity_per_hour": round(velocity_per_hour, 2),
        "velocity_2h": velocity_2h,
        "velocity_24h": velocity_24h,
        "avg_velocity_24h": round(avg_velocity_24h, 2),
    }
    print(f"  Velocity: {velocity_per_hour:,.2f}/h, 2h={velocity_2h:,}, 24h={velocity_24h:,}")

    # ── Acceleration ─────────────────────────────────────────────
    acceleration = 0.0
    acceleration_pct = 0.0
    if snap_count >= 3:
        t0, t1, t2 = snap_times[-3], snap_times[-2], snap_times[-1]
        s0, s1, s2 = snapshots[-3], snapshots[-2], snapshots[-1]
        h_prev = interval_hours(t0, t1)
        h_curr = interval_hours(t1, t2)
        v_prev = ((s1.get("total_downloads", 0) - s0.get("total_downloads", 0)) / h_prev) if h_prev > 0 else 0.0
        v_curr = ((s2.get("total_downloads", 0) - s1.get("total_downloads", 0)) / h_curr) if h_curr > 0 else 0.0
        acceleration = v_curr - v_prev
        acceleration_pct = (acceleration / v_prev * 100) if v_prev != 0 else 0.0
    acceleration_obj = {
        "acceleration": round(acceleration, 2),
        "acceleration_pct": round(acceleration_pct, 2),
    }
    print(f"  Acceleration: {acceleration:,.2f} ({acceleration_pct:.2f}%)")

    # ── Predictions ──────────────────────────────────────────────
    hours_main = hours_until_next_main_hour(current_time)
    predicted_daily_total = current_total + (avg_velocity_24h * hours_main)
    predicted_daily_growth = predicted_daily_total - baseline_total
    predicted_daily_growth_pct = (predicted_daily_growth / baseline_total * 100) if baseline_total > 0 else 0.0
    if snap_count >= 6:
        confidence = "high"
    elif snap_count >= 3:
        confidence = "medium"
    else:
        confidence = "low"
    predictions = {
        "predicted_daily_total": int(predicted_daily_total),
        "predicted_daily_growth": int(predicted_daily_growth),
        "predicted_daily_growth_pct": round(predicted_daily_growth_pct, 2),
        "confidence": confidence,
    }
    print(f"  Predicted daily total: {predicted_daily_total:,.0f} (confidence: {confidence})")

    # ── Top movers (last 2h) ─────────────────────────────────────
    top_movers_2h = []
    if snap_count >= 2:
        prev_map = {p["project_id"]: p.get("downloads", 0)
                    for p in snapshots[-2].get("projects", [])}
        h_last = interval_hours(snap_times[-2], current_time) or 2.0
        movers = []
        for p in current_projects:
            pid = p["project_id"]
            cur_dl = p.get("downloads", 0)
            prev_dl = prev_map.get(pid, 0)
            delta = cur_dl - prev_dl
            if delta > 0:
                movers.append({
                    "project_id": pid,
                    "title": p.get("title", ""),
                    "slug": p.get("slug", ""),
                    "previous_downloads": prev_dl,
                    "current_downloads": cur_dl,
                    "delta_2h": delta,
                    "velocity": round(delta / h_last, 2),
                })
        movers.sort(key=lambda x: x["delta_2h"], reverse=True)
        top_movers_2h = movers[:20]
    print(f"  Top movers (2h): {len(top_movers_2h)}")

    # ── Velocity by content category ─────────────────────────────
    velocity_by_category = []
    content_cats = load_content_categories(project_type)
    if content_cats and current_projects and snap_count >= 2:
        prev_map = {p["project_id"]: p.get("downloads", 0)
                    for p in snapshots[-2].get("projects", [])}
        h_last = interval_hours(snap_times[-2], current_time) or 2.0
        cat_velocity = {}  # cat -> {"total_velocity": float, "projects": int}
        for p in current_projects:
            cats = set(p.get("categories", [])) & content_cats
            if not cats:
                continue
            pid = p["project_id"]
            cur_dl = p.get("downloads", 0)
            prev_dl = prev_map.get(pid, 0)
            pv = (cur_dl - prev_dl) / h_last if h_last > 0 else 0.0
            for cat in cats:
                stat = cat_velocity.get(cat)
                if stat is None:
                    stat = {"total_velocity": 0.0, "projects": 0}
                    cat_velocity[cat] = stat
                stat["total_velocity"] += pv
                stat["projects"] += 1
        for cat, stat in cat_velocity.items():
            proj = stat["projects"]
            total_v = stat["total_velocity"]
            velocity_by_category.append({
                "category": cat,
                "total_velocity": round(total_v, 2),
                "projects_tracked": proj,
                "avg_velocity": round(total_v / proj, 2) if proj > 0 else 0.0,
            })
        velocity_by_category.sort(key=lambda x: x["total_velocity"], reverse=True)
    print(f"  Velocity by category: {len(velocity_by_category)} categories")

    # ── Anomaly detection ────────────────────────────────────────
    anomalies = []
    if snap_count >= 4:
        # Build per-project download series across all window snapshots
        project_series = {}  # pid -> list of (snap_index, downloads)
        for i, s in enumerate(snapshots):
            for p in s.get("projects", []):
                pid = p["project_id"]
                project_series.setdefault(pid, []).append((i, p.get("downloads", 0)))
        current_project_map = {p["project_id"]: p for p in current_projects}
        t1, t2 = snap_times[-2], snap_times[-1]
        h_curr = interval_hours(t1, t2) or 2.0
        for pid, series in project_series.items():
            # Must be present in the last two snapshots (current interval)
            if len(series) < 2 or series[-1][0] != snap_count - 1 or series[-2][0] != snap_count - 2:
                continue
            cur_dl = series[-1][1]
            prev_dl = series[-2][1]
            current_v = (cur_dl - prev_dl) / h_curr if h_curr > 0 else 0.0
            if current_v <= 0:
                continue
            # Historical velocities from earlier consecutive intervals
            historical = []
            for k in range(1, len(series) - 1):
                i_prev, dl_prev = series[k - 1]
                i_curr, dl_curr = series[k]
                if i_curr - i_prev != 1:
                    continue
                hh = interval_hours(snap_times[i_prev], snap_times[i_curr])
                if hh > 0:
                    historical.append((dl_curr - dl_prev) / hh)
            if len(historical) < 2:
                continue
            avg_v = sum(historical) / len(historical)
            if avg_v > 0 and current_v > ANOMALY_THRESHOLD * avg_v:
                factor = current_v / avg_v
                pinfo = current_project_map.get(pid)
                if pinfo:
                    anomalies.append({
                        "project_id": pid,
                        "title": pinfo.get("title", ""),
                        "slug": pinfo.get("slug", ""),
                        "current_velocity": round(current_v, 2),
                        "avg_velocity": round(avg_v, 2),
                        "anomaly_factor": round(factor, 2),
                    })
        anomalies.sort(key=lambda x: x["anomaly_factor"], reverse=True)
        anomalies = anomalies[:50]
    print(f"  Anomalies: {len(anomalies)}")

    # ── Hourly pattern (downloads gained per 2h window, last 24h) ─
    hourly_pattern = []
    if snap_count >= 2:
        target_time = current_time - timedelta(hours=24)
        for k in range(1, len(snap_times)):
            t_prev = snap_times[k - 1]
            t_curr = snap_times[k]
            if t_curr < target_time:
                continue
            delta = snapshots[k].get("total_downloads", 0) - snapshots[k - 1].get("total_downloads", 0)
            hourly_pattern.append({
                "hour_label": f"{t_prev.hour:02d}-{t_curr.hour:02d}",
                "downloads_gained": delta,
            })
    print(f"  Hourly pattern: {len(hourly_pattern)} windows")

    # ── Assemble output ──────────────────────────────────────────
    timestamp = get_timestamp()
    analysis = {
        "timestamp": timestamp,
        "date": current_date,
        "project_type": project_type,
        "analysis_type": "sub",
        "snapshots_analyzed": snap_count,
        "last_snapshot_time": current_snap.get("timestamp", ""),
        "hours_until_main": hours_main,
        "velocity": velocity,
        "acceleration": acceleration_obj,
        "predictions": predictions,
        "top_movers_2h": top_movers_2h,
        "velocity_by_category": velocity_by_category,
        "anomalies": anomalies,
        "hourly_pattern": hourly_pattern,
    }

    analysis_dir = get_analysis_dir(project_type)
    ensure_dir(analysis_dir)
    analysis_path = f"{analysis_dir}/{timestamp}.json"
    save_json(analysis_path, analysis)
    print(f"Saved sub analysis to {analysis_path}")

    type_dir = get_project_type_dir(project_type)
    latest_path = f"{type_dir}/latest_sub_analysis.json"
    save_json(latest_path, analysis)
    print(f"Saved latest sub analysis to {latest_path}")

    print(f"=== Predictive Analyze ({project_type}) complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
