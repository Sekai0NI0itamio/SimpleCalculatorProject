#!/usr/bin/env python3
"""
Phase 5: Analyze
- Generates reports by category, loader, game version
- Identifies trends with advanced mathematics
- Outputs markdown reports to reports/ and JSON to reports/latest_analysis.json
"""
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from utils import load_json, save_json, ensure_dir, get_current_date
from db import Database


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def sigmoid(x):
    """Sigmoid function."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 1.0 if x > 0 else 0.0


def ema_smooth(values, alpha=0.3):
    """Exponential moving average smoothing of a list of values (oldest first)."""
    if not values:
        return 0.0
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def min_max_normalize(values):
    """Min-max normalize a list of numbers to [0, 1]. Returns dict {value: normalized}."""
    if not values:
        return {}
    mn = min(values)
    mx = max(values)
    if mx == mn:
        return {v: 0.5 for v in values}
    return {v: (v - mn) / (mx - mn) for v in values}


# ---------------------------------------------------------------------------
# Category rankings (enhanced with advanced metrics)
# ---------------------------------------------------------------------------

def generate_category_rankings(db, today):
    """Generate category rankings with growth rate, projections, saturation, opportunity."""
    categories = db.get_categories_for_date(today)
    if not categories:
        return "No category data available for today.", []

    # Gather previous dates for trend computation
    cursor = db.conn.execute(
        "SELECT DISTINCT date FROM daily_category_stats WHERE date <= ? ORDER BY date DESC",
        (today,)
    )
    all_dates = [row["date"] for row in cursor.fetchall()]
    all_dates.sort()  # oldest first

    # Build per-category download history (oldest first)
    cat_history = defaultdict(list)  # category -> [(date, total_downloads)]
    for d in all_dates:
        rows = db.get_categories_for_date(d)
        for r in rows:
            cat_history[r["category"]].append((d, r["total_downloads"]))

    # Compute growth rate (EMA-smoothed) per category
    growth_rates = {}
    for cat, history in cat_history.items():
        vals = [h[1] for h in history]
        if len(vals) >= 2:
            # Daily growth rates
            daily_rates = []
            for i in range(1, len(vals)):
                if vals[i - 1] > 0:
                    daily_rates.append((vals[i] - vals[i - 1]) / vals[i - 1])
            if daily_rates:
                growth_rates[cat] = ema_smooth(daily_rates, alpha=0.3)
            else:
                growth_rates[cat] = 0.0
        else:
            growth_rates[cat] = 0.0

    # Gather all category data for normalization
    all_densities = []
    all_competition = []
    all_opportunity = []
    cat_metrics = []

    for cat in categories:
        cat_name = cat["category"]
        total_dl = cat["total_downloads"]
        proj_count = cat["project_count"]
        avg_dl = cat["avg_downloads"]

        # Density
        density = total_dl / proj_count if proj_count > 0 else 0.0

        # Competition score (sigmoid normalized)
        competition_score = 1.0 - sigmoid(-0.001 * (proj_count - 500))

        all_densities.append(density)
        all_competition.append(competition_score)

        cat_metrics.append({
            "cat": cat_name,
            "total_downloads": total_dl,
            "project_count": proj_count,
            "avg_downloads": avg_dl,
            "new_downloads": cat["total_new_downloads"],
            "growth_rate": growth_rates.get(cat_name, 0.0),
            "density": density,
            "competition_score": competition_score,
        })

    # Normalize densities and compute opportunity scores
    density_norm_map = min_max_normalize(all_densities) if all_densities else {}
    comp_norm_map = min_max_normalize(all_competition) if all_competition else {}

    # Normalize growth rates for opportunity calculation
    all_growth_vals = [m["growth_rate"] for m in cat_metrics]
    growth_norm_map = min_max_normalize(all_growth_vals) if all_growth_vals else {}

    for m in cat_metrics:
        g_norm = growth_norm_map.get(m["growth_rate"], 0.5)
        c_norm = comp_norm_map.get(m["competition_score"], 0.5)
        d_norm = density_norm_map.get(m["density"], 0.5)
        m["opportunity_score"] = (g_norm * 0.5) + ((1.0 - c_norm) * 0.3) + (d_norm * 0.2)

    # Sort by total downloads descending (original order)
    cat_metrics.sort(key=lambda m: m["total_downloads"], reverse=True)

    # Build markdown table
    lines = [
        "## Category Rankings",
        "",
        "| Category | Projects | Total Downloads | Avg Downloads | New Downloads Today | Growth % | Growth Rate | 7d Projected | 30d Projected | Density | Competition | Opportunity |",
        "|----------|----------|----------------|---------------|-------------------|----------|-------------|-------------|--------------|---------|-------------|-------------|"
    ]

    for m in cat_metrics:
        gr = m["growth_rate"]
        proj_7d = int(m["total_downloads"] * ((1 + gr) ** 7)) if gr != 0 else m["total_downloads"]
        proj_30d = int(m["total_downloads"] * ((1 + gr) ** 30)) if gr != 0 else m["total_downloads"]
        growth_pct = gr * 100

        # Previous day comparison for Growth %
        prev_cursor = db.conn.execute(
            "SELECT DISTINCT date FROM daily_category_stats WHERE date < ? ORDER BY date DESC LIMIT 1",
            (today,)
        )
        prev_row = prev_cursor.fetchone()
        growth_pct_display = 0.0
        if prev_row:
            prev_date = prev_row["date"]
            prev_cats = db.get_categories_for_date(prev_date)
            prev_map = {pc["category"]: pc["total_downloads"] for pc in prev_cats}
            prev_total = prev_map.get(m["cat"], 0)
            if prev_total > 0:
                growth_pct_display = ((m["total_downloads"] - prev_total) / prev_total) * 100

        lines.append(
            f"| {m['cat']} | {m['project_count']} | {m['total_downloads']:,} | "
            f"{m['avg_downloads']:,.0f} | {m['new_downloads']:,} | "
            f"{growth_pct_display:+.2f}% | {gr:.6f} | {proj_7d:,} | {proj_30d:,} | "
            f"{m['density']:,.0f} | {m['competition_score']:.4f} | {m['opportunity_score']:.4f} |"
        )

    return "\n".join(lines), cat_metrics


# ---------------------------------------------------------------------------
# Category trends (7-day comparison, kept from original)
# ---------------------------------------------------------------------------

def generate_category_trends(db, today):
    """Generate 7-day category trends."""
    cursor = db.conn.execute("""
        SELECT DISTINCT date FROM daily_category_stats
        WHERE date <= ?
        ORDER BY date DESC
        LIMIT 7
    """, (today,))
    dates = [row["date"] for row in cursor.fetchall()]
    dates.reverse()

    if len(dates) < 2:
        return "Insufficient data for trend analysis (need at least 2 days)."

    oldest_date = dates[0]
    newest_date = dates[-1]

    oldest_cats = db.get_categories_for_date(oldest_date)
    newest_cats = db.get_categories_for_date(newest_date)

    oldest_map = {c["category"]: c["total_downloads"] for c in oldest_cats}
    newest_map = {c["category"]: c for c in newest_cats}

    lines = [
        f"## Category Trends ({oldest_date} to {newest_date})",
        "",
        f"| Category | Downloads {oldest_date} | Downloads {newest_date} | Change | Growth % |",
        "|----------|------------------------|------------------------|--------|----------|"
    ]

    sorted_cats = sorted(
        newest_cats,
        key=lambda c: c["total_downloads"] - oldest_map.get(c["category"], 0),
        reverse=True
    )

    for cat in sorted_cats:
        cat_name = cat["category"]
        old_val = oldest_map.get(cat_name, 0)
        new_val = cat["total_downloads"]
        change = new_val - old_val
        growth = 0
        if old_val > 0:
            growth = (change / old_val) * 100

        lines.append(
            f"| {cat_name} | {old_val:,} | {new_val:,} | {change:+,} | {growth:+.2f}% |"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top growing projects (daily download gain)
# ---------------------------------------------------------------------------

def generate_top_growing_projects(db, today, limit=50):
    """Generate top growing projects by daily download gain."""
    cursor = db.conn.execute("""
        SELECT project_id, date, downloads, follows
        FROM daily_project_snapshots
        WHERE date = ?
        ORDER BY downloads DESC
        LIMIT 500
    """, (today,))
    today_snapshots = {row["project_id"]: row for row in cursor.fetchall()}

    if not today_snapshots:
        return "No project snapshot data available for today."

    prev_cursor = db.conn.execute(
        "SELECT DISTINCT date FROM daily_project_snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today,)
    )
    prev_row = prev_cursor.fetchone()
    if not prev_row:
        return "No previous snapshot data available for comparison."

    prev_date = prev_row["date"]
    prev_cursor = db.conn.execute(
        "SELECT project_id, downloads FROM daily_project_snapshots WHERE date = ?",
        (prev_date,)
    )
    yesterday_snapshots = {row["project_id"]: row["downloads"] for row in prev_cursor.fetchall()}

    gains = []
    for pid, snap in today_snapshots.items():
        prev_downloads = yesterday_snapshots.get(pid, 0)
        gain = snap["downloads"] - prev_downloads
        gains.append((pid, snap["downloads"], prev_downloads, gain, snap["follows"]))

    gains.sort(key=lambda x: x[3], reverse=True)
    top_gains = gains[:limit]

    lines = [
        "## Top Growing Projects",
        "",
        "| Project | Category | Downloads Yesterday | Downloads Today | Gain |",
        "|---------|----------|-------------------|----------------|------|"
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
            lines.append(f"| {pid} | N/A | {yesterday_dl:,} | {today_dl:,} | {gain:+,} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loader ecosystem analysis
# ---------------------------------------------------------------------------

def generate_loader_ecosystem(db):
    """Generate loader ecosystem analysis with counts, downloads, and growth."""
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

    # Determine fastest growing loader by new project count
    # (projects with date_created closest to today)
    loader_new_projects = defaultdict(int)
    for project in projects:
        try:
            cats = json.loads(project.get("categories", "[]"))
        except (json.JSONDecodeError, TypeError):
            cats = []
        created = project.get("date_created", "")
        # Count projects created in the last 30 days as "new"
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                delta = now - created_dt
                if delta.days <= 30:
                    for loader in ["fabric", "forge", "neoforge", "quilt"]:
                        if loader in cats:
                            loader_new_projects[loader] += 1
            except (ValueError, TypeError):
                pass

    # Build sorted list
    loader_data = []
    for loader in sorted(loader_counts.keys(), key=lambda l: loader_counts[l], reverse=True):
        pc = loader_counts[loader]
        td = loader_downloads[loader]
        avg_dl = td // pc if pc > 0 else 0
        new_pc = loader_new_projects.get(loader, 0)
        loader_data.append({
            "loader": loader,
            "project_count": pc,
            "total_downloads": td,
            "avg_downloads": avg_dl,
            "new_projects_30d": new_pc,
        })

    # Find fastest growing loader
    fastest_loader = "N/A"
    if loader_new_projects:
        fastest_loader = max(loader_new_projects, key=loader_new_projects.get)

    lines = [
        "## Loader Ecosystem Analysis",
        "",
        "| Loader | Project Count | Total Downloads | Avg Downloads / Project | New Projects (30d) |",
        "|--------|--------------|----------------|------------------------|-------------------|"
    ]

    for ld in loader_data:
        lines.append(
            f"| {ld['loader']} | {ld['project_count']} | {ld['total_downloads']:,} | "
            f"{ld['avg_downloads']:,} | {ld['new_projects_30d']} |"
        )
    lines.append("")
    lines.append(f"**Fastest growing loader (by new project count):** {fastest_loader}")

    return "\n".join(lines), loader_data, fastest_loader


# ---------------------------------------------------------------------------
# Version adoption analysis
# ---------------------------------------------------------------------------

def generate_version_adoption(db):
    """Analyze version adoption across major Minecraft versions."""
    cursor = db.conn.execute("SELECT project_id, id, game_versions, downloads FROM versions")
    version_counts = defaultdict(int)
    version_downloads = defaultdict(int)
    version_projects = defaultdict(set)

    for row in cursor.fetchall():
        try:
            game_versions = json.loads(row["game_versions"])
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(game_versions, list):
            continue

        for gv in game_versions:
            parts = gv.split(".")
            if len(parts) >= 2:
                major_key = ".".join(parts[:2]) if len(parts) == 2 else ".".join(parts[:3])
            else:
                major_key = gv

            version_counts[major_key] += 1
            version_downloads[major_key] += row["downloads"]
            version_projects[major_key].add(row["project_id"])

    # Sort by project count descending
    sorted_versions = sorted(version_counts.keys(), key=lambda v: version_counts[v], reverse=True)

    version_data = []
    for ver in sorted_versions[:20]:
        version_data.append({
            "version": ver,
            "project_count": len(version_projects[ver]),
            "total_downloads": version_downloads[ver],
        })

    lines = [
        "## Version Adoption Analysis",
        "",
        "| Version | Project Count | Total Downloads |",
        "|---------|--------------|----------------|"
    ]

    for vd in version_data:
        lines.append(f"| {vd['version']} | {vd['project_count']} | {vd['total_downloads']:,} |")

    # Identify trending versions (those with most projects adding support)
    # We can check which versions have the most distinct projects
    trending = sorted(version_data, key=lambda v: v["project_count"], reverse=True)[:3]
    trending_names = [t["version"] for t in trending]

    lines.append("")
    lines.append(f"**Trending versions (most projects):** {', '.join(trending_names)}")

    return "\n".join(lines), version_data, trending_names


# ---------------------------------------------------------------------------
# Top projects by total downloads
# ---------------------------------------------------------------------------

def generate_top_projects(db, limit=100):
    """Generate top N projects by total downloads."""
    cursor = db.conn.execute("""
        SELECT p.project_id, p.slug, p.title, p.categories, p.downloads,
               (SELECT COUNT(*) FROM versions v WHERE v.project_id = p.project_id) AS version_count
        FROM projects p
        ORDER BY p.downloads DESC
        LIMIT ?
    """, (limit,))

    top_projects = []
    for row in cursor.fetchall():
        try:
            cats = json.loads(row["categories"]) if row["categories"] else []
        except (json.JSONDecodeError, TypeError):
            cats = []
        top_projects.append({
            "project_id": row["project_id"],
            "slug": row["slug"],
            "title": row["title"],
            "categories": cats,
            "total_downloads": row["downloads"],
            "version_count": row["version_count"],
        })

    lines = [
        "## Top Projects by Total Downloads",
        "",
        "| Rank | Project | Slug | Categories | Total Downloads | Versions |",
        "|------|---------|------|------------|----------------|----------|"
    ]

    for i, tp in enumerate(top_projects, 1):
        cat_str = ", ".join(tp["categories"][:3]) if tp["categories"] else "N/A"
        lines.append(
            f"| {i} | {tp['title']} | {tp['slug']} | {cat_str} | "
            f"{tp['total_downloads']:,} | {tp['version_count']} |"
        )

    return "\n".join(lines), top_projects


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------

def generate_recommendations(cat_metrics, loader_data):
    """Generate 'what to build next' recommendations."""
    recommendations = []

    if not cat_metrics:
        return "Insufficient data for recommendations.", []

    # 1. Categories with high opportunity_score + high growth
    sorted_by_opportunity = sorted(cat_metrics, key=lambda m: m["opportunity_score"], reverse=True)
    sorted_by_growth = sorted(cat_metrics, key=lambda m: m["growth_rate"], reverse=True)

    # Top opportunity category
    top_opp = sorted_by_opportunity[0] if sorted_by_opportunity else None
    if top_opp and top_opp["opportunity_score"] > 0:
        recommendations.append({
            "rank": 1,
            "category": top_opp["cat"],
            "loader": "N/A",
            "reason": (
                f"Highest opportunity score ({top_opp['opportunity_score']:.2f}) with "
                f"{top_opp['project_count']:,} projects and {top_opp['avg_downloads']:,} avg downloads. "
                "Strong balance of growth potential and manageable competition."
            ),
            "opportunity_score": round(top_opp["opportunity_score"], 4),
            "avg_downloads": int(top_opp["avg_downloads"]),
            "project_count": top_opp["project_count"],
        })

    # 2. Fastest growing category
    top_growth = sorted_by_growth[0] if sorted_by_growth else None
    if top_growth and top_growth["growth_rate"] > 0 and (not top_opp or top_growth["cat"] != top_opp["cat"]):
        recommendations.append({
            "rank": 2,
            "category": top_growth["cat"],
            "loader": "N/A",
            "reason": (
                f"Highest growth rate ({top_growth['growth_rate']:.4f}) with "
                f"{top_growth['project_count']:,} projects. "
                "Momentum is strong — early entry while competition is still forming."
            ),
            "opportunity_score": round(top_growth["opportunity_score"], 4),
            "avg_downloads": int(top_growth["avg_downloads"]),
            "project_count": top_growth["project_count"],
        })

    # 3. Low competition + high avg downloads
    sorted_by_comp = sorted(cat_metrics, key=lambda m: m["competition_score"])
    low_comp_high_avg = [
        m for m in sorted_by_comp
        if m["competition_score"] < 0.5 and m["avg_downloads"] > 50000
    ]
    if low_comp_high_avg:
        pick = low_comp_high_avg[0]
        # Find a loader that pairs well
        suggested_loader = "N/A"
        for ld in loader_data:
            if ld["loader"] in pick["cat"] or pick["cat"] in ld["loader"]:
                suggested_loader = ld["loader"]
                break
        if suggested_loader == "N/A" and loader_data:
            suggested_loader = loader_data[0]["loader"]
        recommendations.append({
            "rank": 3,
            "category": pick["cat"],
            "loader": suggested_loader,
            "reason": (
                f"Low competition (score: {pick['competition_score']:.2f}) yet high average downloads "
                f"({pick['avg_downloads']:,}). Build for {suggested_loader} to capture underserved demand."
            ),
            "opportunity_score": round(pick["opportunity_score"], 4),
            "avg_downloads": int(pick["avg_downloads"]),
            "project_count": pick["project_count"],
        })

    # 4. Underserved loader + popular category
    if loader_data:
        # Find a loader growing fast but with relatively few projects
        loader_sorted = sorted(loader_data, key=lambda l: l["new_projects_30d"], reverse=True)
        for fast_loader in loader_sorted:
            if fast_loader["new_projects_30d"] > 0:
                # Find a popular category that doesn't have many projects for this loader
                # Look for a category with high avg_downloads but moderate competition
                good_cats = [
                    m for m in cat_metrics
                    if m["competition_score"] < 0.7 and m["avg_downloads"] > 80000
                    and m["project_count"] > 100
                ]
                if good_cats:
                    target_cat = good_cats[0]
                    recommendations.append({
                        "rank": 4,
                        "category": target_cat["cat"],
                        "loader": fast_loader["loader"],
                        "reason": (
                            f"{fast_loader['loader'].title()} is growing fast ({fast_loader['new_projects_30d']} new projects/30d) "
                            f"and '{target_cat['cat']}' has high avg downloads ({target_cat['avg_downloads']:,}) "
                            f"with moderate competition. Combine for a strong niche."
                        ),
                        "opportunity_score": round(target_cat["opportunity_score"], 4),
                        "avg_downloads": int(target_cat["avg_downloads"]),
                        "project_count": target_cat["project_count"],
                    })
                break

    # 5. Second best opportunity (different category from #1)
    if len(sorted_by_opportunity) > 1:
        alt = sorted_by_opportunity[1]
        if not recommendations or alt["cat"] != recommendations[0]["category"]:
            suggestions_loader = "N/A"
            if loader_data:
                suggestions_loader = loader_data[0]["loader"]
            recommendations.append({
                "rank": 5,
                "category": alt["cat"],
                "loader": suggestions_loader,
                "reason": (
                    f"Strong alternate opportunity (score: {alt['opportunity_score']:.2f}) with "
                    f"{alt['project_count']:,} projects and {alt['avg_downloads']:,} avg downloads. "
                    f"Diversify into {alt['cat']} for broad market coverage."
                ),
                "opportunity_score": round(alt["opportunity_score"], 4),
                "avg_downloads": int(alt["avg_downloads"]),
                "project_count": alt["project_count"],
            })

    # Ensure we have at least some recommendations
    while len(recommendations) < 5 and sorted_by_opportunity:
        idx = len(recommendations)
        if idx < len(sorted_by_opportunity):
            m = sorted_by_opportunity[idx]
            if not any(r["category"] == m["cat"] for r in recommendations):
                recommendations.append({
                    "rank": idx + 1,
                    "category": m["cat"],
                    "loader": "N/A",
                    "reason": (
                        f"Opportunity score {m['opportunity_score']:.2f} — "
                        f"{m['project_count']:,} projects, {m['avg_downloads']:,} avg downloads."
                    ),
                    "opportunity_score": round(m["opportunity_score"], 4),
                    "avg_downloads": int(m["avg_downloads"]),
                    "project_count": m["project_count"],
                })
        else:
            break

    # Truncate / renumber
    recommendations = recommendations[:5]
    for i, r in enumerate(recommendations):
        r["rank"] = i + 1

    lines = [
        "## Recommendations: What to Build Next",
        "",
        "| Rank | Category | Loader | Opportunity Score | Avg Downloads | Project Count | Reasoning |",
        "|------|----------|--------|-------------------|---------------|---------------|-----------|"
    ]

    for r in recommendations:
        lines.append(
            f"| {r['rank']} | {r['category']} | {r['loader']} | {r['opportunity_score']:.4f} | "
            f"{r['avg_downloads']:,} | {r['project_count']:,} | {r['reason']} |"
        )

    return "\n".join(lines), recommendations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Phase 5: Analyze (Advanced) ===")

    today = get_current_date()
    db_path = "data/modrinth_tracker.db"
    db = Database(db_path)

    # -- Gather totals -------------------------------------------------------
    cursor = db.conn.execute("SELECT COUNT(*) AS cnt FROM projects")
    total_projects = cursor.fetchone()["cnt"]

    cursor = db.conn.execute("SELECT COUNT(*) AS cnt FROM versions")
    total_versions = cursor.fetchone()["cnt"]

    # Total ecosystem downloads (sum of all project downloads)
    cursor = db.conn.execute("SELECT COALESCE(SUM(downloads), 0) AS total FROM projects")
    total_eco_dl = cursor.fetchone()["total"]

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- Generate all sections -----------------------------------------------
    sections = [f"# Modrinth Daily Report - {today}", ""]

    # A. Category rankings with advanced metrics
    cat_table, cat_metrics = generate_category_rankings(db, today)
    sections.append(cat_table)
    sections.append("")

    # B. Category trends (7-day)
    sections.append(generate_category_trends(db, today))
    sections.append("")

    # C. Top growing projects (daily gain)
    sections.append(generate_top_growing_projects(db, today))
    sections.append("")

    # D. Loader ecosystem analysis
    loader_table, loader_data, fastest_loader = generate_loader_ecosystem(db)
    sections.append(loader_table)
    sections.append("")

    # E. Version adoption analysis
    version_table, version_data, trending_versions = generate_version_adoption(db)
    sections.append(version_table)
    sections.append("")

    # F. Top projects by total downloads
    top_projects_table, top_projects = generate_top_projects(db, limit=100)
    sections.append(top_projects_table)
    sections.append("")

    # G. Recommendations
    rec_table, recommendations = generate_recommendations(cat_metrics, loader_data)
    sections.append(rec_table)
    sections.append("")

    report = "\n".join(sections)

    # -- Save markdown report ------------------------------------------------
    ensure_dir("reports")
    report_path = f"reports/daily_report_{today}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Saved markdown report to {report_path}")

    # -- Build JSON output ---------------------------------------------------
    category_rankings_json = []
    for m in cat_metrics:
        gr = m["growth_rate"]
        proj_7d = int(m["total_downloads"] * ((1 + gr) ** 7)) if gr != 0 else m["total_downloads"]
        proj_30d = int(m["total_downloads"] * ((1 + gr) ** 30)) if gr != 0 else m["total_downloads"]
        growth_pct = gr * 100

        # Previous day comparison for growth_percent
        prev_cursor = db.conn.execute(
            "SELECT DISTINCT date FROM daily_category_stats WHERE date < ? ORDER BY date DESC LIMIT 1",
            (today,)
        )
        prev_row = prev_cursor.fetchone()
        growth_pct_display = 0.0
        if prev_row:
            prev_date = prev_row["date"]
            prev_cats = db.get_categories_for_date(prev_date)
            prev_map = {pc["category"]: pc["total_downloads"] for pc in prev_cats}
            prev_total = prev_map.get(m["cat"], 0)
            if prev_total > 0:
                growth_pct_display = ((m["total_downloads"] - prev_total) / prev_total) * 100

        category_rankings_json.append({
            "category": m["cat"],
            "project_count": m["project_count"],
            "total_downloads": m["total_downloads"],
            "avg_downloads": int(m["avg_downloads"]),
            "new_downloads_today": m["new_downloads"],
            "growth_percent": round(growth_pct_display, 2),
            "growth_rate": round(gr, 6),
            "projected_7d": proj_7d,
            "projected_30d": proj_30d,
            "density": int(m["density"]),
            "competition_score": round(m["competition_score"], 4),
            "opportunity_score": round(m["opportunity_score"], 4),
        })

    loader_rankings_json = [
        {
            "loader": ld["loader"],
            "project_count": ld["project_count"],
            "total_downloads": ld["total_downloads"],
            "avg_downloads": ld["avg_downloads"],
        }
        for ld in loader_data
    ]

    version_rankings_json = [
        {
            "version": vd["version"],
            "project_count": vd["project_count"],
            "total_downloads": vd["total_downloads"],
        }
        for vd in version_data
    ]

    # Determine fastest growing category
    fastest_growing_cat = "N/A"
    if cat_metrics:
        sorted_by_growth = sorted(cat_metrics, key=lambda m: m["growth_rate"], reverse=True)
        if sorted_by_growth and sorted_by_growth[0]["growth_rate"] > 0:
            fastest_growing_cat = sorted_by_growth[0]["cat"]

    # Highest opportunity category
    highest_opp_cat = "N/A"
    if cat_metrics:
        sorted_by_opp = sorted(cat_metrics, key=lambda m: m["opportunity_score"], reverse=True)
        highest_opp_cat = sorted_by_opp[0]["cat"]

    # Most popular loader
    most_popular_loader = "N/A"
    if loader_data:
        most_popular_loader = max(loader_data, key=lambda l: l["project_count"])["loader"]

    analysis = {
        "report_date": today,
        "total_projects": total_projects,
        "total_versions": total_versions,
        "fetched_at": now_iso,
        "category_rankings": category_rankings_json,
        "loader_rankings": loader_rankings_json,
        "version_rankings": version_rankings_json,
        "top_projects": top_projects,
        "recommendations": [
            {
                "rank": r["rank"],
                "category": r["category"],
                "loader": r["loader"],
                "reason": r["reason"],
                "opportunity_score": r["opportunity_score"],
                "avg_downloads": r["avg_downloads"],
                "project_count": r["project_count"],
            }
            for r in recommendations
        ],
        "trends": {
            "fastest_growing_category": fastest_growing_cat,
            "highest_opportunity_category": highest_opp_cat,
            "most_popular_loader": most_popular_loader,
            "fastest_growing_loader": fastest_loader,
            "total_ecosystem_downloads": total_eco_dl,
        },
    }

    save_json("reports/latest_analysis.json", analysis)
    print("Saved JSON analysis to reports/latest_analysis.json")

    # Also keep the original summary for backward compatibility
    categories = db.get_categories_for_date(today)
    summary = {
        "date": today,
        "categories": [
            {
                "category": c["category"],
                "total_downloads": c["total_downloads"],
                "project_count": c["project_count"],
                "avg_downloads": c["avg_downloads"],
                "new_downloads": c["total_new_downloads"],
            }
            for c in categories
        ]
    }
    save_json("reports/latest_summary.json", summary)
    print("Saved summary to reports/latest_summary.json")

    db.close()
    print("=== Analyze complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())