#!/usr/bin/env python3
"""
Phase 5: Analyze — History-based Market Intelligence

Two analysis modes:
  --mode daily   (08:00 Beijing / 00:00 UTC)
     24-hour global analysis: compares current snapshot with the snapshot
     from 24 hours ago (yesterday at the same time). Shows day-over-day
     growth, category trends, version+loader growth, recommendations.

  --mode hourly  (every 2 hours on even UTC hours)
     2-hour delta analysis: compares current snapshot with the snapshot
     from 2 hours ago. Shows short-term velocity, top movers, and
     projected daily totals.

All analysis is derived from the raw snapshot history stored in
data/{project_type}/raw/. The raw snapshot is the single source of truth.

Outputs:
  - data/{project_type}/analysis/{timestamp}.json  — full analysis
  - data/{project_type}/latest_analysis.json       — same content (for the app)
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
from opportunity import build_opportunity_analysis

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
    """Gini coefficient (0=equal, 1=unequal)."""
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


# ═══════════════════════════════════════════════════════════════════
#  SNAPSHOT LOADING
# ═══════════════════════════════════════════════════════════════════


def parse_snapshot_timestamp(snapshot):
    """Parse a snapshot's timestamp into a datetime object (Beijing time).
    Handles format: '2026-07-15T09-18-56' (date T hour-min-sec)
    """
    ts_str = snapshot.get("timestamp", "")
    if not ts_str:
        return None
    # Replace all '-' after the 'T' with ':' to get ISO-like format
    # Result: '2026-07-15T09:18:56'
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
    """Find the snapshot closest to `hours_back` hours before the current snapshot.
    Returns the snapshot or None if none found within a reasonable window.
    """
    current_ts = parse_snapshot_timestamp(current_snapshot)
    if not current_ts:
        return None
    target = current_ts - timedelta(hours=hours_back)
    # Find the snapshot closest to target (within ±50% of hours_back)
    window = timedelta(hours=hours_back * 0.5)
    best = None
    best_diff = None
    for snap in snapshots:
        st = parse_snapshot_timestamp(snap)
        if not st:
            continue
        if st >= current_ts:
            continue  # must be before current
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
#  CORE ANALYSIS (shared by daily and hourly modes)
# ═══════════════════════════════════════════════════════════════════


def build_project_analysis(current_snapshot, baseline_snapshot,
                           project_type, loader_names, loader_set, content_cat_names):
    """Build the full analysis for a given baseline/current pair.
    Returns a dict with all analysis sections.
    """
    current_projects = current_snapshot.get("projects", [])
    current_total_downloads = current_snapshot.get("total_downloads", 0)
    baseline_total_downloads = baseline_snapshot.get("total_downloads", 0)
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

    total_versions = len(current_versions)

    summary = {
        "total_projects": current_snapshot.get("project_count", len(current_projects)),
        "total_versions": total_versions,
        "total_downloads": current_total_downloads,
        "baseline_date": baseline_date,
        "current_date": current_date,
        "new_projects_since_baseline": sum(1 for pid in current_map if pid not in baseline_map),
        "new_downloads_since_baseline": new_downloads,
    }

    # ── Trend (all snapshots for time series) ──────────────────────
    snapshots = load_all_snapshots(project_type)
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

    # ── Category rankings ─────────────────────────────────────────
    cat_projects = {}
    for p in current_projects:
        for cat in p.get("categories", []):
            if cat in content_cat_names:
                cat_projects.setdefault(cat, []).append(p)

    category_rankings = []
    category_stats = {}
    for cat, projs in cat_projects.items():
        current_total = sum(p.get("downloads", 0) for p in projs)
        baseline_total = sum(baseline_map.get(p["project_id"], 0) for p in projs)
        new_dl = current_total - baseline_total
        project_count = len(projs)
        avg_downloads = current_total / project_count if project_count > 0 else 0.0
        growth_pct = (new_dl / baseline_total * 100) if baseline_total > 0 else 0.0
        market_share = (current_total / current_total_downloads * 100) if current_total_downloads > 0 else 0.0
        category_rankings.append({
            "category": cat,
            "projects": project_count,
            "total_downloads": current_total,
            "avg_downloads": round(avg_downloads, 2),
            "new_downloads": new_dl,
            "growth_pct": round(growth_pct, 2),
            "market_share": round(market_share, 2),
        })
        category_stats[cat] = {
            "avg_downloads": avg_downloads,
            "new_downloads": new_dl,
            "projects": project_count,
            "total_downloads": current_total,
        }
    category_rankings.sort(key=lambda x: x["total_downloads"], reverse=True)

    # ── Loader rankings ───────────────────────────────────────────
    loader_rankings = []
    for loader in loader_names:
        projs = [p for p in current_projects if loader in p.get("categories", [])]
        if not projs:
            loader_rankings.append({
                "loader": loader, "projects": 0, "total_downloads": 0,
                "avg_downloads": 0.0, "growth_pct": 0.0, "market_share": 0.0,
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

    # ── Top projects ──────────────────────────────────────────────
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
            "client_side": p.get("client_side", "unknown"),
            "server_side": p.get("server_side", "unknown"),
            "baseline_downloads": baseline_dl,
            "current_downloads": current_dl,
            "delta_downloads": delta,
            "growth_pct": round(growth_pct, 2),
        })
    top_projects.sort(key=lambda x: x["delta_downloads"], reverse=True)
    top_projects = top_projects[:100]

    # ── Top version+loader growth ─────────────────────────────────
    project_title_map = {p["project_id"]: p.get("title", "") for p in current_projects}
    top_version_loaders = []
    for v in current_versions:
        vid = v.get("version_id")
        if not vid:
            continue
        current_dl = v.get("downloads", 0) or 0
        baseline_dl = baseline_version_map.get(vid, 0)
        delta = current_dl - baseline_dl
        if delta > 0:
            pid = v.get("project_id", "")
            top_version_loaders.append({
                "version_id": vid,
                "project_id": pid,
                "project_title": project_title_map.get(pid, pid),
                "version_number": v.get("version_number", ""),
                "loaders": v.get("loaders", []) or [],
                "game_versions": v.get("game_versions", []) or [],
                "delta_downloads": delta,
            })
    top_version_loaders.sort(key=lambda x: x["delta_downloads"], reverse=True)
    top_version_loaders = top_version_loaders[:50]

    # ── Distribution ──────────────────────────────────────────────
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

    # ── Concentration ─────────────────────────────────────────────
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

    # ── Category × Loader combos ──────────────────────────────────
    combo_stats = {}
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

    # ── Recommendations ───────────────────────────────────────────
    best_loader_per_cat = {}
    for (cat, loader), stat in combo_stats.items():
        cur = stat["current"]
        if cat not in best_loader_per_cat or cur > best_loader_per_cat[cat][1]:
            best_loader_per_cat[cat] = (loader, cur)

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

    return {
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


# ═══════════════════════════════════════════════════════════════════
#  TRENDING ANALYSIS — Composite scoring system
# ═══════════════════════════════════════════════════════════════════
#
#  The trending score balances multiple factors to identify projects
#  that are genuinely "trending" — not just the biggest projects
#  getting their usual daily downloads.
#
#  Formula:
#    RGR  = Relative Growth Rate = (delta / baseline) * 100  (%)
#    AGS  = Absolute Growth Score = log(1 + delta) * 10
#    SF   = Size Factor = 1 / (1 + log(1 + baseline / 1000))
#           (smaller projects get a higher score)
#    VEL  = Velocity = delta / hours_between (downloads per hour)
#
#    TRENDING_SCORE = RGR * 0.35 + AGS * 0.25 + SF * 30 * 0.20 + VEL * 0.20
#
#  Example:
#    Sodium (65M downloads, +1000/day):  trending_score ≈ 26
#    CoolProject (1K downloads, +500/day): trending_score ≈ 54
#    → CoolProject is correctly identified as more "trending"
#
#  We produce BOTH simple (absolute delta) and advanced (trending score)
#  rankings so you can compare them side by side.
# ═══════════════════════════════════════════════════════════════════


def compute_trending_score(baseline_downloads, delta, hours_between):
    """Compute a composite trending score for a single project.

    Parameters:
      baseline_downloads: downloads at the baseline snapshot
      delta: downloads gained since baseline
      hours_between: hours elapsed between baseline and current

    Returns a dict with all component scores and the final trending_score.
    """
    # ── Relative Growth Rate (RGR) ────────────────────────────────
    if baseline_downloads > 0:
        rgr = (delta / baseline_downloads) * 100
    else:
        rgr = 100.0 if delta > 0 else 0.0

    # ── Absolute Growth Score (AGS) ───────────────────────────────
    ags = math.log(1 + abs(delta)) * 10

    # ── Size Factor (SF) ──────────────────────────────────────────
    # Smaller projects get a higher factor. For a project with 1K downloads,
    # SF ≈ 0.59. For a project with 65M, SF ≈ 0.08.
    size_factor = 1.0 / (1.0 + math.log(1 + max(baseline_downloads, 0) / 1000.0))

    # ── Velocity (VEL) ────────────────────────────────────────────
    velocity = delta / max(hours_between, 1)

    # ── Composite Trending Score ──────────────────────────────────
    trending_score = (
        rgr * 0.35 +
        ags * 0.25 +
        size_factor * 30 * 0.20 +
        velocity * 0.20
    )

    return {
        "trending_score": round(trending_score, 2),
        "components": {
            "relative_growth_pct": round(rgr, 4),
            "absolute_growth_score": round(ags, 2),
            "size_factor": round(size_factor, 4),
            "velocity": round(velocity, 2),
        },
        "weights": {
            "relative_growth": 0.35,
            "absolute_growth": 0.25,
            "size_factor": 0.20,
            "velocity": 0.20,
        },
    }


def build_trending_analysis(top_projects, hours_between, current_snapshot, baseline_snapshot):
    """Build trending analysis with both simple and advanced rankings.

    Returns a dict with:
      - formula: explanation of the trending score formula
      - simple_ranking: top 50 by absolute delta (downloads gained)
      - advanced_ranking: top 50 by trending_score (composite)
      - rising_stars: top 20 small projects (<5000 downloads) with high relative growth
      - velocity_ranking: top 30 by downloads per hour
      - momentum_ranking: top 30 by trending_score * velocity
    """
    # ── Compute trending scores for all projects ──────────────────
    scored_projects = []
    for p in top_projects:
        baseline = p.get("baseline_downloads", 0)
        delta = p.get("delta_downloads", 0)
        if delta <= 0:
            continue  # skip projects with no growth

        ts = compute_trending_score(baseline, delta, hours_between)
        p["trending_score"] = ts["trending_score"]
        p["trending_components"] = ts["components"]
        p["velocity"] = ts["components"]["velocity"]
        scored_projects.append(p)

    # ── Simple Ranking (absolute delta) ───────────────────────────
    simple_ranking = sorted(scored_projects, key=lambda x: x["delta_downloads"], reverse=True)[:50]

    # ── Advanced Ranking (trending score) ─────────────────────────
    advanced_ranking = sorted(scored_projects, key=lambda x: x["trending_score"], reverse=True)[:50]

    # ── Rising Stars (small projects with high relative growth) ───
    # Projects with baseline < 5000 downloads and growth > 10%
    rising_stars = [
        p for p in scored_projects
        if p["baseline_downloads"] < 5000
        and p["trending_components"]["relative_growth_pct"] > 10
    ]
    rising_stars.sort(key=lambda x: x["trending_score"], reverse=True)
    rising_stars = rising_stars[:20]

    # ── Velocity Ranking (downloads per hour) ─────────────────────
    velocity_ranking = sorted(scored_projects, key=lambda x: x["velocity"], reverse=True)[:30]

    # ── Momentum Ranking (trending_score * velocity) ──────────────
    for p in scored_projects:
        p["momentum"] = round(p["trending_score"] * p["velocity"], 2)
    momentum_ranking = sorted(scored_projects, key=lambda x: x["momentum"], reverse=True)[:30]

    # ── Summary stats ─────────────────────────────────────────────
    all_scores = [p["trending_score"] for p in scored_projects]
    all_velocities = [p["velocity"] for p in scored_projects]
    all_rgrs = [p["trending_components"]["relative_growth_pct"] for p in scored_projects]

    stats = {
        "projects_scored": len(scored_projects),
        "avg_trending_score": round(sum(all_scores) / len(all_scores), 2) if all_scores else 0,
        "median_trending_score": round(percentile(sorted(all_scores), 50), 2) if all_scores else 0,
        "avg_velocity": round(sum(all_velocities) / len(all_velocities), 2) if all_velocities else 0,
        "avg_relative_growth": round(sum(all_rgrs) / len(all_rgrs), 4) if all_rgrs else 0,
    }

    return {
        "formula": {
            "description": (
                "Composite trending score that balances relative growth (percentage), "
                "absolute growth (log of delta), project size (smaller = higher), "
                "and velocity (downloads per hour)."
            ),
            "equation": "RGR*0.35 + AGS*0.25 + SF*30*0.20 + VEL*0.20",
            "components": {
                "RGR": "Relative Growth Rate = (delta / baseline) * 100 (%)",
                "AGS": "Absolute Growth Score = log(1 + delta) * 10",
                "SF": "Size Factor = 1 / (1 + log(1 + baseline / 1000))",
                "VEL": "Velocity = delta / hours_between",
            },
            "weights": {
                "relative_growth": 0.35,
                "absolute_growth": 0.25,
                "size_factor": 0.20,
                "velocity": 0.20,
            },
        },
        "stats": stats,
        "simple_ranking": simple_ranking,
        "advanced_ranking": advanced_ranking,
        "rising_stars": rising_stars,
        "velocity_ranking": velocity_ranking,
        "momentum_ranking": momentum_ranking,
    }


# ═══════════════════════════════════════════════════════════════════
#  HOURLY MODE: 2-hour velocity + predictions
# ═══════════════════════════════════════════════════════════════════


def build_hourly_extras(analysis, current_snapshot, baseline_snapshot):
    """Add hourly-specific metrics: velocity, acceleration, predictions."""
    current_projects = current_snapshot.get("projects", [])
    hours = 2.0  # always 2-hour window

    # Velocity: downloads per hour for top projects
    top_movers = []
    for p in analysis["top_projects"][:30]:
        top_movers.append({
            "project_id": p["project_id"],
            "title": p["title"],
            "slug": p["slug"],
            "delta_downloads": p["delta_downloads"],
            "downloads_per_hour": round(p["delta_downloads"] / hours, 1),
            "growth_pct": p["growth_pct"],
        })

    # Velocity by category
    velocity_by_category = []
    for cat in analysis["category_rankings"]:
        velocity_by_category.append({
            "category": cat["category"],
            "new_downloads": cat["new_downloads"],
            "downloads_per_hour": round(cat["new_downloads"] / hours, 1),
            "growth_pct": cat["growth_pct"],
        })
    velocity_by_category.sort(key=lambda x: x["downloads_per_hour"], reverse=True)

    # Total velocity
    total_delta = analysis["summary"]["new_downloads_since_baseline"]
    velocity = {
        "hours": hours,
        "total_delta": total_delta,
        "downloads_per_hour": round(total_delta / hours, 1),
        "predicted_daily_total": round(total_delta * (24.0 / hours)),
        "predicted_daily_growth": round(total_delta * (24.0 / hours)),
        "confidence": "low" if hours < 6 else "medium",
    }

    # Top growing categories (fastest velocity)
    velocity_by_category = velocity_by_category[:10]

    return {
        "velocity": velocity,
        "top_movers": top_movers,
        "velocity_by_category": velocity_by_category,
        "hourly_analysis": True,
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
    mode_label = f"24h Daily" if mode == "daily" else f"2h Hourly"

    print(f"=== Phase 5: Analyze ({project_type}) — {mode_label} ===")

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
        # Fallback: use the oldest snapshot as baseline
        baseline_snapshot = snapshots[0]
        print(f"  No {hours_back}h baseline found — using oldest snapshot ({baseline_snapshot.get('date', '?')}) as fallback")
    else:
        baseline_ts = parse_snapshot_timestamp(baseline_snapshot)
        diff_hours = abs((current_ts - baseline_ts).total_seconds() / 3600) if current_ts and baseline_ts else hours_back
        print(f"  Baseline snapshot: {baseline_snapshot.get('date', '?')} ({baseline_ts}, ~{diff_hours:.1f}h ago)")

    # ── Load filter sets ──────────────────────────────────────────
    loader_names, loader_set, content_cat_names = load_filter_sets(project_type)
    print(f"  Loaders: {len(loader_names)}, content categories: {len(content_cat_names)}")

    # ── Build core analysis ───────────────────────────────────────
    analysis_data = build_project_analysis(
        current_snapshot, baseline_snapshot,
        project_type, loader_names, loader_set, content_cat_names
    )

    print(f"  Summary: {analysis_data['summary']['total_projects']:,} projects, "
          f"{analysis_data['summary']['total_versions']:,} versions, "
          f"{analysis_data['summary']['total_downloads']:,} downloads")
    print(f"  New downloads: {analysis_data['summary']['new_downloads_since_baseline']:+,}")

    # ── Trending Analysis (both simple and advanced) ──────────────
    # Compute the actual hours between current and baseline snapshots
    actual_hours = hours_back
    if current_ts and baseline_snapshot:
        baseline_ts = parse_snapshot_timestamp(baseline_snapshot)
        if baseline_ts:
            actual_hours = abs((current_ts - baseline_ts).total_seconds() / 3600)
    trending_analysis = build_trending_analysis(
        analysis_data["top_projects"], actual_hours,
        current_snapshot, baseline_snapshot
    )
    print(f"  Trending: {trending_analysis['stats']['projects_scored']:,} projects scored, "
          f"avg trending score: {trending_analysis['stats']['avg_trending_score']:.2f}, "
          f"rising stars: {len(trending_analysis['rising_stars'])}")

    # ── Hourly extras ─────────────────────────────────────────────
    if mode == "hourly":
        hourly_extras = build_hourly_extras(analysis_data, current_snapshot, baseline_snapshot)
        print(f"  Velocity: {hourly_extras['velocity']['downloads_per_hour']:,.1f}/hr, "
              f"predicted daily: {hourly_extras['velocity']['predicted_daily_total']:+,}")
    else:
        hourly_extras = {}

    # ── Opportunity Analysis (Decision Engine) ─────────────────────
    # Only run on daily mode — the decision engine needs enough data to be meaningful.
    # Running on 2-hour data would produce noisy/volatile recommendations.
    if mode == "daily":
        opportunity_analysis = build_opportunity_analysis(
            current_snapshot, baseline_snapshot, actual_hours,
            loader_names, loader_set
        )
        print(f"  Opportunity: {len(opportunity_analysis.get('opportunities', []))} opportunities, "
              f"{len(opportunity_analysis.get('emerging_concepts', []))} emerging concepts")
    else:
        opportunity_analysis = {"note": "opportunity_analysis_only_on_daily_mode"}

    # ── Assemble final analysis ───────────────────────────────────
    timestamp = get_timestamp()
    analysis = {
        "timestamp": timestamp,
        "date": current_date,
        "project_type": project_type,
        "analysis_type": mode,
        "baseline_date": baseline_snapshot.get("date", ""),
        "hours_between": hours_back,
        **analysis_data,
        "trending_analysis": trending_analysis,
        "opportunity_analysis": opportunity_analysis,
        **hourly_extras,
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

    print(f"=== Analyze ({project_type}) {mode} complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())