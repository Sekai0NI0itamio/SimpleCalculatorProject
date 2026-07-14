#!/usr/bin/env python3
"""
Phase 5: Analyze — Market Intelligence Engine

Generates actionable reports that tell you:
  - Which mod categories have the most downloads
  - Which niches have high demand but low competition (opportunities)
  - Which category+loader combinations perform best
  - Which markets are saturated vs under-served
  - Growth trends over time
  - Concrete recommendations on what to build next

Outputs markdown reports to reports/ and structured JSON to reports/.
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from utils import load_json, save_json, ensure_dir, get_current_date
from db import Database


# ── Scoring constants ──────────────────────────────────────────────
# How much to penalize competition. Higher = more penalty for crowded categories.
COMPETITION_PENALTY_WEIGHT = 0.5
# How much to weight recent growth vs total downloads
GROWTH_WEIGHT = 0.3
# Minimum projects to consider a category viable
MIN_PROJECTS_THRESHOLD = 5


# ═══════════════════════════════════════════════════════════════════
#  OPPORTUNITY ANALYSIS
# ═══════════════════════════════════════════════════════════════════


def compute_opportunity_score(
    avg_downloads: float, project_count: int, new_downloads: int
) -> float:
    """Score how good an opportunity a category is.

    Formula: opportunity = (avg_downloads^0.7 * new_downloads^0.3) / project_count^COMPETITION_PENALTY_WEIGHT

    High avg downloads + high new downloads + low competition = best score.
    """
    if project_count == 0:
        return 0.0
    demand = (avg_downloads**0.7) * (max(new_downloads, 1) ** 0.3)
    competition = project_count**COMPETITION_PENALTY_WEIGHT
    return demand / competition


def generate_opportunity_matrix(db, today):
    """Rank categories by opportunity score — finds the best niches to build in."""
    categories = db.get_categories_for_date(today)
    if not categories:
        return "No category data available.", []

    # Get previous date for growth context
    prev_cursor = db.conn.execute(
        "SELECT DISTINCT date FROM daily_category_stats WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today,),
    )
    prev_row = prev_cursor.fetchone()
    prev_date = prev_row["date"] if prev_row else None
    prev_categories = {}
    if prev_date:
        prev_cats = db.get_categories_for_date(prev_date)
        prev_categories = {c["category"]: c for c in prev_cats}

    # Build opportunity matrix
    rows = []
    for cat in categories:
        name = cat["category"]
        proj_count = cat["project_count"]
        total_dl = cat["total_downloads"]
        avg_dl = cat["avg_downloads"]
        new_dl = cat["total_new_downloads"]

        if proj_count < MIN_PROJECTS_THRESHOLD:
            continue

        score = compute_opportunity_score(avg_dl, proj_count, new_dl)

        prev = prev_categories.get(name, {})
        prev_avg = prev.get("avg_downloads", 0)
        avg_change_pct = ((avg_dl - prev_avg) / prev_avg * 100) if prev_avg > 0 else 0

        rows.append(
            {
                "category": name,
                "projects": proj_count,
                "total_downloads": total_dl,
                "avg_downloads": avg_dl,
                "new_downloads": new_dl,
                "opportunity_score": score,
                "avg_change_pct": avg_change_pct,
            }
        )

    rows.sort(key=lambda r: r["opportunity_score"], reverse=True)

    lines = [
        "## Opportunity Matrix — Best Niches to Build In",
        "",
        "_Categories sorted by opportunity score. High avg downloads + low competition = best opportunity._",
        "",
        "| Category | Projects | Avg Downloads | New Downloads Today | Opportunity Score | Avg Δ% |",
        "|----------|----------|--------------|-------------------|-----------------|--------|",
    ]
    for r in rows[:25]:
        lines.append(
            f"| {r['category']} | {r['projects']} | {r['avg_downloads']:,.0f} | "
            f"{r['new_downloads']:,} | {r['opportunity_score']:,.1f} | "
            f"{r['avg_change_pct']:+.1f}% |"
        )

    return "\n".join(lines), rows


# ═══════════════════════════════════════════════════════════════════
#  CATEGORY + LOADER CROSS-ANALYSIS
# ═══════════════════════════════════════════════════════════════════


def generate_category_loader_analysis(db):
    """Cross-reference categories with loaders to find best combos."""
    cursor = db.conn.execute("""
        SELECT p.project_id, p.categories, p.downloads, v.loaders
        FROM projects p
        JOIN versions v ON p.project_id = v.project_id
        WHERE p.categories IS NOT NULL AND v.loaders IS NOT NULL
    """)

    # Aggregate: (category, loader) -> {total_dl, count}
    combo_stats = defaultdict(lambda: {"total_downloads": 0, "count": 0})

    for row in cursor.fetchall():
        try:
            cats = json.loads(row["categories"])
            loaders = json.loads(row["loaders"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cats, list) or not isinstance(loaders, list):
            continue
        downloads = row["downloads"] or 0
        for cat in cats:
            for loader in loaders:
                key = (cat, loader)
                combo_stats[key]["total_downloads"] += downloads
                combo_stats[key]["count"] += 1

    if not combo_stats:
        return "No category+loader data available (run version fetch first).", []

    sorted_combos = sorted(
        combo_stats.items(), key=lambda x: x[1]["total_downloads"], reverse=True
    )

    lines = [
        "## Category + Loader Analysis",
        "",
        "_Which (category, loader) combinations have the most downloads._",
        "",
        "| Category | Loader | Projects | Total Downloads | Avg Downloads / Project |",
        "|----------|--------|---------|----------------|------------------------|",
    ]
    for (cat, loader), stats in sorted_combos[:30]:
        avg = stats["total_downloads"] / stats["count"] if stats["count"] > 0 else 0
        lines.append(
            f"| {cat} | {loader} | {stats['count']} | "
            f"{stats['total_downloads']:,} | {avg:,.0f} |"
        )

    return "\n".join(lines), sorted_combos[:30]


# ═══════════════════════════════════════════════════════════════════
#  MARKET SATURATION ANALYSIS
# ═══════════════════════════════════════════════════════════════════


def generate_market_saturation(db, today):
    """Identify saturated vs under-served categories."""
    categories = db.get_categories_for_date(today)
    if not categories:
        return "No category data available.", {"saturated": [], "underserved": []}

    # Compute saturation metrics
    saturated = []
    underserved = []
    for cat in categories:
        proj_count = cat["project_count"]
        avg_dl = cat["avg_downloads"]
        if proj_count < MIN_PROJECTS_THRESHOLD:
            continue
        # Saturated: many projects but low avg downloads
        if proj_count > 100 and avg_dl < 50000:
            saturated.append(cat)
        # Under-served: few projects but high avg downloads
        if proj_count < 50 and avg_dl > 200000:
            underserved.append(cat)

    saturated.sort(key=lambda x: x["project_count"], reverse=True)
    underserved.sort(key=lambda x: x["avg_downloads"], reverse=True)

    lines = []

    # Over-saturated
    lines.append("## Market Saturation — Overcrowded Categories (Avoid)")
    lines.append("")
    lines.append("| Category | Projects | Avg Downloads | Signal |")
    lines.append("|----------|---------|--------------|--------|")
    if saturated:
        for cat in saturated[:10]:
            lines.append(
                f"| {cat['category']} | {cat['project_count']} | "
                f"{cat['avg_downloads']:,.0f} | 🔴 Oversaturated |"
            )
    else:
        lines.append("| _No heavily saturated categories found_ | | | |")

    lines.append("")

    # Under-served
    lines.append("### Under-Served Niches (High Opportunity)")
    lines.append("")
    lines.append("| Category | Projects | Avg Downloads | Signal |")
    lines.append("|----------|---------|--------------|--------|")
    if underserved:
        for cat in underserved[:10]:
            lines.append(
                f"| {cat['category']} | {cat['project_count']} | "
                f"{cat['avg_downloads']:,.0f} | 🟢 Under-Served |"
            )
    else:
        lines.append("| _No clearly under-served categories found_ | | | |")

    return "\n".join(lines), {
        "saturated": saturated[:10],
        "underserved": underserved[:10],
    }


# ═══════════════════════════════════════════════════════════════════
#  GROWTH MOMENTUM
# ═══════════════════════════════════════════════════════════════════


def generate_growth_momentum(db, today, lookback_days=7):
    """Track which categories are accelerating vs decelerating."""
    cursor = db.conn.execute(
        """
        SELECT DISTINCT date FROM daily_category_stats
        WHERE date <= ?
        ORDER BY date DESC
        LIMIT ?
    """,
        (today, lookback_days + 1),
    )
    dates = [row["date"] for row in cursor.fetchall()]
    dates.reverse()

    if len(dates) < 2:
        return "Insufficient data for momentum analysis (need at least 2 days).", []

    # Gather category data for each date
    date_categories = {}
    for d in dates:
        cats = db.get_categories_for_date(d)
        date_categories[d] = {c["category"]: c for c in cats}

    # Compute daily new downloads per category across the period
    all_categories = set()
    for dc in date_categories.values():
        all_categories.update(dc.keys())

    momentum_rows = []
    for cat_name in all_categories:
        daily_new = []
        for i in range(len(dates) - 1):
            d1 = dates[i]
            d2 = dates[i + 1]
            c1 = date_categories.get(d1, {}).get(cat_name)
            c2 = date_categories.get(d2, {}).get(cat_name)
            if c1 and c2:
                new_dl = c2["total_new_downloads"]
                daily_new.append(new_dl)

        if len(daily_new) < 2:
            continue

        # Simple trend: compare first half vs second half
        mid = len(daily_new) // 2
        first_half = sum(daily_new[:mid]) / max(mid, 1)
        second_half = sum(daily_new[mid:]) / max(len(daily_new) - mid, 1)
        momentum = ((second_half - first_half) / max(first_half, 1)) * 100

        total_dl = (
            date_categories[dates[-1]].get(cat_name, {}).get("total_downloads", 0)
        )

        if momentum > 20:
            signal = "🚀 Accelerating"
        elif momentum > 5:
            signal = "📈 Growing"
        elif momentum > -5:
            signal = "➡️ Stable"
        elif momentum > -20:
            signal = "📉 Declining"
        else:
            signal = "🚨 Plunging"

        momentum_rows.append(
            {
                "category": cat_name,
                "momentum_pct": momentum,
                "signal": signal,
                "total_downloads": total_dl,
                "avg_daily_new": sum(daily_new) / max(len(daily_new), 1),
            }
        )

    momentum_rows.sort(key=lambda r: r["momentum_pct"], reverse=True)

    lines = [
        "## Growth Momentum — Categories on the Move",
        "",
        f"_Based on last {min(lookback_days, len(dates) - 1)} days of data._",
        "",
        "| Category | Momentum % | Signal | Avg Daily New Downloads |",
        "|----------|-----------|--------|------------------------|",
    ]
    for r in momentum_rows[:20]:
        lines.append(
            f"| {r['category']} | {r['momentum_pct']:+.1f}% | {r['signal']} | "
            f"{r['avg_daily_new']:,.0f} |"
        )

    return "\n".join(lines), momentum_rows


# ═══════════════════════════════════════════════════════════════════
#  RECOMMENDATIONS ENGINE
# ═══════════════════════════════════════════════════════════════════


def generate_recommendations(
    opportunity_rows, momentum_rows, category_loader_combos, today
):
    """Synthesize all analyses into concrete, actionable recommendations."""
    lines = []

    if not opportunity_rows:
        lines.append("_Insufficient data for recommendations._")
        return "\n".join(lines)

    # Top 3 by opportunity score
    top_opportunities = opportunity_rows[:3]
    lines.append("### 🎯 Top 3 Categories to Build In")
    lines.append("")
    for i, r in enumerate(top_opportunities, 1):
        lines.append(
            f"  **{i}. {r['category'].title()}** — {r['avg_downloads']:,.0f} avg downloads, "
            f"{r['projects']} competitors. Opportunity score: {r['opportunity_score']:,.0f}"
        )

    # Best loaders by category (from combo data)
    if category_loader_combos:
        lines.append("")
        lines.append("### 🛠️ Best Loaders per Category")
        lines.append("")
        # Group combos by category, pick top loader per category
        by_cat = defaultdict(list)
        for (cat, loader), stats in category_loader_combos:
            by_cat[cat].append((loader, stats))
        for cat in top_opportunities:
            cat_name = cat["category"]
            combos = by_cat.get(cat_name, [])
            if combos:
                combos.sort(key=lambda x: x[1]["total_downloads"], reverse=True)
                best_loader = combos[0][0]
                lines.append(
                    f"  - **{cat_name.title()}**: target **{best_loader}** ({combos[0][1]['total_downloads']:,} total downloads)"
                )
            else:
                lines.append(f"  - **{cat_name.title()}**: _no version data yet_")

    # Growth picks
    if momentum_rows:
        top_growers = [r for r in momentum_rows if r["momentum_pct"] > 10][:3]
        if top_growers:
            lines.append("")
            lines.append("### ⚡ Trending Categories (Fastest Growth)")
            lines.append("")
            for r in top_growers:
                lines.append(
                    f"  - **{r['category'].title()}** — {r['momentum_pct']:+.1f}% momentum ({r['signal']})"
                )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  CATEGORY RANKINGS (enhanced from original)
# ═══════════════════════════════════════════════════════════════════


def generate_category_rankings(db, today):
    """Rank categories by total downloads with growth metrics."""
    categories = db.get_categories_for_date(today)
    if not categories:
        return "No category data available for today.", []

    prev_cursor = db.conn.execute(
        "SELECT DISTINCT date FROM daily_category_stats WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today,),
    )
    prev_row = prev_cursor.fetchone()
    prev_date = prev_row["date"] if prev_row else None
    prev_categories = {}
    if prev_date:
        prev_cats = db.get_categories_for_date(prev_date)
        prev_categories = {c["category"]: c for c in prev_cats}

    lines = [
        "## Category Rankings by Total Downloads",
        "",
        "| # | Category | Projects | Total Downloads | Avg Downloads | New Today | Growth % |",
        "|---|----------|----------|----------------|---------------|-----------|----------|",
    ]
    for i, cat in enumerate(categories, 1):
        cat_name = cat["category"]
        prev = prev_categories.get(cat_name, {})
        prev_total = prev.get("total_downloads", 0)
        growth = 0
        if prev_total > 0:
            growth = ((cat["total_downloads"] - prev_total) / prev_total) * 100
        lines.append(
            f"| {i} | {cat_name} | {cat['project_count']} | {cat['total_downloads']:,} | "
            f"{cat['avg_downloads']:,.0f} | {cat['total_new_downloads']:,} | "
            f"{growth:+.4f}% |"
        )

    return "\n".join(lines), categories


# ═══════════════════════════════════════════════════════════════════
#  TOP GROWING PROJECTS
# ═══════════════════════════════════════════════════════════════════


def generate_top_growing_projects(db, today, limit=50):
    """Top projects by daily download gain (kept from original)."""
    cursor = db.conn.execute(
        """
        SELECT project_id, date, downloads, follows
        FROM daily_project_snapshots
        WHERE date = ?
        ORDER BY downloads DESC
        LIMIT 500
    """,
        (today,),
    )
    today_snapshots = {row["project_id"]: row for row in cursor.fetchall()}

    if not today_snapshots:
        return "No project snapshot data for today."

    prev_cursor = db.conn.execute(
        "SELECT DISTINCT date FROM daily_project_snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today,),
    )
    prev_row = prev_cursor.fetchone()
    if not prev_row:
        return "No previous snapshot data for comparison."
    prev_date = prev_row["date"]
    prev_cursor = db.conn.execute(
        "SELECT project_id, downloads FROM daily_project_snapshots WHERE date = ?",
        (prev_date,),
    )
    yesterday_snapshots = {
        row["project_id"]: row["downloads"] for row in prev_cursor.fetchall()
    }

    gains = []
    for pid, snap in today_snapshots.items():
        prev_downloads = yesterday_snapshots.get(pid, 0)
        gain = snap["downloads"] - prev_downloads
        gains.append((pid, snap["downloads"], prev_downloads, gain, snap["follows"]))

    gains.sort(key=lambda x: x[3], reverse=True)
    top_gains = gains[:limit]

    lines = [
        "## Top Growing Projects by Daily Gain",
        "",
        "| Project | Category | Downloads Yesterday | Downloads Today | Gain |",
        "|---------|----------|-------------------|----------------|------|",
    ]
    for pid, today_dl, yesterday_dl, gain, follows in top_gains:
        project = db.get_project(pid)
        if project:
            try:
                cats = json.loads(project.get("categories", "[]"))
                category = ", ".join(cats[:2]) if cats else "N/A"
            except (json.JSONDecodeError, TypeError):
                category = "N/A"
            title = project.get("title", pid)
            lines.append(
                f"| {title} | {category} | {yesterday_dl:,} | {today_dl:,} | {gain:+,} |"
            )
        else:
            lines.append(
                f"| {pid} | N/A | {yesterday_dl:,} | {today_dl:,} | {gain:+,} |"
            )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  LOADER & VERSION POPULARITY (enhanced)
# ═══════════════════════════════════════════════════════════════════


def generate_loader_popularity(db):
    """Rank loaders by total downloads and project count."""
    projects = db.get_all_projects()
    loader_counts = defaultdict(int)
    loader_downloads = defaultdict(int)

    for project in projects:
        try:
            cats = json.loads(project.get("categories", "[]"))
        except (json.JSONDecodeError, TypeError):
            cats = []
        downloads = project.get("downloads", 0)
        for loader in ["fabric", "forge", "neoforge", "quilt"]:
            if loader in cats:
                loader_counts[loader] += 1
                loader_downloads[loader] += downloads

    lines = [
        "## Loader Popularity",
        "",
        "| Loader | Projects | Total Downloads | Avg / Project |",
        "|--------|---------|----------------|---------------|",
    ]
    for loader in sorted(
        loader_counts.keys(), key=lambda l: loader_downloads[l], reverse=True
    ):
        avg = (
            loader_downloads[loader] / loader_counts[loader]
            if loader_counts[loader] > 0
            else 0
        )
        lines.append(
            f"| {loader} | {loader_counts[loader]} | {loader_downloads[loader]:,} | {avg:,.0f} |"
        )

    return "\n".join(lines)


def generate_game_version_popularity(db):
    """Rank game versions by total downloads."""
    ver_cursor = db.conn.execute(
        "SELECT project_id, id, game_versions, downloads FROM versions"
    )
    version_counts = defaultdict(int)
    version_downloads = defaultdict(int)

    for row in ver_cursor.fetchall():
        try:
            game_versions = json.loads(row["game_versions"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(game_versions, list):
            continue
        for gv in game_versions:
            version_counts[gv] += 1
            version_downloads[gv] += row["downloads"]

    major_versions = {}
    for ver, count in version_counts.items():
        parts = ver.split(".")
        if len(parts) >= 2:
            major_key = ".".join(parts[:2]) if len(parts) == 2 else ".".join(parts[:3])
            if major_key not in major_versions:
                major_versions[major_key] = {"count": 0, "downloads": 0}
            major_versions[major_key]["count"] += count
            major_versions[major_key]["downloads"] += version_downloads[ver]

    lines = [
        "## Game Version Popularity (Top 15)",
        "",
        "| Version | Project Count | Total Downloads | Avg / Project |",
        "|---------|--------------|----------------|---------------|",
    ]
    for ver in sorted(
        major_versions.keys(), key=lambda v: major_versions[v]["count"], reverse=True
    )[:15]:
        data = major_versions[ver]
        avg = data["downloads"] / data["count"] if data["count"] > 0 else 0
        lines.append(
            f"| {ver} | {data['count']} | {data['downloads']:,} | {avg:,.0f} |"
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════


def main():
    print("=== Phase 5: Analyze — Market Intelligence ===")

    today = get_current_date()
    db = Database("data/modrinth_tracker.db")

    # ── Generate all report sections ────────────────────────────

    # Rankings
    rankings_text, rankings_data = generate_category_rankings(db, today)
    if isinstance(rankings_data, str):
        rankings_data = []  # no data yet

    # Opportunity Matrix
    opp_text, opp_rows = generate_opportunity_matrix(db, today)
    if isinstance(opp_rows, str):
        opp_rows = []

    # Category + Loader Cross Analysis
    combo_text, combos = generate_category_loader_analysis(db)
    if isinstance(combos, str):
        combos = []

    # Market Saturation
    sat_text, sat_data = generate_market_saturation(db, today)
    if isinstance(sat_data, str):
        sat_data = {"saturated": [], "underserved": []}

    # Growth Momentum
    momentum_text, momentum_rows = generate_growth_momentum(db, today)
    if isinstance(momentum_rows, str):
        momentum_rows = []

    # Recommendations
    recs = generate_recommendations(opp_rows, momentum_rows, combos, today)

    # Top Growing Projects
    top_projects_text = generate_top_growing_projects(db, today)

    # Loader Popularity
    loader_text = generate_loader_popularity(db)

    # Game Version Popularity
    version_text = generate_game_version_popularity(db)

    # ── Assemble Report ─────────────────────────────────────────

    sections = [
        f"# Modrinth Market Intelligence Report — {today}",
        "",
        "---",
        "## 📋 Executive Summary",
        "",
        recs
        if recs
        else "_Collecting data... Reports will populate after the first few daily runs._",
        "",
        "---",
        rankings_text,
        "",
        "---",
        opp_text,
        "",
        "---",
        combo_text,
        "",
        "---",
        sat_text,
        "",
        "---",
        momentum_text,
        "",
        "---",
        top_projects_text,
        "",
        "---",
        loader_text,
        "",
        "---",
        version_text,
        "",
        "---",
        f"_Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
    ]

    report = "\n".join(sections)

    # ── Save report ─────────────────────────────────────────────

    ensure_dir("reports")
    report_path = f"reports/daily_report_{today}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Saved report to {report_path}")

    # ── Save structured summary JSON ────────────────────────────

    summary = {
        "date": today,
        "executive_summary": {
            "top_opportunities": [
                {
                    "category": r["category"],
                    "avg_downloads": r["avg_downloads"],
                    "projects": r["projects"],
                    "opportunity_score": r["opportunity_score"],
                    "new_downloads": r["new_downloads"],
                }
                for r in opp_rows[:5]
            ],
            "top_trending": [
                {
                    "category": r["category"],
                    "momentum_pct": round(r["momentum_pct"], 2),
                    "signal": r["signal"],
                }
                for r in momentum_rows[:5]
            ],
        },
        "categories": [
            {
                "category": c["category"],
                "total_downloads": c["total_downloads"],
                "project_count": c["project_count"],
                "avg_downloads": c["avg_downloads"],
                "new_downloads": c["total_new_downloads"],
                "opportunity_score": next(
                    (
                        r["opportunity_score"]
                        for r in opp_rows
                        if r["category"] == c["category"]
                    ),
                    None,
                ),
            }
            for c in rankings_data
        ],
        "category_loader_combos": [
            {
                "category": cat,
                "loader": loader,
                "projects": stats["count"],
                "total_downloads": stats["total_downloads"],
                "avg_downloads": round(stats["total_downloads"] / stats["count"])
                if stats["count"] > 0
                else 0,
            }
            for (cat, loader), stats in combos
        ],
        "market_saturation": {
            "oversaturated": [c["category"] for c in sat_data.get("saturated", [])],
            "underserved": [c["category"] for c in sat_data.get("underserved", [])],
        },
        "growth_momentum": [
            {
                "category": r["category"],
                "momentum_pct": round(r["momentum_pct"], 2),
                "signal": r["signal"],
            }
            for r in momentum_rows
        ],
    }
    save_json("reports/latest_summary.json", summary)
    print("Saved structured summary to reports/latest_summary.json")

    db.close()
    print("=== Analyze complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
