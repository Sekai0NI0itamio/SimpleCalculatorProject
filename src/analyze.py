#!/usr/bin/env python3
"""
Phase 5: Analyze — History-Based Market Intelligence

All analysis is derived from the raw snapshot history stored in
data/{project_type}/raw/. The first snapshot is the baseline, the last
is the current state. Version download data comes from the project-type DB.

Outputs:
  - data/{project_type}/analysis/{timestamp}.json  — full analysis
  - data/{project_type}/latest_analysis.json       — same content (for the app)

Category filtering:
  - Loads loader names from data/{project_type}/loaders.json (/tag/loader API)
  - Loads category headers from data/{project_type}/categories.json (/tag/category API)
  - Content categories = header == "categories" AND not a loader name
  - Loaders, resolutions, features, etc. are excluded from category rankings
"""
import argparse
import glob
import json
import math
import sys

from utils import (
    load_json, save_json, ensure_dir, get_timestamp,
    get_project_type_dir, get_raw_dir, get_analysis_dir
)
from db import Database

CONTENT_CATEGORY_HEADER = "categories"


# ═══════════════════════════════════════════════════════════════════
#  MATH HELPERS
# ═══════════════════════════════════════════════════════════════════


def percentile(sorted_data, p):
    """Percentile with linear interpolation. p in 0-100. sorted_data ascending."""
    if not sorted_data:
        return 0.0
    n = len(sorted_data)
    if n == 1:
        return float(sorted_data[0])
    rank = (p / 100.0) * (n - 1)
    lower = int(rank)
    upper = lower + 1
    if upper >= n:
        return float(sorted_data[n - 1])
    frac = rank - lower
    return float(sorted_data[lower] + (sorted_data[upper] - sorted_data[lower]) * frac)


def gini_coefficient(values):
    """Gini coefficient (0=equal, 1=unequal).
    G = (2 * sum(i * x_i)) / (n * sum(x_i)) - (n+1)/n, x_i sorted ascending, i 1-indexed."""
    n = len(values)
    if n == 0:
        return 0.0
    sorted_vals = sorted(values)
    total = sum(sorted_vals)
    if total == 0:
        return 0.0
    cum = 0
    for i, x in enumerate(sorted_vals, start=1):
        cum += i * x
    g = (2 * cum) / (n * total) - (n + 1) / n
    return g


def population_std_dev(values, mean):
    """Population standard deviation."""
    n = len(values)
    if n == 0:
        return 0.0
    return (sum((x - mean) ** 2 for x in values) / n) ** 0.5


def parse_json_field(val):
    """Parse a DB field that may be a JSON string or already a list."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(val, list):
        return val
    return []


# ═══════════════════════════════════════════════════════════════════
#  SNAPSHOT LOADING
# ═══════════════════════════════════════════════════════════════════


def load_all_snapshots(project_type):
    """Load all raw snapshots sorted by filename (timestamp)."""
    raw_dir = get_raw_dir(project_type)
    snapshot_files = sorted(glob.glob(f"{raw_dir}/*.json"))
    snapshots = []
    for f in snapshot_files:
        data = load_json(f)
        if data:
            snapshots.append(data)
    return snapshots


def load_filter_sets(project_type):
    """Load loader names and content category names for filtering.

    Returns (loader_names_list, loader_set, content_cat_names_set).
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

    return loaders, loader_set, content_cats


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Analyze market data for a project type")
    parser.add_argument(
        "--project-type", required=True,
        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "world"],
        help="Project type to analyze"
    )
    args = parser.parse_args()

    project_type = args.project_type
    print(f"=== Phase 5: Analyze ({project_type}) — History-Based Intelligence ===")

    # ── Load raw snapshot history ─────────────────────────────────
    snapshots = load_all_snapshots(project_type)
    if not snapshots:
        print(f"Error: no raw snapshots found in {get_raw_dir(project_type)}/. Run snapshot.py first.")
        return 1

    baseline_snapshot = snapshots[0]
    current_snapshot = snapshots[-1]
    baseline_date = baseline_snapshot.get("date", "")
    current_date = current_snapshot.get("date", "")
    baseline_total_downloads = baseline_snapshot.get("total_downloads", 0)
    current_total_downloads = current_snapshot.get("total_downloads", 0)

    print(f"  Snapshots: {len(snapshots)} (baseline {baseline_date} -> current {current_date})")
    print(f"  Baseline downloads: {baseline_total_downloads:,}")
    print(f"  Current downloads:  {current_total_downloads:,}")

    # Build download maps
    baseline_map = {p["project_id"]: p.get("downloads", 0) for p in baseline_snapshot.get("projects", [])}
    current_projects = current_snapshot.get("projects", [])
    current_map = {p["project_id"]: p.get("downloads", 0) for p in current_projects}

    # ── Load filter sets ──────────────────────────────────────────
    loader_names, loader_set, content_cat_names = load_filter_sets(project_type)
    print(f"  Loaders: {len(loader_names)}, content categories: {len(content_cat_names)}")

    # ── Open DB for version data ──────────────────────────────────
    db = Database(project_type)

    # ── Summary ───────────────────────────────────────────────────
    new_projects_since_baseline = sum(1 for pid in current_map if pid not in baseline_map)
    new_downloads_since_baseline = 0
    for pid, cur_dl in current_map.items():
        delta = cur_dl - baseline_map.get(pid, 0)
        if delta > 0:
            new_downloads_since_baseline += delta

    total_versions = db.conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]

    summary = {
        "total_projects": current_snapshot.get("project_count", len(current_projects)),
        "total_versions": total_versions,
        "total_downloads": current_total_downloads,
        "baseline_date": baseline_date,
        "current_date": current_date,
        "new_projects_since_baseline": new_projects_since_baseline,
        "new_downloads_since_baseline": new_downloads_since_baseline,
    }
    print(f"  Summary: {summary['total_projects']:,} projects, {total_versions:,} versions, {current_total_downloads:,} downloads")

    # ── Trend (time series) ───────────────────────────────────────
    trend = []
    for snap in snapshots:
        snap_total = snap.get("total_downloads", 0)
        delta = snap_total - baseline_total_downloads
        growth_pct = (delta / baseline_total_downloads * 100) if baseline_total_downloads > 0 else 0.0
        trend.append({
            "date": snap.get("date", ""),
            "total_downloads": snap_total,
            "total_projects": snap.get("project_count", 0),
            "delta_from_baseline": delta,
            "growth_pct": round(growth_pct, 2),
        })
    print(f"  Trend: {len(trend)} points")

    # ── Category rankings (content categories only) ──────────────
    # Group current projects by their content categories
    cat_projects = {}  # cat -> list of project dicts (current)
    for p in current_projects:
        for cat in p.get("categories", []):
            if cat in content_cat_names:
                cat_projects.setdefault(cat, []).append(p)

    category_rankings = []
    category_stats = {}  # cat -> {avg_downloads, new_downloads, projects, total_downloads}
    for cat, projs in cat_projects.items():
        current_total = sum(p.get("downloads", 0) for p in projs)
        baseline_total = sum(baseline_map.get(p["project_id"], 0) for p in projs)
        new_downloads = current_total - baseline_total
        project_count = len(projs)
        avg_downloads = current_total / project_count if project_count > 0 else 0.0
        growth_pct = (new_downloads / baseline_total * 100) if baseline_total > 0 else 0.0
        market_share = (current_total / current_total_downloads * 100) if current_total_downloads > 0 else 0.0
        category_rankings.append({
            "category": cat,
            "projects": project_count,
            "total_downloads": current_total,
            "avg_downloads": round(avg_downloads, 2),
            "new_downloads": new_downloads,
            "growth_pct": round(growth_pct, 2),
            "market_share": round(market_share, 2),
        })
        category_stats[cat] = {
            "avg_downloads": avg_downloads,
            "new_downloads": new_downloads,
            "projects": project_count,
            "total_downloads": current_total,
        }
    category_rankings.sort(key=lambda x: x["total_downloads"], reverse=True)
    print(f"  Category rankings: {len(category_rankings)} content categories")

    # ── Loader rankings (built from category-style project stats) ──
    loader_rankings = []
    for loader in loader_names:
        projs = [p for p in current_projects if loader in p.get("categories", [])]
        if not projs:
            loader_rankings.append({
                "loader": loader,
                "projects": 0,
                "total_downloads": 0,
                "avg_downloads": 0.0,
                "growth_pct": 0.0,
                "market_share": 0.0,
            })
            continue
        current_total = sum(p.get("downloads", 0) for p in projs)
        baseline_total = sum(baseline_map.get(p["project_id"], 0) for p in projs)
        project_count = len(projs)
        avg_downloads = current_total / project_count if project_count > 0 else 0.0
        growth_pct = ((current_total - baseline_total) / baseline_total * 100) if baseline_total > 0 else 0.0
        market_share = (current_total / current_total_downloads * 100) if current_total_downloads > 0 else 0.0
        loader_rankings.append({
            "loader": loader,
            "projects": project_count,
            "total_downloads": current_total,
            "avg_downloads": round(avg_downloads, 2),
            "growth_pct": round(growth_pct, 2),
            "market_share": round(market_share, 2),
        })
    loader_rankings.sort(key=lambda x: x["total_downloads"], reverse=True)
    print(f"  Loader rankings: {len(loader_rankings)} loaders")

    # ── Top projects (by delta_downloads) ─────────────────────────
    top_projects = []
    for p in current_projects:
        pid = p["project_id"]
        baseline_dl = baseline_map.get(pid, 0)
        current_dl = p.get("downloads", 0)
        delta = current_dl - baseline_dl
        growth_pct = (delta / baseline_dl * 100) if baseline_dl > 0 else 0.0
        top_projects.append({
            "project_id": pid,
            "title": p.get("title", ""),
            "slug": p.get("slug", ""),
            "categories": p.get("categories", []),
            "baseline_downloads": baseline_dl,
            "current_downloads": current_dl,
            "delta_downloads": delta,
            "growth_pct": round(growth_pct, 2),
        })
    top_projects.sort(key=lambda x: x["delta_downloads"], reverse=True)
    top_projects = top_projects[:100]
    print(f"  Top projects: {len(top_projects)} (by delta_downloads)")

    # ── Top version+loader growth ─────────────────────────────────
    baseline_version_map = db.get_latest_version_snapshots(baseline_date)
    project_title_map = {p["project_id"]: p.get("title", "") for p in current_projects}

    cursor = db.conn.execute(
        "SELECT id, project_id, version_number, loaders, game_versions, downloads FROM versions"
    )
    top_version_loaders = []
    for row in cursor.fetchall():
        vid = row["id"]
        current_dl = row["downloads"] or 0
        baseline_dl = baseline_version_map.get(vid, 0)
        delta = current_dl - baseline_dl
        if delta > 0:
            pid = row["project_id"]
            top_version_loaders.append({
                "version_id": vid,
                "project_id": pid,
                "project_title": project_title_map.get(pid, pid),
                "version_number": row["version_number"],
                "loaders": parse_json_field(row["loaders"]),
                "game_versions": parse_json_field(row["game_versions"]),
                "delta_downloads": delta,
            })
    top_version_loaders.sort(key=lambda x: x["delta_downloads"], reverse=True)
    top_version_loaders = top_version_loaders[:50]
    print(f"  Top version+loader: {len(top_version_loaders)} (delta > 0)")

    # ── Distribution metrics ──────────────────────────────────────
    downloads_list = [p.get("downloads", 0) for p in current_projects]
    sorted_asc = sorted(downloads_list)
    n = len(sorted_asc)
    total = sum(downloads_list)
    mean = total / n if n > 0 else 0.0

    distribution = {
        "mean": round(mean, 2),
        "median": round(percentile(sorted_asc, 50), 2),
        "std_dev": round(population_std_dev(downloads_list, mean), 2),
        "min": sorted_asc[0] if n > 0 else 0,
        "max": sorted_asc[-1] if n > 0 else 0,
        "p25": round(percentile(sorted_asc, 25), 2),
        "p50": round(percentile(sorted_asc, 50), 2),
        "p75": round(percentile(sorted_asc, 75), 2),
        "p90": round(percentile(sorted_asc, 90), 2),
        "p95": round(percentile(sorted_asc, 95), 2),
        "p99": round(percentile(sorted_asc, 99), 2),
        "gini_coefficient": round(gini_coefficient(downloads_list), 4),
    }
    print(f"  Distribution: mean={distribution['mean']}, gini={distribution['gini_coefficient']}")

    # ── Market concentration ──────────────────────────────────────
    sorted_desc = sorted(downloads_list, reverse=True)
    if total > 0:
        hhi_val = sum((d / total * 100) ** 2 for d in downloads_list)
        cr4 = sum(sorted_desc[:4]) / total * 100
        cr10 = sum(sorted_desc[:10]) / total * 100
        top10pct_count = max(1, math.ceil(n * 0.10))
        top1pct_count = max(1, math.ceil(n * 0.01))
        top_10pct_share = sum(sorted_desc[:top10pct_count]) / total * 100
        top_1pct_share = sum(sorted_desc[:top1pct_count]) / total * 100
    else:
        hhi_val = cr4 = cr10 = top_10pct_share = top_1pct_share = 0.0

    concentration = {
        "hhi": round(hhi_val, 2),
        "cr4": round(cr4, 2),
        "cr10": round(cr10, 2),
        "top_10pct_share": round(top_10pct_share, 2),
        "top_1pct_share": round(top_1pct_share, 2),
    }
    print(f"  Concentration: HHI={concentration['hhi']}, CR4={concentration['cr4']}%")

    # ── Category × Loader combinations ────────────────────────────
    # Compute for all types (used by recommendations), but only output for mod.
    combo_stats = {}  # (cat, loader) -> {"projects": set, "current": 0, "baseline": 0}
    for p in current_projects:
        p_cats = set(p.get("categories", []))
        p_content_cats = p_cats & content_cat_names
        p_loaders = p_cats & loader_set
        if not p_content_cats or not p_loaders:
            continue
        pid = p["project_id"]
        cur_dl = p.get("downloads", 0)
        base_dl = baseline_map.get(pid, 0)
        for cat in p_content_cats:
            for loader in p_loaders:
                key = (cat, loader)
                stat = combo_stats.get(key)
                if stat is None:
                    stat = {"projects": set(), "current": 0, "baseline": 0}
                    combo_stats[key] = stat
                stat["projects"].add(pid)
                stat["current"] += cur_dl
                stat["baseline"] += base_dl

    category_loader_combos = []
    if project_type == "mod":
        for (cat, loader), stat in combo_stats.items():
            category_loader_combos.append({
                "category": cat,
                "loader": loader,
                "projects": len(stat["projects"]),
                "total_downloads": stat["current"],
                "delta_downloads": stat["current"] - stat["baseline"],
            })
        category_loader_combos.sort(key=lambda x: x["total_downloads"], reverse=True)
        category_loader_combos = category_loader_combos[:20]
        print(f"  Category x Loader combos: {len(category_loader_combos)} (top 20)")

    # ── Recommendations ───────────────────────────────────────────
    # Best loader per category (by total downloads in that combo)
    best_loader_per_cat = {}
    for (cat, loader), stat in combo_stats.items():
        cur = stat["current"]
        if cat not in best_loader_per_cat or cur > best_loader_per_cat[cat][1]:
            best_loader_per_cat[cat] = (loader, cur)

    # Global fallback loader (most popular by total downloads)
    fallback_loader = loader_rankings[0]["loader"] if loader_rankings and loader_rankings[0]["total_downloads"] > 0 else "fabric"

    recommendations = []
    for cat, cs in category_stats.items():
        avg = cs["avg_downloads"]
        delta = cs["new_downloads"]
        projects = cs["projects"]
        opp_score = (avg ** 0.7) * (max(delta, 1) ** 0.3) / max(projects ** 0.5, 1)
        suggested = best_loader_per_cat.get(cat, (fallback_loader, 0))[0]
        recommendations.append({
            "category": cat,
            "suggested_loader": suggested,
            "opportunity_score": round(opp_score, 1),
            "reasoning": (
                f"{cat.title()} has {delta:+,} new downloads since baseline with {projects} "
                f"projects and avg {avg:,.0f} downloads. Suggested loader: {suggested}."
            ),
            "expected_downloads": int(avg),
        })
    recommendations.sort(key=lambda x: x["opportunity_score"], reverse=True)
    print(f"  Recommendations: {len(recommendations)}")

    db.close()

    # ── Assemble final analysis ───────────────────────────────────
    timestamp = get_timestamp()
    analysis = {
        "timestamp": timestamp,
        "date": current_date,
        "project_type": project_type,
        "baseline_date": baseline_date,
        "summary": summary,
        "trend": trend,
        "category_rankings": category_rankings,
        "loader_rankings": loader_rankings,
        "top_projects": top_projects,
        "top_version_loaders": top_version_loaders,
        "distribution": distribution,
        "concentration": concentration,
        "category_loader_combos": category_loader_combos,
        "recommendations": recommendations,
    }

    # Save timestamped analysis
    analysis_dir = get_analysis_dir(project_type)
    ensure_dir(analysis_dir)
    analysis_path = f"{analysis_dir}/{timestamp}.json"
    save_json(analysis_path, analysis)
    print(f"Saved analysis to {analysis_path}")

    # Save latest analysis (for the app)
    type_dir = get_project_type_dir(project_type)
    latest_path = f"{type_dir}/latest_analysis.json"
    save_json(latest_path, analysis)
    print(f"Saved latest analysis to {latest_path}")

    print(f"=== Analyze ({project_type}) complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
