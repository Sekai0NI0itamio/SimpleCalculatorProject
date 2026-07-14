#!/usr/bin/env python3
"""
Local runner script that runs all phases sequentially for a single project type.
Useful for testing and development.

Usage:
  python src/run_all.py --project-type mod
  python src/run_all.py --project-type modpack
"""
import argparse
import json
import subprocess
import sys


def run_phase(description, command):
    """Run a phase and print the result."""
    print(f"\n{'='*60}")
    print(f"Running: {description}")
    print(f"Command: {command}")
    print(f"{'='*60}\n")

    result = subprocess.run(command, shell=True, capture_output=False, text=True)

    if result.returncode != 0:
        print(f"\nERROR: {description} failed with exit code {result.returncode}")
        return False

    print(f"\n✓ {description} completed successfully")
    return True


def main():
    parser = argparse.ArgumentParser(description="Run the full tracker pipeline for one project type")
    parser.add_argument(
        "--project-type", required=True,
        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "world"],
        help="Project type to process"
    )
    parser.add_argument("--version-chunks", type=int, default=10, help="Number of version chunks")
    args = parser.parse_args()

    ptype = args.project_type
    vchunks = args.version_chunks

    print("=" * 60)
    print(f"Modrinth Project Tracker - Full Run ({ptype})")
    print("=" * 60)

    # Phase 1: Discover
    if not run_phase(f"Phase 1: Discover ({ptype})",
                     f"python src/discover.py --project-type {ptype}"):
        return 1

    # Load discovery to get partitions
    try:
        with open(f"data/{ptype}/discovery.json", "r") as f:
            discovery = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading discovery.json: {e}")
        return 1

    partitions = discovery.get("partitions", [])
    print(f"\nDiscovered {len(partitions)} partitions to fetch")

    # Phase 2: Fetch Projects for each partition
    for partition in partitions:
        chunk = partition["index"]
        cat = partition.get("category", "unknown")
        loader = partition.get("loader", "")
        desc = f"category={cat}"
        if loader:
            desc += f", loader={loader}"
        if not run_phase(f"Phase 2: Fetch Projects (chunk {chunk}, {desc})",
                         f"python src/fetch_projects.py --project-type {ptype} --chunk {chunk}"):
            print(f"Warning: Chunk {chunk} failed, continuing...")

    # Phase 3: Fetch Versions
    if not run_phase("Phase 3a: Split Versions",
                     f"python src/fetch_versions.py --project-type {ptype} --split {vchunks}"):
        return 1

    try:
        with open(f"data/{ptype}/version_split.json", "r") as f:
            vsplit = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading version_split.json: {e}")
        return 1

    chunks = vsplit.get("chunks", [])
    for chunk_info in chunks:
        chunk_idx = chunk_info["index"]
        if not run_phase(f"Phase 3b: Fetch Versions (chunk {chunk_idx})",
                         f"python src/fetch_versions.py --project-type {ptype} --chunk {chunk_idx}"):
            print(f"Warning: Version chunk {chunk_idx} failed, continuing...")

    if not run_phase("Phase 3c: Merge Versions",
                     f"python src/fetch_versions.py --project-type {ptype} --merge"):
        return 1

    # Phase 4: Snapshot
    if not run_phase(f"Phase 4: Snapshot ({ptype})",
                     f"python src/snapshot.py --project-type {ptype}"):
        return 1

    # Phase 5: Analyze
    if not run_phase(f"Phase 5: Analyze ({ptype})",
                     f"python src/analyze.py --project-type {ptype}"):
        return 1

    print(f"\n{'='*60}")
    print(f"All phases completed successfully for {ptype}!")
    print(f"  Raw snapshot:  data/{ptype}/raw/")
    print(f"  Analysis:      data/{ptype}/analysis/")
    print(f"  Latest:        data/{ptype}/latest_analysis.json")
    print(f"{'='*60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
