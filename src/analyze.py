#!/usr/bin/env python3
"""
Phase 5: Analyze — Baseline-Driven Market Intelligence

All analysis is based on deltas from the baseline (first captured data).
This gives us:
  - Which MODS gained the most downloads since tracking began
  - Which VERSION+LOADER combos gained the most downloads
  - Which CATEGORIES are booming
  - Which LOADERS are gaining market share

Outputs:
  - reports/daily_report_{date}.md  — human-readable markdown report
  - reports/latest_analysis.json    — structured JSON for the app
  - reports/latest_summary.json     — same data, different key layout
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

from utils import load_json, save_json, ensure_dir, get_current_date
from db import Database


# ── Scoring constants ──────────────────────────────────────────────
COMPETITION_PENALTY_WEIGHT = 0.5
MIN_PROJECTS_THRESHOLD = 5

# Loader categories that should be excluded from category_rankings
# (they are tracked separately in loader_rankings)
LOADER_CATEGORIES = {"fabric", "forge", "neoforge", "quilt", "liteloader", "rift"}


# ═══════════════════════════════════════════════════════════════════
#  DELTA COMPUTATION
# ═══════════════════════════════════════════════════════════════════


def compute_project_deltas(db, today):
    """Compute download deltas from baseline for every project.
    Returns list of {project_id, title, slug, categories, baseline_downloads,
                      current_downloads, delta_downloads, delta_follows}."""
    baseline = db.get_baseline_project_snapshots()
    current = db.get_latest_project_snapshots(today)

    if not baseline:
        print("  No baseline data — this is likely the first run, skipping delta computation")
        return []

    rows = []
    for pid, current_dl in current.items():
        baseline_dl = baseline.get(pid, 0)
        delta = current_dl - baseline_dl
        if delta > 0:
            project = db.get_project(pid)
            title = project["title"] if project else pid
            slug = project["slug"] if project else ""
            try:
                cats = json.loads(project.get("categories", "[]")) if project else []
            except (json.JSONDecodeError, TypeError):
                cats = []
            rows.append({
                "project_id": pid,
                "title": title,
                "slug": slug,
                "categories": cats,
                "baseline_downloads": baseline_dl,
                "current_downloads": current_dl,
                "delta_downloads": delta,
            })

    rows.sort(key=lambda r: r["delta_downloads"], reverse=True)
    return rows


def compute_version_deltas(db, today):
    """Compute download deltas from baseline for every version.
    Returns list of {version_id, project_id, version_number, loaders,
                      game_versions, baseline_downloads, current_downloads,
                      delta_downloads}."""
    baseline = db.get_baseline_version_snapshots()
    cursor = db.conn.execute(
        "SELECT id, project_id, version_number, loaders, game_versions, downloads FROM versions"
    )
    current = {row["id"]: dict(row) for row in cursor.fetchall()}

    if not baseline:
        print("  No baseline version data — first run, skipping")
        return []

    rows = []
    for vid, vdata in current.items():
        baseline_dl = baseline.get(vid, 0)
        delta = vdata["downloads"] - baseline_dl
        if delta > 0:
            try:
                loaders = json.loads(vdata["loaders"]) if isinstance(vdata["loaders"], str) else (vdata["loaders"] or [])
            except (json.JSONDecodeError, TypeError):
                loaders = []
            try:
                game_versions = json.loads(vdata["game_versions"]) if isinstance(vdata["game_versions"], str) else (vdata["game_versions"] or [])
            except (json.JSONDecodeError, TypeError):
                game_versions = []
            rows.append({
                "version_id": vid,
                "project_id": vdata["project_id"],
                "version_number": vdata["version_number"],
                "loaders": loaders,
                "game_versions": game_versions,
                "baseline_downloads": baseline_dl,
                "current_downloads": vdata["downloads"],
                "delta_downloads": delta,
            })

    rows.sort(key=lambda r: r["delta_downloads"], reverse=True)
    return rows


def compute_loader_deltas(version_deltas):
    """Aggregate version deltas by loader.
    Returns list of {loader, projects, total_delta, avg_delta}."""
    loader_data = defaultdict(lambda: {"total_delta": 0, "project_ids": set()})
    for vd in version_deltas:
        for loader in vd["loaders"]:
            loader_data[loader]["total_delta"] += vd["delta_downloads"]
            loader_data[loader]["project_ids"].add(vd["project_id"])

    rows = []
    for loader, data in loader_data.items():
        rows.append({
            "loader": loader,
            "projects": len(data["project_ids"]),
            "total_delta": data["total_delta"],
            "avg_delta": data["total_delta"] // max(len(data["project_ids"]), 1),
        })
    rows.sort(key=lambda r: r["total_delta"], reverse=True)
    return rows


def compute_loader_version_combos(version_deltas):
    """Aggregate version deltas by (game_version, loader) combo.
    Returns list of {game_version, loader, projects, total_delta, avg_delta}."""
    combo_data = defaultdict(lambda: {"total_delta": 0, "project_ids": set()})
    for vd in version_deltas:
        for loader in vd["loaders"]:
            for gv in vd["game_versions"]:
                key = (gv, loader)
                combo_data[key]["total_delta"] += vd["delta_downloads"]
                combo_data[key]["project_ids"].add(vd["project_id"])

    rows = []
    for (gv, loader), data in combo_data.items():
        rows.append({
            "game_version": gv,
            "loader": loader,
            "projects": len(data["project_ids"]),
            "total_delta": data["total_delta"],
            "avg_delta": data["total_delta"] // max(len(data["project_ids"]), 1),
        })
    rows.sort(key=lambda r: r["total_delta"], reverse=True)
    return rows


# ═══════════════════════════════════════════════════════════════════
#  CATEGORY ANALYSIS (baseline-based)
# ═══════════════════════════════════════════════════════════════════


def compute_category_deltas(db, today):
    """Compute category deltas from baseline.
    Returns list of {category, baseline_downloads, current_downloads,
                      delta_downloads, baseline_projects, current_projects,
                      baseline_avg, current_avg, delta_pct}."""
    baseline_cats = db.get_baseline_category_stats()
    current_cats = db.get_categories_for_date(today)

    rows = []
    for cc in current_cats:
        cat_name = cc["category"]
        bc = baseline_cats.get(cat_name, {})
        baseline_dl = bc.get("total_downloads", 0)
        delta = cc["total_downloads"] - baseline_dl
        delta_pct = (delta / max(baseline_dl, 1)) * 100 if baseline_dl > 0 else 0
        rows.append({
            "category": cat_name,
            "baseline_downloads": baseline_dl,
            "current_downloads": cc["total_downloads"],
            "delta_downloads": delta,
            "delta_pct": round(delta_pct, 2),
            "baseline_projects": bc.get("project_count", 0),
            "current_projects": cc["project_count"],
            "baseline_avg": bc.get("avg_downloads", 0),
            "current_avg": cc["avg_downloads"],
        })

    rows.sort(key=lambda r: r["delta_downloads"], reverse=True)
    return rows


# ═══════════════════════════════════════════════════════════════════
#  RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════


def generate_recommendations(category_deltas, loader_deltas, version_deltas, project_deltas):
    """Generate actionable recommendations based on baseline deltas."""
    lines = []

    if not category_deltas:
        lines.append("_Insufficient data for recommendations. First run may still be in progress._")
        return "\n".join(lines)

    # Top growing categories
    top_cats = [c for c in category_deltas if c["current_projects"] >= MIN_PROJECTS_THRESHOLD][:5]
    if top_cats:
        lines.append("### Top Growing Categories (Since Baseline)")
        lines.append("")
        lines.append("| Category | Δ Downloads | Δ % | Projects |")
        lines.append("|----------|-------------|------|----------|")
        for c in top_cats:
            sign = "+" if c["delta_pct"] >= 0 else ""
            lines.append(
                f"| {c['category']} | {c['delta_downloads']:+,} | {sign}{c['delta_pct']}% | "
                f"{c['current_projects']} |"
            )
        lines.append("")

    # Top growing loaders
    if loader_deltas:
        lines.append("### Loader Growth (Since Baseline)")
        lines.append("")
        lines.append("| Loader | Projects | Δ Downloads | Avg / Project |")
        lines.append("|--------|----------|-------------|---------------|")
        for ld in loader_deltas[:5]:
            lines.append(
                f"| {ld['loader']} | {ld['projects']} | {ld['total_delta']:+,} | "
                f"{ld['avg_delta']:,} |"
            )
        lines.append("")

    # Top growing version+loader combos
    if version_deltas:
        lines.append("### Top Growing Version + Loader Combos")
        lines.append("")
        lines.append("| Version+Loader | Project | Δ Downloads |")
        lines.append("|----------------|---------|-------------|")
        for vd in version_deltas[:10]:
            loaders_str = ", ".join(vd["loaders"][:2])
            gv_str = ", ".join(vd["game_versions"][:2])
            label = f"{gv_str} / {loaders_str}" if gv_str else loaders_str
            project = db.get_project(vd["project_id"]) if 'db' in dir() else None
            pname = project["title"] if project else vd["project_id"][:8]
            lines.append(
                f"| {label} | {pname} | {vd['delta_downloads']:+,} |"
            )
        lines.append("")

    # Top growing projects
    if project_deltas:
        lines.append("### Top Growing Projects (Since Baseline)")
        lines.append("")
        lines.append("| Project | Category | Δ Downloads | Total Downloads |")
        lines.append("|---------|----------|-------------|----------------|")
        for pd in project_deltas[:15]:
            cat_str = ", ".join(pd["categories"][:2]) if pd["categories"] else "N/A"
            lines.append(
                f"| [{pd['title']}](https://modrinth.com/project/{pd['slug']}) | {cat_str} | "
                f"{pd['delta_downloads']:+,} | {pd['current_downloads']:,} |"
            )
        lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════


def main():
    print("=== Phase 5: Analyze — Baseline-Driven Intelligence ===")

    today = get_current_date()
    db = Database("data/modrinth_tracker.db")

    # ── Compute all deltas ────────────────────────────────────────

    project_deltas = compute_project_deltas(db, today)
    print(f"  Project deltas: {len(project_deltas)} projects with growth")

    version_deltas = compute_version_deltas(db, today)
    print(f"  Version deltas: {len(version_deltas)} versions with growth")

    loader_deltas = compute_loader_deltas(version_deltas)
    print(f"  Loader deltas:  {len(loader_deltas)} loaders")

    combo_deltas = compute_loader_version_combos(version_deltas)
    print(f"  Combo deltas:   {len(combo_deltas)} version+loader combos")

    category_deltas = compute_category_deltas(db, today)
    print(f"  Category deltas: {len(category_deltas)} categories")

    # ── Generate report ───────────────────────────────────────────

    recs = generate_recommendations(category_deltas, loader_deltas, version_deltas, project_deltas)

    # Get totals
    total_projects = len(db.get_all_projects())
    total_versions = db.conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    baseline_date = db.get_baseline_date() or today

    sections = [
        f"# Modrinth Tracker — Baseline Report",
        f"",
        f"**Report Date:** {today}",
        f"**Baseline Date:** {baseline_date}",
        f"**Total Projects:** {total_projects:,}",
        f"**Total Versions:** {total_versions:,}",
        f"**Projects with Growth:** {len(project_deltas):,}",
        f"**Versions with Growth:** {len(version_deltas):,}",
        f"",
        f"---",
        f"",
        recs if recs else "_Collecting data..._",
        f"",
        f"---",
        f"## Category Rankings by Download Growth",
        f"",
        f"| # | Category | Δ Downloads | Δ % | Projects | Avg Downloads |",
        f"|---|----------|-------------|------|----------|---------------|",
    ]
    for i, cd in enumerate(category_deltas, 1):
        sign = "+" if cd["delta_pct"] >= 0 else ""
        sections.append(
            f"| {i} | {cd['category']} | {cd['delta_downloads']:+,} | {sign}{cd['delta_pct']}% | "
            f"{cd['current_projects']} | {cd['current_avg']:,.0f} |"
        )

    sections.append("")
    sections.append("---")
    sections.append("")
    sections.append(f"_Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
    sections.append("")

    report = "\n".join(sections)

    # ── Save report ───────────────────────────────────────────────

    ensure_dir("reports")
    report_path = f"reports/daily_report_{today}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Saved report to {report_path}")

    # ── Save structured JSON for the app ──────────────────────────

    # Build category_rankings for the app's TrackerDashboard
    # Exclude loader categories (fabric, forge, etc.) — they are tracked in loader_rankings
    category_rankings = []
    for cd in category_deltas:
        if cd["category"] in LOADER_CATEGORIES:
            continue
        category_rankings.append({
            "category": cd["category"],
            "projects": cd["current_projects"],
            "total_downloads": cd["current_downloads"],
            "avg_downloads": cd["current_avg"],
            "new_downloads": cd["delta_downloads"],
            "growth_pct": cd["delta_pct"],
        })

    # Build loader_rankings
    loader_rankings = []
    for ld in loader_deltas:
        loader_rankings.append({
            "loader": ld["loader"],
            "projects": ld["projects"],
            "total_downloads": ld["total_delta"],
        })

    # Build top_projects
    top_projects = []
    for pd in project_deltas[:50]:
        top_projects.append({
            "project_id": pd["project_id"],
            "title": pd["title"],
            "slug": pd["slug"],
            "categories": pd["categories"],
            "delta_downloads": pd["delta_downloads"],
            "current_downloads": pd["current_downloads"],
        })

    # Build recommendations
    recommendations = []
    for cd in category_deltas[:10]:
        if cd["current_projects"] < MIN_PROJECTS_THRESHOLD:
            continue
        # Find best loader for this category from version deltas
        cat_loader_deltas = defaultdict(int)
        for vd in version_deltas:
            project = db.get_project(vd["project_id"])
            if project:
                try:
                    p_cats = json.loads(project.get("categories", "[]"))
                except (json.JSONDecodeError, TypeError):
                    p_cats = []
                if cd["category"] in p_cats:
                    for loader in vd["loaders"]:
                        cat_loader_deltas[loader] += vd["delta_downloads"]
        best_loader = max(cat_loader_deltas, key=cat_loader_deltas.get) if cat_loader_deltas else "fabric"
        # Opportunity score based on avg_delta * growth
        opp_score = round(
            (cd["current_avg"] ** 0.7) * (max(cd["delta_downloads"], 1) ** 0.3)
            / max(cd["current_projects"] ** COMPETITION_PENALTY_WEIGHT, 1),
            1,
        )
        recommendations.append({
            "category": cd["category"],
            "suggested_loader": best_loader,
            "opportunity_score": opp_score,
            "reasoning": (
                f"{cd['category'].title()} has {cd['delta_downloads']:+,} new downloads "
                f"({cd['delta_pct']:+.1f}%) since baseline with {cd['current_projects']} projects. "
                f"Best loader: {best_loader}."
            ),
            "expected_downloads": int(cd["current_avg"]),
        })

    # Build top version+loader growth
    top_version_loaders = []
    for vd in version_deltas[:50]:
        project = db.get_project(vd["project_id"])
        pname = project["title"] if project else vd["project_id"]
        top_version_loaders.append({
            "version_id": vd["version_id"],
            "project_id": vd["project_id"],
            "project_title": pname,
            "version_number": vd["version_number"],
            "loaders": vd["loaders"],
            "game_versions": vd["game_versions"],
            "delta_downloads": vd["delta_downloads"],
        })

    analysis = {
        "report_date": today,
        "baseline_date": baseline_date,
        "total_projects": total_projects,
        "total_versions": total_versions,
        "category_rankings": category_rankings,
        "loader_rankings": loader_rankings,
        "top_projects": top_projects,
        "top_version_loaders": top_version_loaders,
        "recommendations": recommendations,
        "trends": {
            "new_projects_24h": 0,
            "new_versions_24h": 0,
            "top_gainer": top_projects[0]["title"] if top_projects else "N/A",
            "top_gainer_downloads": top_projects[0]["delta_downloads"] if top_projects else 0,
        },
    }

    save_json("reports/latest_analysis.json", analysis)
    print("Saved structured analysis to reports/latest_analysis.json")

    # Also save latest_summary for backward compatibility
    summary = {
        "report_date": today,
        "total_projects": total_projects,
        "total_versions": total_versions,
        "category_rankings": category_rankings,
        "loader_rankings": loader_rankings,
        "top_projects": top_projects[:20],
        "top_version_loaders": top_version_loaders[:20],
        "recommendations": recommendations[:5],
        "trends": analysis["trends"],
    }
    save_json("reports/latest_summary.json", summary)
    print("Saved structured summary to reports/latest_summary.json")

    db.close()
    print("=== Analyze complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())