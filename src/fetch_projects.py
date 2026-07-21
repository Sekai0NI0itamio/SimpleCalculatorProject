#!/usr/bin/env python3
"""
Phase 2: Fetch Projects
- Accepts --project-type and --chunk arguments
- Fetches all pages for that partition (for the given project type)
- Saves project data to data/{project_type}/chunks/projects_{chunk}.json
  and data/{project_type}/chunks/projects_{chunk}_compact.json
"""
import argparse
import json
import sys
import time

from utils import (
    MODRINTH_API_BASE, PAGE_SIZE, load_json, save_json, ensure_dir,
    create_session, rate_limit_sleep, get_project_type_dir
)


def fetch_page(session, facets, offset):
    """Fetch a single page of search results."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps(facets),
        "limit": PAGE_SIZE,
        "offset": offset,
        "index": "downloads"
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    return resp.json()


def extract_project_data(hit):
    """Extract relevant fields from a search hit."""
    # Handle license: Modrinth API returns a license object {id, name, url}
    license_raw = hit.get("license")
    if isinstance(license_raw, dict):
        license_data = {"id": license_raw.get("id", ""), "name": license_raw.get("name", "")}
    elif isinstance(license_raw, str):
        license_data = {"id": license_raw, "name": license_raw}
    else:
        license_data = {}

    return {
        "project_id": hit.get("project_id"),
        "slug": hit.get("slug", ""),
        "title": hit.get("title", ""),
        "description": hit.get("description", ""),
        "categories": json.dumps(hit.get("categories", [])),
        "client_side": hit.get("client_side"),
        "server_side": hit.get("server_side"),
        "project_type": hit.get("project_type", "mod"),
        "downloads": hit.get("downloads", 0),
        "follows": hit.get("follows", 0),
        "icon_url": hit.get("icon_url"),
        "date_created": hit.get("date_created"),
        "date_modified": hit.get("date_modified"),
        "status": hit.get("status", "unknown"),
        "issues_url": hit.get("issues_url", ""),
        "source_url": hit.get("source_url", ""),
        "wiki_url": hit.get("wiki_url", ""),
        "discord_url": hit.get("discord_url", ""),
        "license": license_data,
        "versions": hit.get("versions", []),
    }


def extract_compact_project_data(hit):
    """Extract minimal fields for quick snapshot."""
    return {
        "project_id": hit.get("project_id"),
        "slug": hit.get("slug", ""),
        "downloads": hit.get("downloads", 0)
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch projects for a partition chunk")
    parser.add_argument(
        "--project-type", required=True,
        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "plugin"],
        help="Project type to fetch"
    )
    parser.add_argument("--chunk", type=int, required=True, help="Partition index to fetch")
    args = parser.parse_args()

    project_type = args.project_type
    chunk = args.chunk
    print(f"=== Phase 2: Fetch Projects ({project_type}, chunk {chunk}) ===")

    type_dir = get_project_type_dir(project_type)

    # Load discovery data
    discovery = load_json(f"{type_dir}/discovery.json")
    if not discovery:
        print(f"Error: {type_dir}/discovery.json not found. Run discover.py first.")
        return 1

    # Find the partition
    partition = None
    for p in discovery["partitions"]:
        if p["index"] == chunk:
            partition = p
            break

    if not partition:
        print(f"Error: Partition with index {chunk} not found in {type_dir}/discovery.json")
        return 1

    print(f"Fetching partition: category={partition.get('category')}, "
          f"loader={partition.get('loader', 'N/A')}, "
          f"pages={partition['pages']}")

    session = create_session()
    all_projects = []
    compact_projects = []

    facets = partition["facets"]
    total_pages = partition["pages"]

    for page in range(total_pages):
        offset = page * PAGE_SIZE
        if offset >= 10000:
            print(f"  Skipping page {page + 1}: offset {offset} exceeds API limit of 10000")
            break

        try:
            data = fetch_page(session, facets, offset)
            hits = data.get("hits", [])

            if not hits:
                print(f"  Page {page + 1}/{total_pages}: no results (stopping)")
                break

            for hit in hits:
                all_projects.append(extract_project_data(hit))
                compact_projects.append(extract_compact_project_data(hit))

            print(f"  Page {page + 1}/{total_pages}: {len(hits)} projects (offset={offset})")

            # Small delay between pages to be nice to the API
            time.sleep(0.2)

        except Exception as e:
            print(f"  Error fetching page {page + 1} (offset={offset}): {e}")
            continue

    print(f"Fetched {len(all_projects)} projects total")

    # Save full data
    chunks_dir = f"{type_dir}/chunks"
    ensure_dir(chunks_dir)
    output_path = f"{chunks_dir}/projects_{chunk}.json"
    save_json(output_path, all_projects)
    print(f"Saved full project data to {output_path}")

    # Save compact data
    compact_path = f"{chunks_dir}/projects_{chunk}_compact.json"
    save_json(compact_path, compact_projects)
    print(f"Saved compact project data to {compact_path}")

    print(f"=== Fetch projects ({project_type}) chunk {chunk} complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
