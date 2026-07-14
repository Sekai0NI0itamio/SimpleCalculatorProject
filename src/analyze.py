#!/usr/bin/env python3
"""
Phase 5: Analyze — Baseline-Driven Market Intelligence

All analysis is based on deltas from the baseline (first captured data).
This gives us:
  - Which PROJECTS gained the most downloads since tracking began
  - Which VERSION+LOADER combos gained the most downloads
  - Which CATEGORIES (content only — no loaders/resolutions/features) are booming
  - Which LOADERS are gaining market share

Category filtering:
  - Loads loader names from data/loaders.json (from /tag/loader API)
  - Loads category headers from data/categories.json (from /tag/category API)
  - Excludes loader names AND categories with header != "categories"
  - This properly separates content categories (adventure, utility, etc.)
    from loaders (fabric, forge, paper, bukkit, etc.) and from
    resolutions (16x, 32x) / features (atmosphere, bloom, etc.)

Outputs:
  - reports/daily_report_{date}.md  — human-readable markdown report
  - reports/latest_analysis.json    — structured JSON for the app
  - reports/latest_summary.json     — same data, different key layout
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime

from utils import load_json, save_json, ensure_dir, get_current_date
from db import Database


# ── Scoring constants ──────────────────────────────────────────────
COMPETITION_PENALTY_WEIGHT = 0.5
MIN_PROJECTS_THRESHOLD = 5

# Content category headers — only "categories" header is a real content
# category. Other headers ("resolutions", "features", "performance impact",
# "minecraft_server_*") are metadata tags, not content categories.
CONTENT_CATEGORY_HEADER = "categories"


def load_exclusion_sets():
    """Load loader names and category headers from data files.
    Returns (loader_names_set, excluded_category_names_set, content_category_names_set).

    - loader_names_set: all loader names from /tag/loader (fabric, forge, etc.)
    - excluded_category_names_set: category names that are NOT content categories
      (resolutions like 16x, features like atmosphere, etc.)
    - content_category_names_set: category names with header == "categories"
    """
    loaders = load_json("data/loaders.json") or []
    loader_set = set(loaders)

    categories = load_json("data/categories.json") or []
    excluded = set()
    content = set()
    for cat in categories:
        name = cat.get("slug") or cat.get("name", "")
        header = cat.get("header", "")
        if header == CONTENT_CATEGORY_HEADER:
            content.add(name)
        else:
            # Resolutions, features, performance impact, server tags, etc.
            excluded.add(name)

    return loader_set, excluded, content


# ═══════════════════════════════════════════════════════════════════
#  DELTA COMPUTATION
# ═══════════════════════════════════════════════════════════════════


def compute_project_deltas(db, today):
    """Compute download deltas from baseline for every project."""
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
    """Compute download deltas from baseline for every version."""
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


# ═══════════════════════════════════════════════════════════════════
#  CATEGORY + LOADER ANALYSIS (baseline-based)
# ═══════════════════════════════════════════════════════════════════


def compute_category_deltas(db, today):
    """Compute category deltas from baseline for ALL categories.
    Returns list of {category, baseline_downloads, current_downloads,
                      delta_downloads, baseline_projects, current_projects,
                      baseline_avg, current_avg, delta_pct}.

    This includes BOTH content categories AND loaders — the caller
    is responsible for splitting them based on data/loaders.json."""
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

    rows.sort(key=lambda r: r["current_downloads"], reverse=True)
    return rows


def compute_total_downloads(db):
    """Compute total downloads across ALL projects (regardless of category)."""
    cursor = db.conn.execute("SELECT SUM(downloads) as total FROM projects")
    row = cursor.fetchone()
    return row["total"] if row and row["total"] else 0


# ═══════════════════════════════════════════════════════════════════
#  RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════


def generate_recommendations(category_deltas, loader_deltas, version_deltas, project_deltas, db):
    """Generate actionable recommendations based on baseline deltas."""
    lines = []

    if not category_deltas:
        lines.append("_Insufficient data for recommendations. First run may still be in progress._")
        return "\n".join(lines)

    # Top growing content categories
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

    # Top growing loaders (loader_deltas has same structure as category_deltas,
    # with "category" being the loader name)
    if loader_deltas:
        lines.append("### Loader Growth (Since Baseline)")
        lines.append("")
        lines.append("| Loader | Projects | Δ Downloads | Avg / Project |")
        lines.append("|--------|----------|-------------|---------------|")
        for ld in loader_deltas[:5]:
            lines.append(
                f"| {ld['category']} | {ld['current_projects']} | {ld['delta_downloads']:+,} | "
                f"{ld['current_avg']:,.0f} |"
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
            project = db.get_project(vd["project_id"])
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

    # ── Load exclusion sets for filtering ─────────────────────────
    loader_set, excluded_cats, content_cats = load_exclusion_sets()
    print(f"  Loaded {len(loader_set)} loader names, {len(excluded_cats)} non-content categories, {len(content_cats)} content categories")

    # ── Compute all deltas ────────────────────────────────────────

    project_deltas = compute_project_deltas(db, today)
    print(f"  Project deltas: {len(project_deltas)} projects with growth")

    version_deltas = compute_version_deltas(db, today)
    print(f"  Version deltas: {len(version_deltas)} versions with growth")

    category_deltas = compute_category_deltas(db, today)
    print(f"  Category deltas: {len(category_deltas)} total (before filtering)")

    # Compute total downloads directly from the projects table
    total_downloads = compute_total_downloads(db)
    print(f"  Total downloads (all projects): {total_downloads:,}")

    # ── Split category deltas into content categories vs loaders ──
    # A category is a "loader" if it's in the loader_set (from /tag/loader)
    # A category is a "content category" if it's in content_cats (header == "categories")
    # Everything else (resolutions, features, server tags) is excluded from both
    content_category_deltas = []
    loader_category_deltas = []
    for cd in category_deltas:
        cat = cd["category"]
        if cat in loader_set:
            loader_category_deltas.append(cd)
        elif cat in content_cats and cat not in excluded_cats:
            content_category_deltas.append(cd)
        # else: skip (resolutions, features, minecraft_server_*, etc.)

    print(f"  Content categories: {len(content_category_deltas)}")
    print(f"  Loader categories: {len(loader_category_deltas)}")

    # ── Compute loader deltas from version data (for growth tracking) ──
    loader_version_deltas = defaultdict(lambda: {"total_delta": 0, "project_ids": set()})
    for vd in version_deltas:
        for loader in vd["loaders"]:
            loader_version_deltas[loader]["total_delta"] += vd["delta_downloads"]
            loader_version_deltas[loader]["project_ids"].add(vd["project_id"])

    # ── Generate report ───────────────────────────────────────────

    recs = generate_recommendations(content_category_deltas, loader_category_deltas, version_deltas, project_deltas, db)

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
        f"**Total Downloads:** {total_downloads:,}",
        f"**Projects with Growth:** {len(project_deltas):,}",
        f"**Versions with Growth:** {len(version_deltas):,}",
        f"",
        f"---",
        f"",
        recs if recs else "_Collecting data..._",
        f"",
        f"---",
        f"## Category Rankings by Download Growth (Content Categories Only)",
        f"",
        f"| # | Category | Δ Downloads | Δ % | Projects | Avg Downloads |",
        f"|---|----------|-------------|------|----------|---------------|",
    ]
    for i, cd in enumerate(content_category_deltas, 1):
        sign = "+" if cd["delta_pct"] >= 0 else ""
        sections.append(
            f"| {i} | {cd['category']} | {cd['delta_downloads']:+,} | {sign}{cd['delta_pct']}% | "
            f"{cd['current_projects']} | {cd['current_avg']:,.0f} |"
        )

    sections.append("")
    sections.append("---")
    sections.append("")
    sections.append(f"## Loader Rankings")
    sections.append("")
    sections.append("| # | Loader | Projects | Total Downloads | Avg Downloads | Δ Downloads |")
    sections.append("|---|--------|----------|------------------|---------------|-------------|")
    for i, ld in enumerate(loader_category_deltas, 1):
        sign = "+" if ld["delta_pct"] >= 0 else ""
        sections.append(
            f"| {i} | {ld['category']} | {ld['current_projects']} | {ld['current_downloads']:,} | "
            f"{ld['current_avg']:,.0f} | {ld['delta_downloads']:+,} |"
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

    # Build category_rankings (content categories ONLY)
    category_rankings = []
    for cd in content_category_deltas:
        category_rankings.append({
            "category": cd["category"],
            "projects": cd["current_projects"],
            "total_downloads": cd["current_downloads"],
            "avg_downloads": cd["current_avg"],
            "new_downloads": cd["delta_downloads"],
            "growth_pct": cd["delta_pct"],
        })

    # Build loader_rankings from category stats (so it shows data even on baseline day)
    loader_rankings = []
    for ld in loader_category_deltas:
        loader_rankings.append({
            "loader": ld["category"],
            "projects": ld["current_projects"],
            "total_downloads": ld["current_downloads"],
            "avg_downloads": ld["current_avg"],
            "growth_pct": ld["delta_pct"],
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

    # Build recommendations (content categories only)
    recommendations = []
    for cd in content_category_deltas[:10]:
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
        "total_downloads": total_downloads,
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
        "total_downloads": total_downloads,
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
