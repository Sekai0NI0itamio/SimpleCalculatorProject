#!/usr/bin/env python3
"""
Phase 1: Discover
- Query total project count for a SINGLE project type (mod, modpack,
  resourcepack, shader, datapack, world)
- Fetch all categories and all loaders from the Modrinth tag API
- For each (project_type, category) pair, count projects
- Create partition plan (subdividing by loader if > MAX_OFFSET)
- Save to data/{project_type}/discovery.json, data/{project_type}/categories.json,
  data/{project_type}/loaders.json

This script now handles ONE project type per invocation.
"""
import argparse
import json
import math
import sys
import time

from utils import (
    MODRINTH_API_BASE, PAGE_SIZE, MAX_OFFSET,
    create_session, rate_limit_sleep, save_json, get_current_datetime,
    get_project_type_dir, ensure_dir
)

# Loaders used to subdivide large mod categories
COMMON_LOADERS = ["fabric", "forge", "neoforge", "quilt"]

# Major Minecraft versions for further subdivision
MAJOR_VERSIONS = [
    "1.20.1", "1.20.4", "1.21", "1.21.1", "1.21.3", "1.21.4",
    "1.19.2", "1.19.4", "1.18.2", "1.17.1", "1.16.5", "1.12.2"
]


def fetch_total_hits(session, project_type):
    """Get total number of projects for a given project type."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps([["project_type:" + project_type]]),
        "limit": 1,
        "offset": 0
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    data = resp.json()
    return data.get("total_hits", 0)


def fetch_all_categories(session):
    """Fetch ALL categories from /tag/category (across all project types).
    Returns the raw list — each entry has name, header, project_type, icon."""
    url = f"{MODRINTH_API_BASE}/tag/category"
    resp = session.get(url)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    return resp.json()


def fetch_all_loaders(session):
    """Fetch ALL loaders from /tag/loader.
    Returns list of {name, icon, supported_project_types}."""
    url = f"{MODRINTH_API_BASE}/tag/loader"
    resp = session.get(url)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    return resp.json()


def fetch_category_count(session, project_type, category_slug):
    """Get project count for a specific project_type + category."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps([
            ["project_type:" + project_type],
            ["categories:" + category_slug]
        ]),
        "limit": 1,
        "offset": 0
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    data = resp.json()
    return data.get("total_hits", 0)


def fetch_loader_count(session, project_type, category_slug, loader):
    """Get project count for a project_type + category + loader combination."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps([
            ["project_type:" + project_type],
            ["categories:" + category_slug],
            ["loader:" + loader]
        ]),
        "limit": 1,
        "offset": 0
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    data = resp.json()
    return data.get("total_hits", 0)


def fetch_loader_version_count(session, project_type, category_slug, loader, version):
    """Get project count for a project_type+category+loader+game version combo."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps([
            ["project_type:" + project_type],
            ["categories:" + category_slug],
            ["loader:" + loader],
            ["versions:" + version]
        ]),
        "limit": 1,
        "offset": 0
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    data = resp.json()
    return data.get("total_hits", 0)


def create_partition_plan(session, project_type, cat_counts):
    """Create partition plan for a single project type.
    Subdivides large categories by loader (and game version if needed).

    cat_counts: list of [(slug, count), ...] for this project type only.
    """
    partitions = []
    index = 0

    print(f"  Planning partitions for {project_type} ({len(cat_counts)} categories)...")

    for cat_slug, count in cat_counts:
        if count == 0:
            continue

        if count <= MAX_OFFSET:
            # Single partition for this project_type + category
            pages = math.ceil(count / PAGE_SIZE) if count > 0 else 1
            partitions.append({
                "index": index,
                "facets": [
                    ["project_type:" + project_type],
                    ["categories:" + cat_slug]
                ],
                "pages": pages,
                "category": cat_slug,
                "project_type": project_type
            })
            index += 1
        else:
            # Subdivide by loader (only for mod project type —
            # loaders don't apply to resourcepacks/shaders/etc.)
            if project_type == "mod":
                for loader in COMMON_LOADERS:
                    loader_count = fetch_loader_count(session, project_type, cat_slug, loader)
                    if loader_count == 0:
                        continue

                    if loader_count <= MAX_OFFSET:
                        pages = math.ceil(loader_count / PAGE_SIZE) if loader_count > 0 else 1
                        partitions.append({
                            "index": index,
                            "facets": [
                                ["project_type:" + project_type],
                                ["categories:" + cat_slug],
                                ["loader:" + loader]
                            ],
                            "pages": pages,
                            "category": cat_slug,
                            "project_type": project_type,
                            "loader": loader
                        })
                        index += 1
                    else:
                        # Subdivide further by game versions
                        for version in MAJOR_VERSIONS:
                            ver_count = fetch_loader_version_count(
                                session, project_type, cat_slug, loader, version
                            )
                            if ver_count == 0:
                                continue
                            pages = math.ceil(ver_count / PAGE_SIZE) if ver_count > 0 else 1
                            partitions.append({
                                "index": index,
                                "facets": [
                                    ["project_type:" + project_type],
                                    ["categories:" + cat_slug],
                                    ["loader:" + loader],
                                    ["versions:" + version]
                                ],
                                "pages": pages,
                                "category": cat_slug,
                                "project_type": project_type,
                                "loader": loader,
                                "game_version": version
                            })
                            index += 1
            else:
                # For non-mod types, just take what we can (first 10000)
                pages = math.ceil(MAX_OFFSET / PAGE_SIZE)
                partitions.append({
                    "index": index,
                    "facets": [
                        ["project_type:" + project_type],
                        ["categories:" + cat_slug]
                    ],
                    "pages": pages,
                    "category": cat_slug,
                    "project_type": project_type
                })
                index += 1

    return partitions


def main():
    parser = argparse.ArgumentParser(description="Discover projects for a single project type")
    parser.add_argument(
        "--project-type", required=True,
        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "world"],
        help="Project type to discover"
    )
    args = parser.parse_args()

    project_type = args.project_type
    print(f"=== Phase 1: Discover ({project_type}) ===")
    session = create_session()

    # ── Fetch total hits for this project type ───────────────────
    total_hits = fetch_total_hits(session, project_type)
    print(f"  {project_type}: {total_hits:,} projects")

    # ── Fetch ALL categories and loaders from tag API ──────────────
    print("Fetching all categories...")
    all_categories = fetch_all_categories(session)
    print(f"Found {len(all_categories)} category tags")

    print("Fetching all loaders...")
    all_loaders = fetch_all_loaders(session)
    loader_names = sorted([l["name"] for l in all_loaders])
    print(f"Found {len(loader_names)} loaders: {', '.join(loader_names)}")

    # ── Filter categories to this project type ────────────────────
    # Each category tag has a "project_type" field (e.g. "mod", "resourcepack")
    type_categories = [
        cat for cat in all_categories
        if cat.get("project_type", "mod") == project_type
    ]
    print(f"Categories for {project_type}: {len(type_categories)}")

    # ── For each category, get project count ──────────────────────
    print(f"Counting projects per category for {project_type}...")
    cat_counts = []
    for cat in type_categories:
        slug = cat.get("name") or cat.get("slug") or ""
        if not slug:
            continue
        count = fetch_category_count(session, project_type, slug)
        cat_counts.append((slug, count))
        print(f"    {project_type}/{slug}: {count:,}")
        time.sleep(0.1)

    # ── Create partition plan ─────────────────────────────────────
    print("Creating partition plan...")
    if cat_counts:
        partitions = create_partition_plan(session, project_type, cat_counts)
    else:
        # No categories for this project type (e.g. datapack, world).
        # Create a single partition that fetches ALL projects of this type.
        print(f"  No categories for {project_type} — creating single bulk partition")
        pages = math.ceil(total_hits / PAGE_SIZE) if total_hits > 0 else 1
        # Cap at MAX_OFFSET (10000 results per API limit)
        pages = min(pages, math.ceil(MAX_OFFSET / PAGE_SIZE))
        partitions = [{
            "index": 0,
            "facets": [["project_type:" + project_type]],
            "pages": pages,
            "project_type": project_type,
            "category": None
        }]
    print(f"Created {len(partitions)} partitions")

    # ── Save discovery data ───────────────────────────────────────
    type_dir = get_project_type_dir(project_type)
    ensure_dir(type_dir)

    discovery_data = {
        "project_type": project_type,
        "total_hits": total_hits,
        "fetched_at": get_current_datetime(),
        "partitions": partitions
    }
    discovery_path = f"{type_dir}/discovery.json"
    save_json(discovery_path, discovery_data)
    print(f"Saved discovery plan to {discovery_path}")

    # ── Save categories data (with header field for filtering) ────
    # The "header" field distinguishes content categories ("categories")
    # from resolutions ("resolutions"), features ("features"), etc.
    categories_data = [
        {
            "slug": c.get("name") or c.get("slug", ""),
            "name": c.get("name", ""),
            "header": c.get("header", ""),
            "icon": c.get("icon"),
            "project_type": c.get("project_type", "mod")
        }
        for c in type_categories
    ]
    categories_path = f"{type_dir}/categories.json"
    save_json(categories_path, categories_data)
    print(f"Saved categories to {categories_path}")

    # ── Save loader names (used to filter loaders from categories) ─
    loaders_path = f"{type_dir}/loaders.json"
    save_json(loaders_path, loader_names)
    print(f"Saved loader names to {loaders_path}")

    print(f"=== Discover ({project_type}) complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
