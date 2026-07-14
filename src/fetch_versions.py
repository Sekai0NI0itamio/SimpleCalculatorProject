#!/usr/bin/env python3
"""
Phase 3: Fetch Versions
- Reads all project IDs from all chunks
- For each project, fetches all versions
- Rate-limited: respects 300 req/min
- Saves version data to database and data/version_summary.json
"""
import glob
import json
import os
import sys
import time

from utils import (
    MODRINTH_API_BASE, RATE_LIMIT, load_json, save_json,
    create_session, rate_limit_sleep
)
from db import Database


def get_all_project_ids():
    """Read all project IDs from all chunk files."""
    project_ids = []
    chunk_files = glob.glob("data/chunks/projects_*.json")
    # Filter out _compact files
    chunk_files = [f for f in chunk_files if not f.endswith("_compact.json")]

    for chunk_file in sorted(chunk_files):
        projects = load_json(chunk_file)
        if projects:
            for p in projects:
                project_ids.append(p["project_id"])

    # Deduplicate while preserving order
    seen = set()
    unique_ids = []
    for pid in project_ids:
        if pid not in seen:
            seen.add(pid)
            unique_ids.append(pid)

    return unique_ids


def fetch_project_versions(session, project_id):
    """Fetch all versions for a project. Returns list of version dicts or None on 404."""
    url = f"{MODRINTH_API_BASE}/project/{project_id}/version"
    try:
        resp = session.get(url)
        if resp.status_code == 404:
            return None  # Project deleted or removed
        resp.raise_for_status()
        rate_limit_sleep(resp.headers)
        return resp.json()
    except Exception as e:
        print(f"    Error fetching versions for {project_id}: {e}")
        return None


def extract_version_data(version, project_id):
    """Extract relevant fields from a version dict."""
    files_data = []
    for f in version.get("files", []):
        files_data.append({
            "url": f.get("url"),
            "filename": f.get("filename"),
            "primary": f.get("primary", False)
        })

    return {
        "id": version.get("id"),
        "project_id": project_id,
        "version_number": version.get("version_number", ""),
        "name": version.get("name", ""),
        "version_type": version.get("version_type", "release"),
        "game_versions": json.dumps(version.get("game_versions", [])),
        "loaders": json.dumps(version.get("loaders", [])),
        "downloads": version.get("downloads", 0),
        "files": json.dumps(files_data),
        "date_published": version.get("date_published")
    }


def main():
    print("=== Phase 3: Fetch Versions ===")

    # Get all project IDs
    project_ids = get_all_project_ids()
    print(f"Found {len(project_ids)} unique projects across all chunks")

    if not project_ids:
        print("No projects found. Run fetch_projects.py first.")
        return 1

    # Open database
    db = Database("data/modrinth_tracker.db")

    # Track request timing for rate limiting
    request_timestamps = []
    session = create_session()

    version_summary = {}
    total_versions = 0
    failed_projects = 0
    skipped_projects = 0

    for i, project_id in enumerate(project_ids):
        # Rate limiting: ensure we don't exceed RATE_LIMIT requests per minute
        current_time = time.time()
        # Remove timestamps older than 60 seconds
        request_timestamps = [t for t in request_timestamps if current_time - t < 60]

        if len(request_timestamps) >= RATE_LIMIT:
            # Wait until we can make another request
            oldest = min(request_timestamps)
            wait_time = 60 - (current_time - oldest)
            if wait_time > 0:
                print(f"  Rate limit reached, waiting {wait_time:.1f}s...")
                time.sleep(wait_time + 0.5)
            # Clear old timestamps
            request_timestamps = []

        # Progress update
        if (i + 1) % 100 == 0:
            print(f"  Progress: {i + 1}/{len(project_ids)} projects processed")

        # Fetch versions
        request_timestamps.append(time.time())
        versions = fetch_project_versions(session, project_id)

        if versions is None:
            failed_projects += 1
            continue

        if len(versions) == 0:
            skipped_projects += 1
            continue

        # Store versions in database
        for version in versions:
            version_data = extract_version_data(version, project_id)
            db.upsert_version(version_data)
            total_versions += 1

        version_summary[project_id] = len(versions)

        # Small delay between projects
        time.sleep(0.1)

    db.close()

    print(f"\nResults:")
    print(f"  Projects processed: {len(project_ids)}")
    print(f"  Projects with versions: {len(project_ids) - failed_projects - skipped_projects}")
    print(f"  Failed (404/deleted): {failed_projects}")
    print(f"  Skipped (no versions): {skipped_projects}")
    print(f"  Total versions stored: {total_versions}")

    # Save version summary
    save_json("data/version_summary.json", {
        "total_projects": len(project_ids),
        "total_versions": total_versions,
        "failed_projects": failed_projects,
        "project_versions": version_summary
    })
    print("Saved version summary to data/version_summary.json")

    print("=== Fetch versions complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())