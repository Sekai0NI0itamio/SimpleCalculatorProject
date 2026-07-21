#!/usr/bin/env python3
"""
Phase 5: Analyze — Simple Delta Analysis

Compares the latest raw snapshot with a baseline snapshot (2h or 24h ago)
and produces:
  - Summary stats (projects, versions, downloads, new downloads)
  - all_project_deltas: ALL projects with delta > 0, sorted by increase
  - top_version_loaders: VL pairs ranked by total delta across all projects
  - project_vl_pairs: per-project VL pairs (saved as separate file)
  - category_rankings: categories ranked by download increase
  - loader_rankings: loaders ranked by download increase

Outputs:
  - data/{project_type}/analysis/{timestamp}.json  — analysis
  - data/{project_type}/latest_analysis.json       — same (for the app)
  - data/{project_type}/project_vl_pairs.json       — per-project VL pairs
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone

from utils import (
    load_json, save_json, ensure_dir, get_timestamp,
    get_project_type_dir, get_raw_dir, get_analysis_dir,
    list_snapshot_files, BEIJING_TZ,
)

CONTENT_CATEGORY_HEADER = "categories"


# ═══════════════════════════════════════════════════════════════════
#  SNAPSHOT LOADING
# ═══════════════════════════════════════════════════════════════════


def parse_snapshot_timestamp(snapshot):
    """Parse a snapshot's timestamp into a datetime object (Beijing time)."""
    ts_str = snapshot.get("timestamp", "")
    if not ts_str:
        return None
    if "T" in ts_str:
        date_part, time_part = ts_str.split("T", 1)
        time_part = time_part.replace("-", ":")
        ts_str = f"{date_part}T{time_part}"
    try:
        return datetime.fromisoformat(ts_str).replace(tzinfo=BEIJING_TZ)
    except (ValueError, TypeError):
        try:
            return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=BEIJING_TZ)
        except (ValueError, TypeError):
            return None


def parse_filename_timestamp(filename):
    """Parse a timestamp from a snapshot filename (e.g. '2026-07-15T09-48-28.json.gz').
    This avoids loading the file just to read its timestamp field."""
    import os
    basename = os.path.basename(filename)
    # Remove .json.gz or .json extension
    for ext in (".json.gz", ".json"):
        if basename.endswith(ext):
            basename = basename[:-len(ext)]
            break
    if "T" in basename:
        date_part, time_part = basename.split("T", 1)
        time_part = time_part.replace("-", ":")
        basename = f"{date_part}T{time_part}"
    try:
        return datetime.fromisoformat(basename).replace(tzinfo=BEIJING_TZ)
    except (ValueError, TypeError):
        return None


def list_snapshot_files_with_ts(project_type):
    """List all snapshot files with their parsed timestamps (from filenames).
    Returns list of (filepath, datetime) sorted by timestamp.
    Does NOT load file contents — much faster than load_all_snapshots()."""
    raw_dir = get_raw_dir(project_type)
    snapshot_files = list_snapshot_files(raw_dir)
    result = []
    for f in snapshot_files:
        ts = parse_filename_timestamp(f)
        if ts:
            result.append((f, ts))
    result.sort(key=lambda x: x[1])
    return result


def find_baseline_file(snapshot_files_ts, current_ts, hours_back):
    """Find the baseline snapshot file closest to `hours_back` before current.
    Works with filenames/timestamps only — no file loading needed.
    Returns (filepath, datetime) or None."""
    if not current_ts:
        return None
    target = current_ts - timedelta(hours=hours_back)
    window = timedelta(hours=hours_back * 0.5)
    best = None
    best_diff = None
    for filepath, ts in snapshot_files_ts:
        if ts >= current_ts:
            continue
        diff = abs((ts - target).total_seconds())
        if best_diff is None or diff < best_diff:
            if diff <= window.total_seconds():
                best = (filepath, ts)
                best_diff = diff
    return best


def find_baseline_files_window(snapshot_files_ts, current_ts, hours_back, max_files=3):
    """Enhancement #1: Rolling-window baseline.
    Find up to `max_files` baseline snapshots closest to `hours_back` before current,
    all within the tolerance window. Used to compute a median baseline that
    smooths single-snapshot anomalies (e.g., a project briefly delisted).

    Returns list of (filepath, datetime, diff_seconds) sorted by diff (closest first).
    Works with filenames/timestamps only — no file loading needed.
    """
    if not current_ts:
        return []
    target = current_ts - timedelta(hours=hours_back)
    window = timedelta(hours=hours_back * 0.5)
    candidates = []
    for filepath, ts in snapshot_files_ts:
        if ts >= current_ts:
            continue
        diff = abs((ts - target).total_seconds())
        if diff <= window.total_seconds():
            candidates.append((filepath, ts, diff))
    candidates.sort(key=lambda x: x[2])  # closest first
    return candidates[:max_files]


def build_rolling_baseline(baseline_files, primary_baseline_snapshot):
    """Enhancement #1: Build a synthetic baseline snapshot using median downloads.

    Takes a list of (filepath, datetime) baseline files plus the primary baseline
    snapshot (already loaded) and returns a synthetic baseline dict where
    `projects` and `versions` have median downloads per project/version_id.

    If only one baseline file is available, returns the primary baseline unchanged.

    `primary_baseline_snapshot` is used as the source of metadata fields
    (date, total_downloads, etc.) so downstream code keeps working.
    """
    if len(baseline_files) <= 1:
        return primary_baseline_snapshot

    # Load all baseline snapshots (skip the primary, which is already loaded)
    primary_path = baseline_files[0][0]
    all_snapshots = [primary_baseline_snapshot]
    for filepath, _ts in baseline_files[1:]:
        if filepath == primary_path:
            continue
        data = load_json(filepath)
        if data:
            all_snapshots.append(data)

    if len(all_snapshots) < 2:
        return primary_baseline_snapshot

    def _median(values):
        """Median of a list of numbers, ignoring None/missing. Returns 0 if empty."""
        clean = [v for v in values if v is not None]
        if not clean:
            return 0
        clean.sort()
        n = len(clean)
        if n % 2 == 1:
            return clean[n // 2]
        return (clean[n // 2 - 1] + clean[n // 2]) / 2

    # Build median project downloads
    project_dls_per_snap = {}  # pid -> [downloads in snap1, snap2, ...]
    for snap in all_snapshots:
        for p in snap.get("projects", []):
            pid = p.get("project_id")
            if not pid:
                continue
            project_dls_per_snap.setdefault(pid, []).append(p.get("downloads", 0))

    median_projects = []
    for pid, dls in project_dls_per_snap.items():
        median_dls = _median(dls)
        # Use the latest snapshot's metadata for the project (title, slug, categories)
        # to keep the median value but the freshest descriptor
        latest_proj = None
        for snap in reversed(all_snapshots):
            for p in snap.get("projects", []):
                if p.get("project_id") == pid:
                    latest_proj = p
                    break
            if latest_proj:
                break
        if latest_proj:
            proj_copy = dict(latest_proj)
            proj_copy["downloads"] = median_dls
            median_projects.append(proj_copy)
        else:
            median_projects.append({"project_id": pid, "downloads": median_dls})

    # Build median version downloads
    version_dls_per_snap = {}  # vid -> [downloads in snap1, snap2, ...]
    for snap in all_snapshots:
        for v in snap.get("versions", []):
            vid = v.get("version_id")
            if not vid:
                continue
            version_dls_per_snap.setdefault(vid, []).append(v.get("downloads", 0))

    median_versions = []
    for vid, dls in version_dls_per_snap.items():
        median_dls = _median(dls)
        latest_ver = None
        for snap in reversed(all_snapshots):
            for v in snap.get("versions", []):
                if v.get("version_id") == vid:
                    latest_ver = v
                    break
            if latest_ver:
                break
        if latest_ver:
            ver_copy = dict(latest_ver)
            ver_copy["downloads"] = median_dls
            median_versions.append(ver_copy)
        else:
            median_versions.append({"version_id": vid, "downloads": median_dls})

    # Build synthetic baseline using primary's metadata + median values
    synthetic = dict(primary_baseline_snapshot)
    synthetic["projects"] = median_projects
    synthetic["versions"] = median_versions
    synthetic["baseline_source_count"] = len(all_snapshots)
    return synthetic


def load_filter_sets(project_type):
    """Load loader names and content category names for filtering."""
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

    return loaders, loader_set, content_cats


# ═══════════════════════════════════════════════════════════════════
#  CORE ANALYSIS
# ═══════════════════════════════════════════════════════════════════


def compute_momentum_score(delta_downloads, growth_pct, downloads_per_hour, baseline_downloads):
    """Composite momentum score (Enhancement E).

    Combines three signals into a single score:
      - log-scaled absolute delta (handles huge range, 1 to 10M+)
      - growth rate percentage (rewards proportional growth)
      - velocity = downloads/hour (rewards high download rate)

    Weighting:
      40% log-scaled delta  — rewards big absolute gains
      30% growth_pct       — rewards proportional growth (small projects can score high)
      30% log-scaled rate  — rewards high download velocity

    All three components are log-scaled (log1p) so the score stays in a
    reasonable range and isn't dominated by one outlier project.
    """
    if baseline_downloads <= 0:
        growth_pct = 0.0
    if downloads_per_hour < 0:
        downloads_per_hour = 0.0
    if delta_downloads < 0:
        # Declining projects get negative momentum proportional to loss
        return round(-math.log1p(abs(delta_downloads)) * 0.4, 2)

    log_delta = math.log1p(max(0, delta_downloads))
    log_rate = math.log1p(max(0, downloads_per_hour))
    # Clamp growth_pct to [0, 100] for scoring (above 100% is great but shouldn't dominate)
    clamped_growth = min(max(growth_pct, 0.0), 100.0)

    score = (log_delta * 0.4) + (clamped_growth * 0.3) + (log_rate * 0.3)
    return round(score, 2)


def anomaly_momentum_penalty(anomaly_factor):
    """Enhancement #3: Anomaly-aware momentum.

    Returns a multiplier (0.4 to 1.0) to penalize momentum_score for projects
    flagged as anomalies by predictive_analyze.py. The anomaly_factor is the
    ratio of current_velocity / historical_avg_velocity.

    Reasoning: an anomaly often means the velocity spike is a data glitch or
    one-off event, not sustained growth. We don't want a glitchy project to
    rank above steady growers.

    - factor <= 3.0: no penalty (not really anomalous, just a slight bump)
    - factor 3.0-5.0: 20% penalty (0.8x)
    - factor 5.0-10.0: up to 50% penalty (0.6-0.5x)
    - factor >= 10.0: 60% penalty (0.4x, capped)
    """
    if anomaly_factor is None or anomaly_factor <= 3.0:
        return 1.0
    # Linear penalty above 3.0, capped at 0.4
    # factor=3.0 → 1.0, factor=5.0 → 0.8, factor=8.0 → 0.5, factor=10+ → 0.4
    penalty = (anomaly_factor - 3.0) * 0.1
    return max(0.4, 1.0 - penalty)


def load_anomaly_factors(project_type):
    """Enhancement #3: Load anomaly factors from the previous predictive run.

    Returns dict: {project_id: anomaly_factor} loaded from
    data/{project_type}/latest_sub_analysis.json. Returns {} if file
    is missing or has no anomalies.
    """
    type_dir = get_project_type_dir(project_type)
    sub_path = f"{type_dir}/latest_sub_analysis.json"
    sub_data = load_json(sub_path)
    if not sub_data:
        return {}
    anomalies = sub_data.get("anomalies", []) or []
    return {a.get("project_id"): a.get("anomaly_factor", 0.0) for a in anomalies if a.get("project_id")}


def build_project_analysis(current_snapshot, baseline_snapshot,
                           project_type, loader_names, loader_set, content_cat_names,
                           actual_hours_between, anomaly_factors=None):
    """Build delta analysis. Returns a dict with the essential sections.

    Enhancements:
      C: Tracks negative deltas (declining_projects)
      D: Rate normalization (downloads_per_hour on every delta)
      E: Momentum score on top/growing projects
      #3: Anomaly-aware momentum — penalizes projects flagged as anomalies
          by predictive_analyze.py (anomaly_factor > 3.0 reduces score).
    """
    if anomaly_factors is None:
        anomaly_factors = {}
    current_projects = current_snapshot.get("projects", [])
    current_total_downloads = current_snapshot.get("total_downloads", 0)
    baseline_date = baseline_snapshot.get("date", "")
    current_date = current_snapshot.get("date", "")

    baseline_map = {p["project_id"]: p.get("downloads", 0) for p in baseline_snapshot.get("projects", [])}
    current_map = {p["project_id"]: p.get("downloads", 0) for p in current_projects}

    # Avoid divide-by-zero — enforce a minimum 1h span for rate normalization
    hours = max(actual_hours_between, 1.0)

    # ── Version data ──────────────────────────────────────────────
    current_versions = current_snapshot.get("versions", [])
    baseline_versions = baseline_snapshot.get("versions", [])
    baseline_version_map = {v.get("version_id"): v.get("downloads", 0) for v in baseline_versions if v.get("version_id")}

    # ── Summary (includes net change and declining totals, Enhancement C) ──
    new_downloads = 0
    lost_downloads = 0
    growing_count = 0
    declining_count = 0
    for pid, cur_dl in current_map.items():
        delta = cur_dl - baseline_map.get(pid, 0)
        if delta > 0:
            new_downloads += delta
            growing_count += 1
        elif delta < 0:
            lost_downloads += abs(delta)
            declining_count += 1

    summary = {
        "total_projects": current_snapshot.get("project_count", len(current_projects)),
        "total_versions": len(current_versions),
        "total_downloads": current_total_downloads,
        "baseline_date": baseline_date,
        "current_date": current_date,
        "new_projects_since_baseline": sum(1 for pid in current_map if pid not in baseline_map),
        "new_downloads_since_baseline": new_downloads,
        "lost_downloads_since_baseline": lost_downloads,
        "net_download_change": new_downloads - lost_downloads,
        "growing_projects": growing_count,
        "declining_projects": declining_count,
        "downloads_per_hour": round(new_downloads / hours, 2),
    }

    # ── Category rankings ─────────────────────────────────────────
    cat_projects = {}
    for p in current_projects:
        for cat in p.get("categories", []):
            if cat in content_cat_names:
                cat_projects.setdefault(cat, []).append(p)

    category_rankings = []
    for cat, projs in cat_projects.items():
        current_total = sum(p.get("downloads", 0) for p in projs)
        baseline_total = sum(baseline_map.get(p["project_id"], 0) for p in projs)
        new_dl = current_total - baseline_total
        category_rankings.append({
            "category": cat,
            "projects": len(projs),
            "total_downloads": current_total,
            "new_downloads": new_dl,
            "downloads_per_hour": round(new_dl / hours, 2),
            "growth_pct": round((new_dl / baseline_total * 100) if baseline_total > 0 else 0.0, 2),
        })
    category_rankings.sort(key=lambda x: x["new_downloads"], reverse=True)

    # ── Category trending projects ─────────────────────────────────
    TOP_TRENDING_PER_CAT = 50
    category_trending = {}
    for cat, projs in cat_projects.items():
        trending = []
        for p in projs:
            pid = p["project_id"]
            cur_dl = p.get("downloads", 0)
            base_dl = baseline_map.get(pid, 0)
            delta = cur_dl - base_dl
            if delta > 0:
                rate = delta / hours
                growth_pct = round((delta / base_dl * 100) if base_dl > 0 else 0.0, 2)
                raw_score = compute_momentum_score(delta, growth_pct, rate, base_dl)
                af = anomaly_factors.get(pid)
                is_anom = af is not None and af > 3.0
                trending.append({
                    "project_id": pid,
                    "title": p.get("title", ""),
                    "slug": p.get("slug", ""),
                    "categories": p.get("categories", []),
                    "current_downloads": cur_dl,
                    "delta_downloads": delta,
                    "downloads_per_hour": round(rate, 2),
                    "growth_pct": growth_pct,
                    "momentum_score": round(raw_score * anomaly_momentum_penalty(af), 2),
                    "is_anomaly": is_anom,
                    "anomaly_factor": round(af, 2) if af is not None else None,
                })
        trending.sort(key=lambda x: x["delta_downloads"], reverse=True)
        category_trending[cat] = trending[:TOP_TRENDING_PER_CAT]

    # ── Loader rankings ────────────────────────────────────────────
    loader_stats = {}
    for p in current_projects:
        for loader in p.get("loaders", []):
            if loader not in loader_set:
                continue
            if loader not in loader_stats:
                loader_stats[loader] = {"projects": 0, "total_downloads": 0, "new_downloads": 0}
            loader_stats[loader]["projects"] += 1
            loader_stats[loader]["total_downloads"] += p.get("downloads", 0)
            loader_stats[loader]["new_downloads"] += p.get("downloads", 0) - baseline_map.get(p["project_id"], 0)

    loader_rankings = []
    for loader, stat in loader_stats.items():
        new_dl = stat["new_downloads"]
        loader_rankings.append({
            "loader": loader,
            "projects": stat["projects"],
            "total_downloads": stat["total_downloads"],
            "new_downloads": new_dl,
            "downloads_per_hour": round(new_dl / hours, 2),
        })
    loader_rankings.sort(key=lambda x: x["new_downloads"], reverse=True)

    # ── Top projects (top 50 by delta, with full details, Enhancement D + E) ──
    project_title_map = {p["project_id"]: p.get("title", "") for p in current_projects}
    top_projects = []
    declining_projects = []  # Enhancement C: track negative deltas
    for p in current_projects:
        pid = p["project_id"]
        cur_dl = p.get("downloads", 0)
        base_dl = baseline_map.get(pid, 0)
        delta = cur_dl - base_dl
        if delta > 0:
            rate = delta / hours
            growth_pct = round((delta / base_dl * 100) if base_dl > 0 else 0.0, 2)
            raw_score = compute_momentum_score(delta, growth_pct, rate, base_dl)
            af = anomaly_factors.get(pid)
            is_anom = af is not None and af > 3.0
            top_projects.append({
                "project_id": pid,
                "title": p.get("title", ""),
                "slug": p.get("slug", ""),
                "categories": p.get("categories", []),
                "current_downloads": cur_dl,
                "baseline_downloads": base_dl,
                "delta_downloads": delta,
                "downloads_per_hour": round(rate, 2),
                "growth_pct": growth_pct,
                "momentum_score": round(raw_score * anomaly_momentum_penalty(af), 2),
                "is_anomaly": is_anom,
                "anomaly_factor": round(af, 2) if af is not None else None,
            })
        elif delta < 0:
            # Enhancement C: capture declining projects
            rate = delta / hours
            growth_pct = round((delta / base_dl * 100) if base_dl > 0 else 0.0, 2)
            declining_projects.append({
                "project_id": pid,
                "title": p.get("title", ""),
                "slug": p.get("slug", ""),
                "categories": p.get("categories", []),
                "current_downloads": cur_dl,
                "baseline_downloads": base_dl,
                "delta_downloads": delta,
                "downloads_per_hour": round(rate, 2),
                "growth_pct": growth_pct,
                "momentum_score": compute_momentum_score(delta, growth_pct, rate, base_dl),
            })
    top_projects.sort(key=lambda x: x["delta_downloads"], reverse=True)
    top_projects = top_projects[:50]
    declining_projects.sort(key=lambda x: x["delta_downloads"])  # most negative first
    declining_projects = declining_projects[:50]

    # ── Top version+loader growth (aggregated by game_version+loader pair) ──
    def _norm_gv(gv):
        return (gv or "").strip()

    def _norm_loader(loader):
        return (loader or "").strip().lower()

    vl_pair_stats = {}
    for v in current_versions:
        vid = v.get("version_id")
        if not vid:
            continue
        current_dl = v.get("downloads", 0) or 0
        baseline_dl = baseline_version_map.get(vid, 0)
        delta = current_dl - baseline_dl
        if delta <= 0:
            continue
        pid = v.get("project_id", "")
        loaders = v.get("loaders", []) or []
        game_versions = v.get("game_versions", []) or []
        for loader in loaders:
            for gv in game_versions:
                norm_gv = _norm_gv(gv)
                norm_loader = _norm_loader(loader)
                key = (norm_gv, norm_loader)
                if key not in vl_pair_stats:
                    vl_pair_stats[key] = {
                        "game_version": norm_gv,
                        "loader": norm_loader,
                        "delta_downloads": 0,
                        "downloads_per_hour": 0.0,
                        "project_count": 0,
                        "top_project_id": pid,
                        "top_project_title": project_title_map.get(pid, pid),
                        "top_project_delta": 0,
                    }
                stat = vl_pair_stats[key]
                stat["delta_downloads"] += delta
                stat["project_count"] += 1
                if delta > stat["top_project_delta"]:
                    stat["top_project_delta"] = delta
                    stat["top_project_id"] = pid
                    stat["top_project_title"] = project_title_map.get(pid, pid)
    # Compute per-hour rate for each VL pair
    for stat in vl_pair_stats.values():
        stat["downloads_per_hour"] = round(stat["delta_downloads"] / hours, 2)

    top_version_loaders = sorted(vl_pair_stats.values(), key=lambda x: x["delta_downloads"], reverse=True)[:200]

    # ── Per-project version+loader pairs (for project detail panel) ──
    project_vl_pairs = {}
    for v in current_versions:
        vid = v.get("version_id")
        if not vid:
            continue
        current_dl = v.get("downloads", 0) or 0
        baseline_dl = baseline_version_map.get(vid, 0)
        delta = current_dl - baseline_dl
        if delta <= 0:
            continue
        pid = v.get("project_id", "")
        if not pid:
            continue
        loaders = v.get("loaders", []) or []
        game_versions = v.get("game_versions", []) or []
        if pid not in project_vl_pairs:
            project_vl_pairs[pid] = {}
        proj_map = project_vl_pairs[pid]
        for loader in loaders:
            for gv in game_versions:
                norm_gv = _norm_gv(gv)
                norm_loader = _norm_loader(loader)
                key = (norm_gv, norm_loader)
                if key not in proj_map:
                    proj_map[key] = {
                        "game_version": norm_gv,
                        "loader": norm_loader,
                        "delta_downloads": 0,
                    }
                proj_map[key]["delta_downloads"] += delta

    project_vl_pairs_list = {}
    for pid, proj_map in project_vl_pairs.items():
        sorted_list = sorted(proj_map.values(), key=lambda x: x["delta_downloads"], reverse=True)
        project_vl_pairs_list[pid] = sorted_list
    project_vl_pairs = project_vl_pairs_list

    # ── All project deltas (Enhancement C: include ALL projects, not just growing) ──
    TOP_VL_PER_PROJECT = 10
    all_project_deltas = []
    for p in current_projects:
        pid = p["project_id"]
        cur_dl = p.get("downloads", 0)
        delta = cur_dl - baseline_map.get(pid, 0)
        if delta == 0:
            continue
        base_dl = baseline_map.get(pid, 0)
        proj_vls = project_vl_pairs.get(pid, [])[:TOP_VL_PER_PROJECT]
        rate = delta / hours
        growth_pct = round((delta / base_dl * 100) if base_dl > 0 else 0.0, 2)
        raw_score = compute_momentum_score(delta, growth_pct, rate, base_dl)
        af = anomaly_factors.get(pid)
        is_anom = af is not None and af > 3.0
        all_project_deltas.append({
            "project_id": pid,
            "title": p.get("title", ""),
            "slug": p.get("slug", ""),
            "categories": p.get("categories", []),
            "current_downloads": cur_dl,
            "delta_downloads": delta,
            "downloads_per_hour": round(rate, 2),
            "growth_pct": growth_pct,
            "momentum_score": round(raw_score * anomaly_momentum_penalty(af), 2),
            "is_anomaly": is_anom,
            "anomaly_factor": round(af, 2) if af is not None else None,
            "top_vl_pairs": proj_vls,
        })
    all_project_deltas.sort(key=lambda x: x["delta_downloads"], reverse=True)

    return {
        "summary": summary,
        "category_rankings": category_rankings,
        "category_trending": category_trending,
        "loader_rankings": loader_rankings,
        "top_projects": top_projects,
        "declining_projects": declining_projects,
        "top_version_loaders": top_version_loaders,
        "all_project_deltas": all_project_deltas,
        "project_vl_pairs": project_vl_pairs,
    }


# ═══════════════════════════════════════════════════════════════════
#  TREND HISTORY (7-day time series)
# ═══════════════════════════════════════════════════════════════════


def build_project_velocity_history(project_type, max_files=20, top_n=50):
    """Enhancement #2: Per-project velocity history.

    Loads the last `max_files` analysis files for this project type and
    extracts per-project {downloads, downloads_per_hour, momentum_score}
    time-series for the top `top_n` projects (by latest momentum_score).

    Returns dict: {project_id: [{timestamp, downloads, downloads_per_hour, momentum_score}, ...]}

    This is a compact file (~150 KB per type) so the frontend can render
    sparklines per top project without loading all raw snapshots.
    """
    import os
    analysis_dir = get_analysis_dir(project_type)
    if not os.path.exists(analysis_dir):
        return {}

    # List analysis files (excluding sub-analyses which have "sub" in their type)
    files = sorted([f for f in os.listdir(analysis_dir) if f.endswith(".json")])
    if len(files) < 2:
        return {}

    # Take the last max_files
    recent_files = files[-max_files:]
    print(f"  Loading {len(recent_files)} analysis files for velocity history...")

    # Per-project series
    project_series = {}  # pid -> list of {timestamp, downloads, downloads_per_hour, momentum_score, title, slug}

    for fname in recent_files:
        fpath = os.path.join(analysis_dir, fname)
        try:
            data = load_json(fpath)
        except Exception:
            continue
        if not data:
            continue
        # Skip sub-analyses (predictive)
        if data.get("analysis_type") == "sub":
            continue

        ts = data.get("timestamp", "")
        # Collect top projects from this analysis
        top_projects = data.get("top_projects", []) or []
        for p in top_projects:
            pid = p.get("project_id")
            if not pid:
                continue
            project_series.setdefault(pid, [])
            project_series[pid].append({
                "timestamp": ts,
                "downloads": p.get("current_downloads", 0),
                "downloads_per_hour": p.get("downloads_per_hour", 0),
                "momentum_score": p.get("momentum_score", 0),
                "title": p.get("title", ""),
                "slug": p.get("slug", ""),
            })
        # Also include declining projects (negative momentum)
        declining = data.get("declining_projects", []) or []
        for p in declining:
            pid = p.get("project_id")
            if not pid:
                continue
            project_series.setdefault(pid, [])
            project_series[pid].append({
                "timestamp": ts,
                "downloads": p.get("current_downloads", 0),
                "downloads_per_hour": p.get("downloads_per_hour", 0),
                "momentum_score": p.get("momentum_score", 0),
                "title": p.get("title", ""),
                "slug": p.get("slug", ""),
            })

    # Filter to top_n projects by their latest momentum_score
    if not project_series:
        return {}

    # Get the latest entry's momentum_score for each project
    latest_scores = {}
    for pid, series in project_series.items():
        if series:
            latest_scores[pid] = series[-1].get("momentum_score", 0)
    top_pids = sorted(latest_scores.keys(), key=lambda pid: latest_scores[pid], reverse=True)[:top_n]

    result = {}
    for pid in top_pids:
        # Sort by timestamp and keep only the data points (drop title/slug from each entry to save space)
        series = sorted(project_series[pid], key=lambda x: x["timestamp"])
        # Compact form: just keep title/slug once at the top level
        title = series[-1].get("title", "") if series else ""
        slug = series[-1].get("slug", "") if series else ""
        result[pid] = {
            "title": title,
            "slug": slug,
            "history": [
                {
                    "timestamp": e["timestamp"],
                    "downloads": e["downloads"],
                    "downloads_per_hour": e["downloads_per_hour"],
                    "momentum_score": e["momentum_score"],
                }
                for e in series
            ],
        }
    return result


def build_trend_history(project_type):
    """Build 7-day trend history from raw snapshots.

    Loads all raw snapshots from the last 7 days, keeps only the first and
    last snapshot per day (boundary snapshots), then computes daily deltas
    for:
      - overall totals (downloads, projects)
      - per-category totals
      - per-version+loader totals

    Returns:
        trend_history: list of {date, total_downloads, new_downloads, new_projects}
        category_trend_history: {category: [{date, total_downloads, new_downloads, growth_pct}]}
        vl_trend_history: {"gv|loader": [{date, total_downloads, delta_downloads, project_count}]}
    """
    raw_dir = get_raw_dir(project_type)
    snapshot_files = list_snapshot_files(raw_dir)
    if not snapshot_files:
        return [], {}, {}

    # Parse timestamps from filenames and filter to last 7 days
    now = datetime.now(BEIJING_TZ)
    cutoff = now - timedelta(days=7)
    dated_files = []
    for f in snapshot_files:
        fname = f.split("/")[-1]
        ts_str = fname.replace(".json.gz", "").replace(".json", "")
        st = parse_snapshot_timestamp({"timestamp": ts_str})
        if st and st >= cutoff:
            dated_files.append((st, f))

    if not dated_files:
        return [], {}, {}

    dated_files.sort(key=lambda x: x[0])

    # Group by date string (YYYY-MM-DD), keep first and last per day
    day_groups = {}
    for st, f in dated_files:
        date_str = st.strftime("%Y-%m-%d")
        day_groups.setdefault(date_str, []).append(f)

    boundary_files = []
    for date_str in sorted(day_groups.keys()):
        files = day_groups[date_str]
        boundary_files.append(files[0])   # first snapshot of day
        if len(files) > 1:
            boundary_files.append(files[-1])  # last snapshot of day

    # Deduplicate consecutive same-file entries
    deduped = []
    for f in boundary_files:
        if not deduped or deduped[-1] != f:
            deduped.append(f)
    boundary_files = deduped

    # Load boundary snapshots
    boundary_snapshots = []
    for f in boundary_files:
        data = load_json(f)
        if data:
            boundary_snapshots.append(data)

    if len(boundary_snapshots) < 2:
        return [], {}, {}

    # ── Overall trend ───────────────────────────────────────────────
    trend_history = []
    prev_total_downloads = None
    prev_project_count = None
    for snap in boundary_snapshots:
        total_downloads = snap.get("total_downloads", 0)
        project_count = snap.get("project_count", len(snap.get("projects", [])))
        date = snap.get("date", "")
        new_downloads = max(0, total_downloads - (prev_total_downloads or 0))
        new_projects = max(0, project_count - (prev_project_count or 0))
        trend_history.append({
            "date": date,
            "timestamp": snap.get("timestamp", ""),
            "total_downloads": total_downloads,
            "new_downloads": new_downloads,
            "new_projects": new_projects,
            "analysis_type": "daily",
        })
        prev_total_downloads = total_downloads
        prev_project_count = project_count

    # ── Category trend ──────────────────────────────────────────────
    category_trend_history = {}
    for snap in boundary_snapshots:
        date = snap.get("date", "")
        projects = snap.get("projects", [])
        cat_totals = {}
        for p in projects:
            for cat in p.get("categories", []):
                cat_totals[cat] = cat_totals.get(cat, 0) + (p.get("downloads", 0) or 0)

        for cat, total in cat_totals.items():
            if cat not in category_trend_history:
                category_trend_history[cat] = []
            prev_total = category_trend_history[cat][-1]["total_downloads"] if category_trend_history[cat] else 0
            new_dl = max(0, total - prev_total)
            growth_pct = round((new_dl / prev_total * 100) if prev_total > 0 else 0.0, 2)
            category_trend_history[cat].append({
                "date": date,
                "total_downloads": total,
                "new_downloads": new_dl,
                "growth_pct": growth_pct,
            })

    # ── Version+Loader trend ────────────────────────────────────────
    vl_trend_history = {}
    for snap in boundary_snapshots:
        date = snap.get("date", "")
        versions = snap.get("versions", [])
        vl_totals = {}
        for v in versions:
            loaders = v.get("loaders", []) or []
            game_versions = v.get("game_versions", []) or []
            dl = v.get("downloads", 0) or 0
            for loader in loaders:
                for gv in game_versions:
                    norm_gv = (gv or "").strip()
                    norm_loader = (loader or "").strip().lower()
                    key = f"{norm_gv}\u0001{norm_loader}"
                    if key not in vl_totals:
                        vl_totals[key] = {"total_downloads": 0, "project_count": 0}
                    vl_totals[key]["total_downloads"] += dl
                    vl_totals[key]["project_count"] += 1

        for key, totals in vl_totals.items():
            if key not in vl_trend_history:
                vl_trend_history[key] = []
            prev = vl_trend_history[key][-1] if vl_trend_history[key] else None
            prev_total = prev["total_downloads"] if prev else 0
            new_dl = max(0, totals["total_downloads"] - prev_total)
            vl_trend_history[key].append({
                "date": date,
                "total_downloads": totals["total_downloads"],
                "delta_downloads": new_dl,
                "project_count": totals["project_count"],
            })

    return trend_history, category_trend_history, vl_trend_history


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Analyze market data for a project type")
    parser.add_argument("--project-type", required=True,
                        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "plugin"],
                        help="Project type to analyze")
    parser.add_argument("--mode", required=True,
                        choices=["daily", "hourly"],
                        help="Analysis mode: daily (24h) or hourly (2h)")
    args = parser.parse_args()

    project_type = args.project_type
    mode = args.mode
    hours_back = 24 if mode == "daily" else 2

    print(f"=== Analyze ({project_type}) — {mode} ===")

    # ── List snapshot files with timestamps (no file loading) ────
    # OPTIMIZATION: Parse timestamps from filenames instead of loading all
    # snapshots into memory. Loading 26-52 snapshots (~400MB+) was causing
    # the Analyze step to be cancelled on GitHub Actions runners.
    snapshot_files_ts = list_snapshot_files_with_ts(project_type)
    if len(snapshot_files_ts) < 2:
        print(f"Need at least 2 snapshots for analysis (have {len(snapshot_files_ts)}). Skipping.")
        return 0

    current_filepath, current_ts = snapshot_files_ts[-1]
    print(f"  Total snapshots: {len(snapshot_files_ts)}")

    # ── Find baseline files (from filenames only — no loading) ───
    # Enhancement #1: Rolling-window baseline — find up to 3 closest snapshots
    # within tolerance, then take the median per project to smooth anomalies.
    baseline_candidates = find_baseline_files_window(snapshot_files_ts, current_ts, hours_back, max_files=3)
    baseline_found_in_window = len(baseline_candidates) > 0
    if not baseline_candidates:
        baseline_filepath, baseline_ts = snapshot_files_ts[0]
        baseline_candidates = [(baseline_filepath, baseline_ts, 0)]
        print(f"  No {hours_back}h baseline found — using oldest snapshot ({baseline_ts}) as fallback")
    else:
        baseline_filepath, baseline_ts = baseline_candidates[0][0], baseline_candidates[0][1]
        diff_hours = abs((current_ts - baseline_ts).total_seconds() / 3600)
        print(f"  Baseline snapshot: {baseline_ts} (~{diff_hours:.1f}h ago)")
        print(f"  Rolling-window: {len(baseline_candidates)} baseline candidates within tolerance")

    # ── Load ONLY the needed snapshots (current + baseline files) ─
    print(f"  Loading current snapshot: {current_filepath}")
    current_snapshot = load_json(current_filepath)
    if not current_snapshot:
        print(f"  ERROR: Failed to load current snapshot from {current_filepath}")
        return 1
    current_date = current_snapshot.get("date", "")
    print(f"  Current snapshot: {current_date} ({current_ts})")

    print(f"  Loading primary baseline snapshot: {baseline_filepath}")
    primary_baseline_snapshot = load_json(baseline_filepath)
    if not primary_baseline_snapshot:
        print(f"  ERROR: Failed to load baseline snapshot from {baseline_filepath}")
        return 1

    # Enhancement #1: Build rolling baseline (median of up to 3 snapshots)
    baseline_snapshot = build_rolling_baseline(baseline_candidates, primary_baseline_snapshot)
    if baseline_snapshot is not primary_baseline_snapshot:
        print(f"  Built rolling baseline from {baseline_snapshot.get('baseline_source_count', 1)} snapshots (median)")
    baseline_source_count = baseline_snapshot.get("baseline_source_count", 1)

    # ── Compute actual time span & data quality (Enhancement B + F) ─
    if current_ts and baseline_ts:
        actual_hours_between = abs((current_ts - baseline_ts).total_seconds() / 3600)
    else:
        actual_hours_between = float(hours_back)

    # Quality: "normal" if actual ≈ requested (within 50% tolerance), else "extended"
    tolerance = hours_back * 0.5
    if baseline_found_in_window and abs(actual_hours_between - hours_back) <= tolerance:
        analysis_quality = "normal"
    else:
        analysis_quality = "extended"

    # Enhancement #6: Stale-snapshot detection
    # If the latest snapshot is more than 3h old, the workflow was delayed —
    # mark quality as "stale" so the frontend can show a warning banner.
    stale = False
    snapshot_age_hours = (datetime.now(BEIJING_TZ) - current_ts).total_seconds() / 3600
    if snapshot_age_hours > 3.0:
        stale = True
        analysis_quality = "stale"

    # Confidence based on snapshot count and quality
    snapshot_count = len(snapshot_files_ts)
    if snapshot_count >= 6 and analysis_quality == "normal":
        confidence = "high"
    elif snapshot_count >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    print(f"  Actual time span: {actual_hours_between:.1f}h (requested {hours_back}h, quality={analysis_quality}, confidence={confidence})")
    if stale:
        print(f"  WARNING: latest snapshot is {snapshot_age_hours:.1f}h old — marked as STALE")

    # ── Load filter sets ──────────────────────────────────────────
    loader_names, loader_set, content_cat_names = load_filter_sets(project_type)
    print(f"  Loaders: {len(loader_names)}, content categories: {len(content_cat_names)}")

    # Enhancement #3: Load anomaly factors from previous predictive run
    anomaly_factors = load_anomaly_factors(project_type)
    if anomaly_factors:
        print(f"  Anomaly factors loaded: {len(anomaly_factors)} projects flagged")

    # ── Build analysis ────────────────────────────────────────────
    analysis_data = build_project_analysis(
        current_snapshot, baseline_snapshot,
        project_type, loader_names, loader_set, content_cat_names,
        actual_hours_between, anomaly_factors=anomaly_factors,
    )

    print(f"  Summary: {analysis_data['summary']['total_projects']:,} projects, "
          f"{analysis_data['summary']['total_versions']:,} versions, "
          f"{analysis_data['summary']['total_downloads']:,} downloads")
    print(f"  New downloads: {analysis_data['summary']['new_downloads_since_baseline']:+,}")
    print(f"  Lost downloads: {analysis_data['summary']['lost_downloads_since_baseline']:-,}")
    print(f"  Net change: {analysis_data['summary']['net_download_change']:+,} "
          f"({analysis_data['summary']['downloads_per_hour']:,.0f}/h)")
    print(f"  Growing: {analysis_data['summary']['growing_projects']:,} | "
          f"Declining: {analysis_data['summary']['declining_projects']:,}")
    print(f"  Projects with delta != 0: {len(analysis_data['all_project_deltas']):,}")
    print(f"  VL pairs: {len(analysis_data['top_version_loaders'])}")
    cat_trending = analysis_data.get("category_trending", {})
    trending_total = sum(len(v) for v in cat_trending.values())
    print(f"  Category trending: {len(cat_trending)} categories, {trending_total} trending projects")

    # ── Build trend history ────────────────────────────────────────
    trend_history, category_trend_history, vl_trend_history = build_trend_history(project_type)
    if trend_history:
        print(f"  Trend history: {len(trend_history)} data points, "
              f"{len(category_trend_history)} categories, {len(vl_trend_history)} VL pairs")
        analysis_data["trend_history"] = trend_history
        analysis_data["category_trend_history"] = category_trend_history
        analysis_data["vl_trend_history"] = vl_trend_history

    # ── Enhancement #2: Per-project velocity history ────────────────
    # Compact per-project time-series (last 20 analyses × top 50 projects)
    # so the frontend can render sparklines without loading raw snapshots.
    type_dir = get_project_type_dir(project_type)
    project_velocity_history = build_project_velocity_history(project_type, max_files=20, top_n=50)
    if project_velocity_history:
        vh_path = f"{type_dir}/project_velocity_history.json"
        save_json(vh_path, project_velocity_history)
        print(f"Saved project_velocity_history to {vh_path} ({len(project_velocity_history)} projects)")

    # ── Save ──────────────────────────────────────────────────────
    timestamp = get_timestamp()

    # Extract project_vl_pairs and save as a separate file
    project_vl_pairs = analysis_data.pop("project_vl_pairs", {})
    vl_pairs_path = f"{type_dir}/project_vl_pairs.json"
    save_json(vl_pairs_path, project_vl_pairs)
    print(f"Saved project_vl_pairs to {vl_pairs_path} ({len(project_vl_pairs)} projects)")

    # Extract all_project_deltas and save as a separate file.
    # The frontend loads the condensed latest_analysis.json first (fast,
    # ~300 KB instead of ~10 MB), then lazy-loads all_project_deltas on
    # demand when the user searches or scrolls past the top 50 projects.
    all_project_deltas = analysis_data.pop("all_project_deltas", [])
    deltas_path = f"{type_dir}/all_project_deltas.json"
    save_json(deltas_path, all_project_deltas)
    print(f"Saved all_project_deltas to {deltas_path} ({len(all_project_deltas)} projects)")

    analysis = {
        "timestamp": timestamp,
        "date": current_date,
        "project_type": project_type,
        "analysis_type": mode,
        "baseline_date": baseline_snapshot.get("date", ""),
        "hours_between": hours_back,
        "actual_hours_between": round(actual_hours_between, 2),
        "analysis_quality": analysis_quality,
        "data_quality": {
            "snapshot_count": snapshot_count,
            "requested_hours": hours_back,
            "actual_hours": round(actual_hours_between, 2),
            "baseline_found_in_window": baseline_found_in_window,
            "quality": analysis_quality,
            "confidence": confidence,
            "stale": stale,
            "snapshot_age_hours": round(snapshot_age_hours, 2),
            "baseline_source_count": baseline_source_count,
        },
        **analysis_data,
    }

    # Save timestamped analysis
    analysis_dir = get_analysis_dir(project_type)
    ensure_dir(analysis_dir)
    analysis_path = f"{analysis_dir}/{timestamp}.json"
    save_json(analysis_path, analysis)
    print(f"Saved analysis to {analysis_path}")

    # Save latest analysis (for the app)
    latest_path = f"{type_dir}/latest_analysis.json"
    save_json(latest_path, analysis)
    print(f"Saved latest analysis to {latest_path}")

    print(f"=== Analyze ({project_type}) {mode} complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
