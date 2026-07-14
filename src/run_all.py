#!/usr/bin/env python3
"""
Local runner script that runs all phases sequentially.
Useful for testing and development.
"""
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
    print("=" * 60)
    print("Modrinth Project Tracker - Full Run")
    print("=" * 60)

    # Phase 1: Discover
    if not run_phase("Phase 1: Discover", "python src/discover.py"):
        return 1

    # Load discovery to get partitions
    try:
        with open("data/discovery.json", "r") as f:
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
                         f"python src/fetch_projects.py --chunk {chunk}"):
            print(f"Warning: Chunk {chunk} failed, continuing...")

    # Phase 3: Fetch Versions
    if not run_phase("Phase 3: Fetch Versions", "python src/fetch_versions.py"):
        return 1

    # Phase 4: Snapshot
    if not run_phase("Phase 4: Snapshot", "python src/snapshot.py"):
        return 1

    # Phase 5: Analyze
    if not run_phase("Phase 5: Analyze", "python src/analyze.py"):
        return 1

    print(f"\n{'='*60}")
    print("All phases completed successfully!")
    print(f"{'='*60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())