#!/usr/bin/env python3
"""
Phase 4: Snapshot
- Reads all project data from data/{project_type}/chunks/ (deduplicated by project_id)
- Saves a raw snapshot to data/{project_type}/raw/{timestamp}.json
  (compact: project_id, slug, title, downloads, follows, categories, project_type)
- Updates the DB: upserts all projects and records daily project/version snapshots

NO baseline logic here — analyze.py derives everything from the raw snapshot history.
"""
import argparse
import glob
import json
import sys

from utils import (
    load_json, save_json, ensure_dir, get_current_date, get_timestamp,
    get_project_type_dir, get_raw_dir
)
from db import Database


def get_all_project_data(project_type):
    """Read all project data from all chunk files, deduplicating by project_id."""
    type_dir = get_project_type_dir(project_type)
    chunk_files = glob.glob(f"{type_dir}/chunks/projects_*.json")
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
    parser = argparse.ArgumentParser(description="Take a daily snapshot for a project type")
    parser.add_argument(
        "--project-type", required=True,
        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "world"],
        help="Project type to snapshot"
    )
    args = parser.parse_args()

    project_type = args.project_type
    print(f"=== Phase 4: Snapshot ({project_type}) ===")

    today = get_current_date()
    timestamp = get_timestamp()
    print(f"Taking snapshot for date: {today} (timestamp: {timestamp})")

    # Load all project data
    projects = get_all_project_data(project_type)
    print(f"Loaded {len(projects)} projects from chunks")

    if not projects:
        print(f"No projects found for {project_type} — creating empty snapshot")
        # Still create an empty raw snapshot so analyze.py has data to work with
        raw_snapshot = {
            "timestamp": timestamp,
            "date": today,
            "project_type": project_type,
            "project_count": 0,
            "total_downloads": 0,
            "projects": [],
            "versions": [],
            "version_count": 0,
        }
        raw_dir = get_raw_dir(project_type)
        ensure_dir(raw_dir)
        raw_path = f"{raw_dir}/{timestamp}.json"
        save_json(raw_path, raw_snapshot)
        print(f"Saved empty raw snapshot to {raw_path}")
        print(f"=== Snapshot ({project_type}) complete ===")
        return 0

    # ── Build raw snapshot (compact — no description/icon_url) ────
    raw_projects = []
    total_downloads = 0
    for p in projects:
        downloads = p.get("downloads", 0) or 0
        total_downloads += downloads

        # categories is stored as a JSON string in chunk files
        cats_raw = p.get("categories", "[]")
        if isinstance(cats_raw, str):
            try:
                cats = json.loads(cats_raw)
            except (json.JSONDecodeError, TypeError):
                cats = []
        elif isinstance(cats_raw, list):
            cats = cats_raw
        else:
            cats = []

        raw_projects.append({
            "project_id": p.get("project_id"),
            "slug": p.get("slug", ""),
            "title": p.get("title", ""),
            "downloads": downloads,
            "follows": p.get("follows", 0) or 0,
            "categories": cats,
            "project_type": p.get("project_type", project_type),
        })

    raw_snapshot = {
        "timestamp": timestamp,
        "date": today,
        "project_type": project_type,
        "project_count": len(raw_projects),
        "total_downloads": total_downloads,
        "projects": raw_projects,
    }

    # ── Load version data from DB (if available) ──────────────────
    # Version data is populated by fetch_versions.py --merge.
    # On sub-hour runs (no version fetch), the DB may have stale or no version data.
    db = Database(project_type)
    version_count = db.conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    if version_count > 0:
        print(f"Loading {version_count} versions from DB...")
        cursor = db.conn.execute(
            "SELECT id, project_id, version_number, loaders, game_versions, downloads FROM versions"
        )
        raw_versions = []
        for row in cursor.fetchall():
            try:
                loaders = json.loads(row["loaders"]) if row["loaders"] else []
            except (json.JSONDecodeError, TypeError):
                loaders = []
            try:
                game_versions = json.loads(row["game_versions"]) if row["game_versions"] else []
            except (json.JSONDecodeError, TypeError):
                game_versions = []
            raw_versions.append({
                "version_id": row["id"],
                "project_id": row["project_id"],
                "version_number": row["version_number"],
                "loaders": loaders,
                "game_versions": game_versions,
                "downloads": row["downloads"] or 0,
            })
        raw_snapshot["versions"] = raw_versions
        raw_snapshot["version_count"] = len(raw_versions)
        print(f"Added {len(raw_versions)} versions to snapshot")
    else:
        print("No version data in DB (fetch-versions may have been skipped)")

    raw_dir = get_raw_dir(project_type)
    ensure_dir(raw_dir)
    raw_path = f"{raw_dir}/{timestamp}.json"
    save_json(raw_path, raw_snapshot)
    print(f"Saved raw snapshot to {raw_path} ({len(raw_projects)} projects, {total_downloads:,} downloads)")

    # ── Update the database ───────────────────────────────────────
    # (DB already opened above for version loading)

    # Upsert all projects + record daily project snapshots
    project_count = 0
    for project in projects:
        project_id = project["project_id"]
        downloads = project.get("downloads", 0) or 0
        follows = project.get("follows", 0) or 0

        db.upsert_project(project)
        db.record_project_snapshot(project_id, today, downloads, follows)
        project_count += 1

    print(f"Recorded snapshots for {project_count} projects")

    # Record version snapshots from the DB's versions table
    cursor = db.conn.execute("SELECT id, downloads FROM versions")
    versions = cursor.fetchall()
    version_count = 0
    for version in versions:
        version_id = version["id"]
        version_downloads = version["downloads"] or 0
        db.record_version_snapshot(version_id, today, version_downloads)
        version_count += 1

    print(f"Recorded snapshots for {version_count} versions")

    db.close()

    print(f"=== Snapshot ({project_type}) complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
