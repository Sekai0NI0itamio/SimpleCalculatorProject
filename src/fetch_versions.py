#!/usr/bin/env python3
"""
Phase 3: Fetch Versions (parallel)

Modes:
  --split N        Split all projects into N version chunks (run after fetch-projects)
  --chunk N        Fetch versions for chunk N (run in parallel GitHub Actions jobs)
  --merge          Merge all chunk results into the database (run after all chunks done)

All paths are scoped to data/{project_type}/.
"""
import glob
import json
import os
import sys
import time
import argparse

from utils import (
    MODRINTH_API_BASE, RATE_LIMIT, load_json, save_json,
    create_session, rate_limit_sleep, ensure_dir, get_project_type_dir
)
from db import Database


def get_all_project_ids(project_type):
    """Read all project IDs from all chunk files (deduplicated) for a project type."""
    type_dir = get_project_type_dir(project_type)
    project_ids = []
    chunk_files = glob.glob(f"{type_dir}/chunks/projects_*.json")
    chunk_files = [f for f in chunk_files if not f.endswith("_compact.json")]

    for chunk_file in sorted(chunk_files):
        projects = load_json(chunk_file)
        if projects:
            for p in projects:
                project_ids.append(p["project_id"])

    seen = set()
    unique = []
    for pid in project_ids:
        if pid not in seen:
            seen.add(pid)
            unique.append(pid)
    return unique


def split_projects_into_chunks(project_type, num_chunks: int = 10):
    """Split all project IDs into N version chunks and save to disk.

    If num_chunks > total projects, each project gets its own chunk (1:1).
    If num_chunks > 256, cap at 256 (GitHub Actions matrix limit).
    """
    type_dir = get_project_type_dir(project_type)
    version_chunks_dir = f"{type_dir}/version_chunks"
    ensure_dir(version_chunks_dir)

    project_ids = get_all_project_ids(project_type)
    total = len(project_ids)
    print(f"Total projects ({project_type}): {total}")

    # Cap at GitHub Actions limit and ensure we don't exceed project count
    num_chunks = min(num_chunks, 256, total)
    if num_chunks == 0:
        num_chunks = 1

    chunk_size = (total + num_chunks - 1) // num_chunks
    chunks_info = []

    for i in range(num_chunks):
        start = i * chunk_size
        end = start + chunk_size
        chunk = project_ids[start:end]
        if chunk:
            save_json(f"{version_chunks_dir}/version_chunk_{i}.json", chunk)
            chunks_info.append({"index": i, "count": len(chunk)})

    print(f"  Chunks: {len(chunks_info)}")
    print(f"  Avg size: ~{chunk_size} projects per chunk")
    if chunk_size > 500:
        print(f"  WARNING: {chunk_size} projects per chunk may take a while")
    if chunk_size == 1:
        print(f"  (1:1 — each project is its own chunk)")

    save_json(f"{type_dir}/version_split.json", {"num_chunks": len(chunks_info), "chunks": chunks_info})
    print(f"Saved split plan to {type_dir}/version_split.json")


def fetch_versions_chunk(project_type, chunk_index: int):
    """Fetch versions for a single chunk."""
    print(f"=== Fetch Versions Chunk {chunk_index} ({project_type}) ===")
    type_dir = get_project_type_dir(project_type)
    chunk_path = f"{type_dir}/version_chunks/version_chunk_{chunk_index}.json"
    if not os.path.exists(chunk_path):
        print(f"ERROR: {chunk_path} not found")
        sys.exit(1)

    project_ids = load_json(chunk_path)
    print(f"Chunk {chunk_index}: {len(project_ids)} projects")

    results_dir = f"{type_dir}/version_results"
    ensure_dir(results_dir)
    request_timestamps = []
    session = create_session()
    results = []
    summary = {}
    errors = []

    for i, project_id in enumerate(project_ids):
        # Rate limiting
        now = time.time()
        request_timestamps = [t for t in request_timestamps if now - t < 60]
        if len(request_timestamps) >= RATE_LIMIT:
            oldest = min(request_timestamps)
            wait = 60 - (now - oldest)
            if wait > 0:
                print(f"  Rate limit reached, waiting {wait:.1f}s...")
                time.sleep(wait + 0.5)
            request_timestamps = []

        if (i + 1) % 100 == 0:
            print(f"  Progress: {i + 1}/{len(project_ids)}")

        request_timestamps.append(time.time())
        url = f"{MODRINTH_API_BASE}/project/{project_id}/version"
        try:
            resp = session.get(url)
            rate_limit_sleep(resp.headers)
            if resp.status_code == 404:
                errors.append({"project_id": project_id, "error": "404"})
                continue
            resp.raise_for_status()
            versions = resp.json()
        except Exception as e:
            print(f"    Error fetching {project_id}: {e}")
            errors.append({"project_id": project_id, "error": str(e)})
            continue

        for v in versions:
            files_data = [{"url": f.get("url"), "filename": f.get("filename"), "primary": f.get("primary", False)} for f in v.get("files", [])]
            results.append({
                "id": v.get("id"),
                "project_id": project_id,
                "version_number": v.get("version_number", ""),
                "name": v.get("name", ""),
                "version_type": v.get("version_type", "release"),
                "game_versions": v.get("game_versions", []),
                "loaders": v.get("loaders", []),
                "downloads": v.get("downloads", 0),
                "files": files_data,
                "date_published": v.get("date_published"),
            })

        summary[project_id] = len(versions)

    output = f"{results_dir}/results_{chunk_index}.json"
    save_json(output, {
        "chunk_index": chunk_index,
        "project_ids": project_ids,
        "version_summary": summary,
        "errors": errors,
        "versions": results,
    })
    print(f"Chunk {chunk_index}: {len(results)} versions across {len(summary)} projects, {len(errors)} errors")


def merge_all_chunks(project_type):
    """Merge all version chunk results into the SQLite database."""
    print(f"=== Merge Version Chunks ({project_type}) ===")
    type_dir = get_project_type_dir(project_type)
    results_dir = f"{type_dir}/version_results"
    ensure_dir(results_dir)
    db = Database(project_type)

    total_versions = 0
    total_projects = 0
    combined_summary = {}
    all_errors = []

    for f in sorted(glob.glob(f"{results_dir}/results_*.json")):
        data = load_json(f)
        versions = data.get("versions", [])
        summary = data.get("version_summary", {})
        errors = data.get("errors", [])

        for v in versions:
            files_data = v.get("files", [])
            db.upsert_version({
                "id": v["id"],
                "project_id": v["project_id"],
                "version_number": v["version_number"],
                "name": v["name"],
                "version_type": v["version_type"],
                "game_versions": json.dumps(v["game_versions"]),
                "loaders": json.dumps(v["loaders"]),
                "downloads": v["downloads"],
                "files": json.dumps(files_data),
                "date_published": v["date_published"],
            })
            total_versions += 1

        combined_summary.update(summary)
        total_projects += len(summary)
        all_errors.extend(errors)

    db.conn.commit()
    print(f"Merged {total_versions} versions across {total_projects} projects")
    if all_errors:
        print(f"Total errors: {len(all_errors)}")

    save_json(f"{type_dir}/version_summary.json", {
        "total_projects": total_projects,
        "total_versions": total_versions,
        "errors": len(all_errors),
        "project_versions": combined_summary,
    })

    db.close()
    print(f"=== Merge Complete ({project_type}) ===")


def main():
    parser = argparse.ArgumentParser(description="Fetch versions (parallel mode)")
    parser.add_argument(
        "--project-type", required=True,
        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "world"],
        help="Project type"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--split", type=int, help="Split all projects into N version chunks")
    group.add_argument("--chunk", type=int, help="Fetch versions for this chunk index")
    group.add_argument("--merge", action="store_true", help="Merge all chunk results into DB")
    args = parser.parse_args()

    if args.split is not None:
        split_projects_into_chunks(args.project_type, args.split)
    elif args.chunk is not None:
        fetch_versions_chunk(args.project_type, args.chunk)
    elif args.merge:
        merge_all_chunks(args.project_type)

    return 0


if __name__ == "__main__":
    sys.exit(main())
