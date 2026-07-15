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


def load_all_snapshots(project_type):
    """Load all raw snapshots sorted by timestamp."""
    raw_dir = get_raw_dir(project_type)
    snapshot_files = list_snapshot_files(raw_dir)
    snapshots = []
    for f in snapshot_files:
        data = load_json(f)
        if data:
            snapshots.append(data)
    return snapshots


def find_baseline_snapshot(snapshots, current_snapshot, hours_back):
    """Find the snapshot closest to `hours_back` hours before the current snapshot."""
    current_ts = parse_snapshot_timestamp(current_snapshot)
    if not current_ts:
        return None
    target = current_ts - timedelta(hours=hours_back)
    window = timedelta(hours=hours_back * 0.5)
    best = None
    best_diff = None
    for snap in snapshots:
        st = parse_snapshot_timestamp(snap)
        if not st:
            continue
        if st >= current_ts:
            continue
        diff = abs((st - target).total_seconds())
        if best_diff is None or diff < best_diff:
            if diff <= window.total_seconds():
                best = snap
                best_diff = diff
    return best


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


def build_project_analysis(current_snapshot, baseline_snapshot,
                           project_type, loader_names, loader_set, content_cat_names):
    """Build simple delta analysis. Returns a dict with the essential sections."""
    current_projects = current_snapshot.get("projects", [])
    current_total_downloads = current_snapshot.get("total_downloads", 0)
    baseline_date = baseline_snapshot.get("date", "")
    current_date = current_snapshot.get("date", "")

    baseline_map = {p["project_id"]: p.get("downloads", 0) for p in baseline_snapshot.get("projects", [])}
    current_map = {p["project_id"]: p.get("downloads", 0) for p in current_projects}

    # ── Version data ──────────────────────────────────────────────
    current_versions = current_snapshot.get("versions", [])
    baseline_versions = baseline_snapshot.get("versions", [])
    baseline_version_map = {v.get("version_id"): v.get("downloads", 0) for v in baseline_versions if v.get("version_id")}

    # ── Summary ───────────────────────────────────────────────────
    new_downloads = 0
    for pid, cur_dl in current_map.items():
        delta = cur_dl - baseline_map.get(pid, 0)
        if delta > 0:
            new_downloads += delta

    summary = {
        "total_projects": current_snapshot.get("project_count", len(current_projects)),
        "total_versions": len(current_versions),
        "total_downloads": current_total_downloads,
        "baseline_date": baseline_date,
        "current_date": current_date,
        "new_projects_since_baseline": sum(1 for pid in current_map if pid not in baseline_map),
        "new_downloads_since_baseline": new_downloads,
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
            "growth_pct": round((new_dl / baseline_total * 100) if baseline_total > 0 else 0.0, 2),
        })
    category_rankings.sort(key=lambda x: x["new_downloads"], reverse=True)

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
        loader_rankings.append({
            "loader": loader,
            "projects": stat["projects"],
            "total_downloads": stat["total_downloads"],
            "new_downloads": stat["new_downloads"],
        })
    loader_rankings.sort(key=lambda x: x["new_downloads"], reverse=True)

    # ── Top projects (top 50 by delta, with full details) ─────────
    project_title_map = {p["project_id"]: p.get("title", "") for p in current_projects}
    top_projects = []
    for p in current_projects:
        pid = p["project_id"]
        cur_dl = p.get("downloads", 0)
        base_dl = baseline_map.get(pid, 0)
        delta = cur_dl - base_dl
        if delta > 0:
            top_projects.append({
                "project_id": pid,
                "title": p.get("title", ""),
                "slug": p.get("slug", ""),
                "categories": p.get("categories", []),
                "current_downloads": cur_dl,
                "baseline_downloads": base_dl,
                "delta_downloads": delta,
                "growth_pct": round((delta / base_dl * 100) if base_dl > 0 else 0.0, 2),
            })
    top_projects.sort(key=lambda x: x["delta_downloads"], reverse=True)
    top_projects = top_projects[:50]

    # ── Top version+loader growth (aggregated by game_version+loader pair) ──
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
                key = (gv, loader)
                if key not in vl_pair_stats:
                    vl_pair_stats[key] = {
                        "game_version": gv,
                        "loader": loader,
                        "delta_downloads": 0,
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
        for loader in loaders:
            for gv in game_versions:
                if pid not in project_vl_pairs:
                    project_vl_pairs[pid] = []
                project_vl_pairs[pid].append({
                    "game_version": gv,
                    "loader": loader,
                    "delta_downloads": delta,
                })

    for pid in project_vl_pairs:
        project_vl_pairs[pid].sort(key=lambda x: x["delta_downloads"], reverse=True)

    # ── All project deltas (for the full scrollable list) ──────────
    all_project_deltas = [
        {
            "project_id": p["project_id"],
            "title": p.get("title", ""),
            "slug": p.get("slug", ""),
            "categories": p.get("categories", []),
            "current_downloads": p.get("downloads", 0),
            "delta_downloads": p.get("downloads", 0) - baseline_map.get(p["project_id"], 0),
        }
        for p in current_projects
        if p.get("downloads", 0) - baseline_map.get(p["project_id"], 0) > 0
    ]
    all_project_deltas.sort(key=lambda x: x["delta_downloads"], reverse=True)

    return {
        "summary": summary,
        "category_rankings": category_rankings,
        "loader_rankings": loader_rankings,
        "top_projects": top_projects,
        "top_version_loaders": top_version_loaders,
        "all_project_deltas": all_project_deltas,
        "project_vl_pairs": project_vl_pairs,
    }


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Analyze market data for a project type")
    parser.add_argument("--project-type", required=True,
                        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "world"],
                        help="Project type to analyze")
    parser.add_argument("--mode", required=True,
                        choices=["daily", "hourly"],
                        help="Analysis mode: daily (24h) or hourly (2h)")
    args = parser.parse_args()

    project_type = args.project_type
    mode = args.mode
    hours_back = 24 if mode == "daily" else 2

    print(f"=== Analyze ({project_type}) — {mode} ===")

    # ── Load raw snapshot history ─────────────────────────────────
    snapshots = load_all_snapshots(project_type)
    if len(snapshots) < 2:
        print(f"Need at least 2 snapshots for analysis (have {len(snapshots)}). Skipping.")
        return 0

    current_snapshot = snapshots[-1]
    current_date = current_snapshot.get("date", "")
    current_ts = parse_snapshot_timestamp(current_snapshot)
    print(f"  Current snapshot: {current_date} ({current_ts})")
    print(f"  Total snapshots: {len(snapshots)}")

    # ── Find baseline snapshot ────────────────────────────────────
    baseline_snapshot = find_baseline_snapshot(snapshots, current_snapshot, hours_back)
    if not baseline_snapshot:
        baseline_snapshot = snapshots[0]
        print(f"  No {hours_back}h baseline found — using oldest snapshot ({baseline_snapshot.get('date', '?')}) as fallback")
    else:
        baseline_ts = parse_snapshot_timestamp(baseline_snapshot)
        diff_hours = abs((current_ts - baseline_ts).total_seconds() / 3600) if current_ts and baseline_ts else hours_back
        print(f"  Baseline snapshot: {baseline_snapshot.get('date', '?')} ({baseline_ts}, ~{diff_hours:.1f}h ago)")

    # ── Load filter sets ──────────────────────────────────────────
    loader_names, loader_set, content_cat_names = load_filter_sets(project_type)
    print(f"  Loaders: {len(loader_names)}, content categories: {len(content_cat_names)}")

    # ── Build analysis ────────────────────────────────────────────
    analysis_data = build_project_analysis(
        current_snapshot, baseline_snapshot,
        project_type, loader_names, loader_set, content_cat_names
    )

    print(f"  Summary: {analysis_data['summary']['total_projects']:,} projects, "
          f"{analysis_data['summary']['total_versions']:,} versions, "
          f"{analysis_data['summary']['total_downloads']:,} downloads")
    print(f"  New downloads: {analysis_data['summary']['new_downloads_since_baseline']:+,}")
    print(f"  Projects with delta > 0: {len(analysis_data['all_project_deltas']):,}")
    print(f"  VL pairs: {len(analysis_data['top_version_loaders'])}")

    # ── Save ──────────────────────────────────────────────────────
    timestamp = get_timestamp()

    # Extract project_vl_pairs and save as a separate file
    project_vl_pairs = analysis_data.pop("project_vl_pairs", {})
    type_dir = get_project_type_dir(project_type)
    vl_pairs_path = f"{type_dir}/project_vl_pairs.json"
    save_json(vl_pairs_path, project_vl_pairs)
    print(f"Saved project_vl_pairs to {vl_pairs_path} ({len(project_vl_pairs)} projects)")

    analysis = {
        "timestamp": timestamp,
        "date": current_date,
        "project_type": project_type,
        "analysis_type": mode,
        "baseline_date": baseline_snapshot.get("date", ""),
        "hours_between": hours_back,
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
