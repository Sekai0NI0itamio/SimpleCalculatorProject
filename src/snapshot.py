#!/usr/bin/env python3
"""
Phase 4: Snapshot
- Reads all project data from data/{project_type}/chunks/ (deduplicated by project_id)
- Reads merged version data from data/{project_type}/versions_merged.json.gz (if present)
- Saves a raw snapshot to data/{project_type}/raw/{timestamp}.json.gz (compressed)
  Fields per project: project_id, slug, title, downloads, follows, categories, project_type
  Fields per version: version_id, project_id, version_number, loaders, game_versions, downloads

NO DB usage — all data is derived from raw snapshots in analyze.py.
The raw snapshot is the single source of truth.
"""
import argparse
import glob
import json
import os
import sys

from utils import (
    load_json, save_json, ensure_dir, get_current_date, get_timestamp,
    get_project_type_dir, get_raw_dir
)


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


def load_merged_versions(project_type):
    """Load merged version data from versions_merged.json.gz.
    Returns list of version dicts or empty list if file doesn't exist.
    """
    type_dir = get_project_type_dir(project_type)
    merged_path = f"{type_dir}/versions_merged.json.gz"
    if not os.path.exists(merged_path):
        # Try uncompressed version (legacy)
        merged_path = f"{type_dir}/versions_merged.json"
        if not os.path.exists(merged_path):
            return []
    data = load_json(merged_path)
    if not data:
        return []
    return data.get("versions", [])


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
        raw_path = f"{raw_dir}/{timestamp}.json.gz"
        save_json(raw_path, raw_snapshot, compress=True)
        print(f"Saved empty raw snapshot to {raw_path}")
        print(f"=== Snapshot ({project_type}) complete ===")
        return 0

    # ── Build project entries ────────────────────────────────────
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

    # ── Load version data from merged file ───────────────────────
    raw_versions = []
    merged_versions = load_merged_versions(project_type)
    if merged_versions:
        print(f"Loaded {len(merged_versions)} versions from versions_merged.json.gz")
        for v in merged_versions:
            # game_versions and loaders are already lists in the merged file
            gv = v.get("game_versions", [])
            ld = v.get("loaders", [])
            if isinstance(gv, str):
                try:
                    gv = json.loads(gv)
                except (json.JSONDecodeError, TypeError):
                    gv = []
            if isinstance(ld, str):
                try:
                    ld = json.loads(ld)
                except (json.JSONDecodeError, TypeError):
                    ld = []
            raw_versions.append({
                "version_id": v.get("id"),
                "project_id": v.get("project_id"),
                "version_number": v.get("version_number", ""),
                "loaders": ld,
                "game_versions": gv,
                "downloads": v.get("downloads", 0) or 0,
            })
        print(f"  Processed {len(raw_versions)} version entries")
    else:
        print("  No version data available (fetch-versions may have been skipped)")

    raw_snapshot = {
        "timestamp": timestamp,
        "date": today,
        "project_type": project_type,
        "project_count": len(raw_projects),
        "total_downloads": total_downloads,
        "projects": raw_projects,
        "versions": raw_versions,
        "version_count": len(raw_versions),
    }

    # Save raw snapshot compressed (gzip) to fit under GitHub's 100MB file size limit
    raw_dir = get_raw_dir(project_type)
    ensure_dir(raw_dir)
    raw_path = f"{raw_dir}/{timestamp}.json.gz"
    save_json(raw_path, raw_snapshot, compress=True)

    # Print summary
    raw_size = os.path.getsize(raw_path)
    print(f"Saved raw snapshot to {raw_path}")
    print(f"  Projects: {len(raw_projects):,}")
    print(f"  Versions: {len(raw_versions):,}")
    print(f"  Total downloads: {total_downloads:,}")
    print(f"  Compressed size: {raw_size / 1024 / 1024:.2f} MB")

    print(f"=== Snapshot ({project_type}) complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
