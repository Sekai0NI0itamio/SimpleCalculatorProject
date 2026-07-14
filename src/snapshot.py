#!/usr/bin/env python3
"""
Phase 4: Snapshot
- Reads all project data from chunks
- Records today's download counts for all projects and versions
- Calculates daily deltas and category stats
- Saves to database
"""
import glob
import json
import sys

from utils import load_json, save_json, get_current_date
from db import Database


def get_all_project_data():
    """Read all project data from all chunk files, deduplicating by project_id."""
    chunk_files = glob.glob("data/chunks/projects_*.json")
    chunk_files = [f for f in chunk_files if not f.endswith("_compact.json")]

    project_map = {}
    for chunk_file in sorted(chunk_files):
        projects = load_json(chunk_file)
        if projects:
            for p in projects:
                pid = p["project_id"]
                if pid not in project_map:
                    project_map[pid] = p

    return list(project_map.values())


def get_all_version_data():
    """Read all version data from the database."""
    # We'll read from DB directly in the main flow
    pass


def main():
    print("=== Phase 4: Snapshot ===")

    today = get_current_date()
    print(f"Taking snapshot for date: {today}")

    # Load all project data
    projects = get_all_project_data()
    print(f"Loaded {len(projects)} projects from chunks")

    if not projects:
        print("No projects found. Run fetch_projects.py first.")
        return 1

    # Open database
    db = Database("data/modrinth_tracker.db")

    # Record project snapshots
    project_count = 0
    for project in projects:
        project_id = project["project_id"]
        downloads = project.get("downloads", 0)
        follows = project.get("follows", 0)

        db.record_project_snapshot(project_id, today, downloads, follows)
        db.upsert_project(project)
        project_count += 1

    print(f"Recorded snapshots for {project_count} projects")

    # Record version snapshots
    # Get all versions from database
    cursor = db.conn.execute("SELECT id, project_id, downloads FROM versions")
    versions = cursor.fetchall()
    version_count = 0
    for version in versions:
        version_id = version["id"]
        version_downloads = version["downloads"]
        db.record_version_snapshot(version_id, today, version_downloads)
        version_count += 1

    print(f"Recorded snapshots for {version_count} versions")

    # Calculate category stats
    # Group projects by category
    category_projects = {}
    for project in projects:
        try:
            cats = json.loads(project.get("categories", "[]"))
        except (json.JSONDecodeError, TypeError):
            cats = []

        for cat in cats:
            if cat not in category_projects:
                category_projects[cat] = []
            category_projects[cat].append(project)

    # Get yesterday's category stats for comparison
    yesterday_cursor = db.conn.execute(
        "SELECT category, total_downloads FROM daily_category_stats WHERE date < ? ORDER BY date DESC LIMIT 100",
        (today,)
    )
    yesterday_stats = {}
    for row in yesterday_cursor.fetchall():
        if row["category"] not in yesterday_stats:
            yesterday_stats[row["category"]] = row["total_downloads"]

    # Calculate and record category stats
    for cat, cat_projects in category_projects.items():
        total_downloads = sum(p.get("downloads", 0) for p in cat_projects)
        project_count = len(cat_projects)
        avg_downloads = total_downloads / project_count if project_count > 0 else 0.0

        # Calculate new downloads compared to yesterday
        yesterday_downloads = yesterday_stats.get(cat, 0)
        new_downloads = max(0, total_downloads - yesterday_downloads)

        db.record_category_stats(
            cat, today, total_downloads, project_count, avg_downloads, new_downloads
        )

    print(f"Recorded stats for {len(category_projects)} categories")

    db.close()

    # Save snapshot date
    with open("data/snapshot_date.txt", "w") as f:
        f.write(today)
    print(f"Saved snapshot date to data/snapshot_date.txt")

    print("=== Snapshot complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())