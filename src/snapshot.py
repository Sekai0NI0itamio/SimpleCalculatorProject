#!/usr/bin/env python3
"""
Phase 4: Snapshot
- Reads all project data from chunks and all version data from the DB
- Records today's download counts for EVERY project and EVERY version
- FIRST RUN: marks today as the baseline date (day 0)
- SUBSEQUENT RUNS: records snapshots, category stats computed by
  comparing against the baseline
- Saves to database
"""
import glob
import json
import sys

from utils import load_json, get_current_date
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

    # ── Baseline tracking ─────────────────────────────────────────
    baseline_date = db.get_baseline_date()
    is_first_run = baseline_date is None

    if is_first_run:
        print(f"FIRST RUN — setting baseline date to {today}")
        db.set_baseline_date(today)
        baseline_date = today
    else:
        print(f"Baseline date: {baseline_date}")

    # ── Record project snapshots ──────────────────────────────────
    project_count = 0
    for project in projects:
        project_id = project["project_id"]
        downloads = project.get("downloads", 0)
        follows = project.get("follows", 0)

        db.record_project_snapshot(project_id, today, downloads, follows)
        db.upsert_project(project)
        project_count += 1

    print(f"Recorded snapshots for {project_count} projects")

    # ── Record version snapshots ──────────────────────────────────
    cursor = db.conn.execute("SELECT id, project_id, downloads FROM versions")
    versions = cursor.fetchall()
    version_count = 0
    for version in versions:
        version_id = version["id"]
        version_downloads = version["downloads"]
        db.record_version_snapshot(version_id, today, version_downloads)
        version_count += 1

    print(f"Recorded snapshots for {version_count} versions")

    # ── Calculate category stats (deltas from baseline) ───────────
    baseline_cat_stats = db.get_baseline_category_stats()
    baseline_project_snaps = db.get_baseline_project_snapshots()

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

    # Calculate and record category stats
    for cat, cat_projects in category_projects.items():
        total_downloads = sum(p.get("downloads", 0) for p in cat_projects)
        project_count_cat = len(cat_projects)
        avg_downloads = total_downloads / project_count_cat if project_count_cat > 0 else 0.0

        if is_first_run:
            # On first run, new_downloads = total_downloads (baseline)
            new_downloads = total_downloads
        else:
            # On subsequent runs, new_downloads = delta from baseline
            baseline_total = baseline_cat_stats.get(cat, {}).get("total_downloads", 0)
            new_downloads = max(0, total_downloads - baseline_total)

        db.record_category_stats(
            cat, today, total_downloads, project_count_cat, avg_downloads, new_downloads
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