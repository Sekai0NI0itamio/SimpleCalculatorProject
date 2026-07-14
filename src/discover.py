#!/usr/bin/env python3
"""
Phase 1: Discover
- Query total project counts for ALL project types (mod, modpack,
  resourcepack, shader, datapack, world)
- Fetch all categories and all loaders from the Modrinth tag API
- For each (project_type, category) pair, count projects
- Create partition plan (subdividing by loader if > MAX_OFFSET)
- Save to data/discovery.json, data/categories.json, data/loaders.json
"""
import json
import math
import sys
import time

from utils import (
    MODRINTH_API_BASE, PAGE_SIZE, MAX_OFFSET,
    create_session, rate_limit_sleep, save_json, get_current_datetime
)

# All project types to collect — the user wants EVERYTHING, not just mods
ALL_PROJECT_TYPES = ["mod", "modpack", "resourcepack", "shader", "datapack", "world"]

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


def create_partition_plan(session, categories_by_type):
    """Create partition plan across ALL project types.
    Subdivides large categories by loader (and game version if needed).

    categories_by_type: dict of {project_type: [(slug, count), ...]}
    """
    partitions = []
    index = 0

    for project_type in ALL_PROJECT_TYPES:
        cat_counts = categories_by_type.get(project_type, [])
        if not cat_counts:
            continue

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
    print("=== Phase 1: Discover (ALL project types) ===")
    session = create_session()

    # ── Fetch total hits per project type ─────────────────────────
    total_hits_all = 0
    hits_by_type = {}
    for ptype in ALL_PROJECT_TYPES:
        count = fetch_total_hits(session, ptype)
        hits_by_type[ptype] = count
        total_hits_all += count
        print(f"  {ptype}: {count:,} projects")
    print(f"Total across all types: {total_hits_all:,}")

    # ── Fetch ALL categories and loaders from tag API ──────────────
    print("Fetching all categories...")
    all_categories = fetch_all_categories(session)
    print(f"Found {len(all_categories)} category tags")

    print("Fetching all loaders...")
    all_loaders = fetch_all_loaders(session)
    loader_names = sorted([l["name"] for l in all_loaders])
    print(f"Found {len(loader_names)} loaders: {', '.join(loader_names)}")

    # ── Group categories by project_type ───────────────────────────
    # Each category tag has a "project_type" field (e.g. "mod", "resourcepack")
    categories_by_type = {}
    for ptype in ALL_PROJECT_TYPES:
        categories_by_type[ptype] = []

    for cat in all_categories:
        ptype = cat.get("project_type", "mod")
        if ptype in categories_by_type:
            categories_by_type[ptype].append(cat)

    # ── For each (project_type, category), get project count ───────
    categories_with_counts_by_type = {}
    for ptype in ALL_PROJECT_TYPES:
        cats = categories_by_type.get(ptype, [])
        if not cats:
            continue
        print(f"Counting projects per category for {ptype}...")
        result = []
        for cat in cats:
            slug = cat.get("name") or cat.get("slug") or ""
            if not slug:
                continue
            count = fetch_category_count(session, ptype, slug)
            result.append((slug, count))
            print(f"    {ptype}/{slug}: {count:,}")
            time.sleep(0.1)
        categories_with_counts_by_type[ptype] = result

    # ── Create partition plan ─────────────────────────────────────
    print("Creating partition plan...")
    partitions = create_partition_plan(session, categories_with_counts_by_type)
    print(f"Created {len(partitions)} partitions")

    # ── Save discovery data ───────────────────────────────────────
    discovery_data = {
        "total_hits": total_hits_all,
        "hits_by_type": hits_by_type,
        "project_types": ALL_PROJECT_TYPES,
        "fetched_at": get_current_datetime(),
        "partitions": partitions
    }
    save_json("data/discovery.json", discovery_data)
    print("Saved discovery plan to data/discovery.json")

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
        for c in all_categories
    ]
    save_json("data/categories.json", categories_data)
    print("Saved categories to data/categories.json")

    # ── Save loader names (used to filter loaders from categories) ─
    save_json("data/loaders.json", loader_names)
    print("Saved loader names to data/loaders.json")

    print("=== Discover complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
