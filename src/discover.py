#!/usr/bin/env python3
"""
Phase 1: Discover
- Query total mod count
- Fetch all categories
- For each category, count projects
- Create partition plan (subdividing by loader if > MAX_OFFSET)
- Save to data/discovery.json and data/categories.json
"""
import json
import math
import sys
import time

from utils import (
    MODRINTH_API_BASE, PAGE_SIZE, MAX_OFFSET,
    create_session, rate_limit_sleep, save_json, get_current_datetime
)

# Common loaders to subdivide large categories
COMMON_LOADERS = ["fabric", "forge", "neoforge", "quilt"]

# Major Minecraft versions for further subdivision
MAJOR_VERSIONS = [
    "1.20.1", "1.20.4", "1.21", "1.21.1", "1.21.3", "1.21.4",
    "1.19.2", "1.19.4", "1.18.2", "1.17.1", "1.16.5", "1.12.2"
]


def fetch_total_hits(session):
    """Get total number of mod projects."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps([["project_type:mod"]]),
        "limit": 1,
        "offset": 0
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    data = resp.json()
    return data.get("total_hits", 0)


def fetch_categories(session):
    """Fetch all categories and filter for mod project_type."""
    url = f"{MODRINTH_API_BASE}/tag/category"
    resp = session.get(url)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    categories = resp.json()
    # Filter for mod categories
    mod_categories = [c for c in categories if c.get("project_type") == "mod"]
    return mod_categories


def fetch_category_count(session, category_slug):
    """Get project count for a specific category."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps([["project_type:mod"], ["categories:" + category_slug]]),
        "limit": 1,
        "offset": 0
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    data = resp.json()
    return data.get("total_hits", 0)


def fetch_loader_count(session, category_slug, loader):
    """Get project count for a category+loader combination."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps([["project_type:mod"], ["categories:" + category_slug], ["loader:" + loader]]),
        "limit": 1,
        "offset": 0
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    rate_limit_sleep(resp.headers)
    data = resp.json()
    return data.get("total_hits", 0)


def fetch_loader_version_count(session, category_slug, loader, version):
    """Get project count for a category+loader+game version combination."""
    url = f"{MODRINTH_API_BASE}/search"
    params = {
        "facets": json.dumps([
            ["project_type:mod"],
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


def create_partition_plan(session, categories_with_counts):
    """Create partition plan, subdividing large categories by loader."""
    partitions = []
    index = 0

    for cat_slug, count in categories_with_counts:
        if count <= MAX_OFFSET:
            # Single partition for this category
            pages = math.ceil(count / PAGE_SIZE) if count > 0 else 1
            partitions.append({
                "index": index,
                "facets": [["project_type:mod"], ["categories:" + cat_slug]],
                "pages": pages,
                "category": cat_slug
            })
            index += 1
        else:
            # Subdivide by loader
            for loader in COMMON_LOADERS:
                loader_count = fetch_loader_count(session, cat_slug, loader)
                if loader_count == 0:
                    continue

                if loader_count <= MAX_OFFSET:
                    pages = math.ceil(loader_count / PAGE_SIZE) if loader_count > 0 else 1
                    partitions.append({
                        "index": index,
                        "facets": [["project_type:mod"], ["categories:" + cat_slug], ["loader:" + loader]],
                        "pages": pages,
                        "category": cat_slug,
                        "loader": loader
                    })
                    index += 1
                else:
                    # Subdivide further by game versions
                    for version in MAJOR_VERSIONS:
                        ver_count = fetch_loader_version_count(session, cat_slug, loader, version)
                        if ver_count == 0:
                            continue
                        pages = math.ceil(ver_count / PAGE_SIZE) if ver_count > 0 else 1
                        partitions.append({
                            "index": index,
                            "facets": [
                                ["project_type:mod"],
                                ["categories:" + cat_slug],
                                ["loader:" + loader],
                                ["versions:" + version]
                            ],
                            "pages": pages,
                            "category": cat_slug,
                            "loader": loader,
                            "game_version": version
                        })
                        index += 1

    return partitions


def main():
    print("=== Phase 1: Discover ===")
    session = create_session()

    # Fetch total hits
    print("Fetching total mod count...")
    total_hits = fetch_total_hits(session)
    print(f"Total mods: {total_hits}")

    # Fetch categories
    print("Fetching categories...")
    categories = fetch_categories(session)
    print(f"Found {len(categories)} mod categories")

    # For each category, get count
    # Note: The API response uses "name" as the slug string
    categories_with_counts = []
    for cat in categories:
        slug = cat.get("name") or cat.get("slug") or ""
        if not slug:
            print(f"  Skipping category with no slug: {cat.get('header', 'unknown')}")
            continue
        count = fetch_category_count(session, slug)
        categories_with_counts.append((slug, count))
        print(f"  Category '{slug}': {count} projects")
        # Small delay to avoid hammering the API
        time.sleep(0.1)

    # Create partition plan
    print("Creating partition plan...")
    partitions = create_partition_plan(session, categories_with_counts)
    print(f"Created {len(partitions)} partitions")

    # Build discovery data
    discovery_data = {
        "total_hits": total_hits,
        "fetched_at": get_current_datetime(),
        "partitions": partitions
    }

    # Save discovery data
    save_json("data/discovery.json", discovery_data)
    print("Saved discovery plan to data/discovery.json")

    # Save categories data
    categories_data = [
        {
            "slug": c.get("name") or c.get("slug", ""),
            "name": c.get("name", ""),
            "header": c.get("header", ""),
            "icon": c.get("icon"),
            "project_type": c.get("project_type", "mod")
        }
        for c in categories
    ]
    save_json("data/categories.json", categories_data)
    print("Saved categories to data/categories.json")

    print("=== Discover complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())