#!/usr/bin/env python3
"""
Phase 5: Analyze
- Generates reports by category, loader, game version
- Identifies trends
- Outputs markdown reports to reports/
"""
import json
import os
import sys
from collections import defaultdict

from utils import load_json, save_json, ensure_dir, get_current_date
from db import Database


def generate_category_rankings(db, today):
    """Generate category rankings sorted by total downloads."""
    categories = db.get_categories_for_date(today)
    if not categories:
        return "No category data available for today."

    # Get previous day's data for comparison
    prev_cursor = db.conn.execute(
        "SELECT DISTINCT date FROM daily_category_stats WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today,)
    )
    prev_row = prev_cursor.fetchone()
    prev_date = prev_row["date"] if prev_row else None

    prev_categories = {}
    if prev_date:
        prev_cats = db.get_categories_for_date(prev_date)
        prev_categories = {c["category"]: c for c in prev_cats}

    lines = [
        "## Category Rankings",
        "",
        "| Category | Projects | Total Downloads | Avg Downloads | New Downloads Today | Growth % |",
        "|----------|----------|----------------|---------------|-------------------|----------|"
    ]

    for cat in categories:
        cat_name = cat["category"]
        prev = prev_categories.get(cat_name, {})
        prev_total = prev.get("total_downloads", 0)
        growth = 0
        if prev_total > 0:
            growth = ((cat["total_downloads"] - prev_total) / prev_total) * 100

        lines.append(
            f"| {cat_name} | {cat['project_count']} | {cat['total_downloads']:,} | "
            f"{cat['avg_downloads']:,.0f} | {cat['total_new_downloads']:,} | "
            f"{growth:+.2f}% |"
        )

    return "\n".join(lines)


def generate_category_trends(db, today):
    """Generate 7-day category trends."""
    # Get data for the last 7 days
    cursor = db.conn.execute("""
        SELECT DISTINCT date FROM daily_category_stats
        WHERE date <= ?
        ORDER BY date DESC
        LIMIT 7
    """, (today,))
    dates = [row["date"] for row in cursor.fetchall()]
    dates.reverse()  # Oldest first

    if len(dates) < 2:
        return "Insufficient data for trend analysis (need at least 2 days)."

    # Get data for oldest and newest dates
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

    # Sort by absolute growth
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

    # Get yesterday's data
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

    # Calculate gains
    gains = []
    for pid, snap in today_snapshots.items():
        prev_downloads = yesterday_snapshots.get(pid, 0)
        gain = snap["downloads"] - prev_downloads
        gains.append((pid, snap["downloads"], prev_downloads, gain, snap["follows"]))

    # Sort by gain descending
    gains.sort(key=lambda x: x[3], reverse=True)
    top_gains = gains[:limit]

    # Get project details
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


def generate_loader_popularity(db):
    """Generate loader popularity report."""
    projects = db.get_all_projects()

    loader_counts = defaultdict(int)
    loader_downloads = defaultdict(int)

    for project in projects:
        try:
            cats = json.loads(project.get("categories", "[]"))
        except (json.JSONDecodeError, TypeError):
            cats = []

        downloads = project.get("downloads", 0)

        # Check for loader categories
        for loader in ["fabric", "forge", "neoforge", "quilt"]:
            if loader in cats:
                loader_counts[loader] += 1
                loader_downloads[loader] += downloads

    lines = [
        "## Loader Popularity",
        "",
        "| Loader | Project Count | Total Downloads |",
        "|--------|--------------|----------------|"
    ]

    for loader in sorted(loader_counts.keys(), key=lambda l: loader_counts[l], reverse=True):
        lines.append(
            f"| {loader} | {loader_counts[loader]} | {loader_downloads[loader]:,} |"
        )

    return "\n".join(lines)


def generate_game_version_popularity(db):
    """Generate game version popularity report from version data."""
    cursor = db.conn.execute("SELECT game_versions FROM versions WHERE game_versions IS NOT NULL")
    version_counts = defaultdict(int)
    version_downloads = defaultdict(int)

    # Also get version downloads per project
    ver_cursor = db.conn.execute("SELECT project_id, id, game_versions, downloads FROM versions")
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

    # Major versions only (filter to x.y.z or x.y format)
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
        "## Game Version Popularity",
        "",
        "| Version | Project Count | Total Downloads |",
        "|---------|--------------|----------------|"
    ]

    for ver in sorted(major_versions.keys(), key=lambda v: major_versions[v]["count"], reverse=True)[:20]:
        data = major_versions[ver]
        lines.append(
            f"| {ver} | {data['count']} | {data['downloads']:,} |"
        )

    return "\n".join(lines)


def main():
    print("=== Phase 5: Analyze ===")

    today = get_current_date()
    db = Database("data/modrinth_tracker.db")

    # Generate all report sections
    sections = [
        f"# Modrinth Daily Report - {today}",
        "",
        generate_category_rankings(db, today),
        "",
        generate_category_trends(db, today),
        "",
        generate_top_growing_projects(db, today),
        "",
        generate_loader_popularity(db),
        "",
        generate_game_version_popularity(db),
        ""
    ]

    report = "\n".join(sections)

    # Save report
    ensure_dir("reports")
    report_path = f"reports/daily_report_{today}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Saved report to {report_path}")

    # Save summary JSON
    categories = db.get_categories_for_date(today)
    summary = {
        "date": today,
        "categories": [
            {
                "category": c["category"],
                "total_downloads": c["total_downloads"],
                "project_count": c["project_count"],
                "avg_downloads": c["avg_downloads"],
                "new_downloads": c["total_new_downloads"]
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