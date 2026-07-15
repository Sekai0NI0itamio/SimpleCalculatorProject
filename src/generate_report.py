#!/usr/bin/env python3
"""
Generate a combined website report from all project type analyses.

Loads the latest daily analysis for each project type, combines them into a
single JSON report served to the website at reports/latest_analysis.json.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

from utils import load_json, save_json, ensure_dir

PROJECT_TYPES = ["mod", "modpack", "resourcepack", "shader", "datapack", "world"]


def load_latest_analysis(data_dir, project_type):
    """Load the latest analysis (any mode) for a project type.

    Returns the analysis dict or None if no analysis exists.
    """
    analysis_dir = Path(data_dir) / project_type / "analysis"
    if not analysis_dir.exists():
        return None

    files = sorted(analysis_dir.glob("*.json"), reverse=True)
    for f in files:
        data = load_json(str(f))
        if data:
            return data
    return None


def load_latest_daily_analysis(data_dir, project_type):
    """Load the latest DAILY analysis for a project type."""
    analysis_dir = Path(data_dir) / project_type / "analysis"
    if not analysis_dir.exists():
        return None

    files = sorted(analysis_dir.glob("*.json"), reverse=True)
    for f in files:
        data = load_json(str(f))
        if data and data.get("analysis_type") == "daily":
            return data
    return None


def combine_analyses(data_dir):
    """Combine all project type analyses into a single website report."""
    data_dir = Path(data_dir)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "project_types": {},
        "combined": {
            "top_projects": [],
            "top_version_loaders": [],
            "category_rankings": [],
            "loader_rankings": [],
            "growth_ranking": [],
            "rising_stars": [],
        },
        "opportunity_analysis": None,
    }

    all_top_projects = []
    all_top_vl = {}  # key: (game_version, loader) -> aggregated stats
    all_category_rankings = {}
    all_loader_rankings = {}
    all_growth_ranking = []
    all_rising_stars = []

    for pt in PROJECT_TYPES:
        analysis = load_latest_analysis(data_dir, pt)
        if not analysis:
            report["project_types"][pt] = {"note": "no_analysis"}
            continue

        # Store the analysis data
        report["project_types"][pt] = {
            "date": analysis.get("date", ""),
            "analysis_type": analysis.get("analysis_type", "hourly"),
            "summary": analysis.get("summary", {}),
            "category_rankings": analysis.get("category_rankings", [])[:10],
            "loader_rankings": analysis.get("loader_rankings", [])[:10],
            "top_projects": analysis.get("top_projects", [])[:20],
            "top_version_loaders": analysis.get("top_version_loaders", [])[:20],
            "trending_analysis": analysis.get("trending_analysis", {}),
            "composition_bias": analysis.get("composition_bias", {}),
            "growth_ranking": analysis.get("growth_ranking", [])[:20],
            "heavy_hitter_adjusted": analysis.get("heavy_hitter_adjusted", [])[:20],
            "rising_stars": analysis.get("rising_stars", [])[:20],
        }

        # Collect top projects with type prefix
        for p in analysis.get("top_projects", []):
            all_top_projects.append({
                **p,
                "project_type": pt,
            })

        # Collect growth ranking with type prefix
        for p in analysis.get("growth_ranking", []):
            all_growth_ranking.append({
                **p,
                "project_type": pt,
            })

        # Collect rising stars with type prefix
        for p in analysis.get("rising_stars", []):
            all_rising_stars.append({
                **p,
                "project_type": pt,
            })

        # Aggregate VL pairs by (game_version, loader) across ALL project types
        # Per user: "1.20.1 fabric and 1.20.1 forge are TWO different things"
        # Each (game_version, loader) pair is summed across all project types.
        for vl in analysis.get("top_version_loaders", []):
            gv = vl.get("game_version", "")
            loader = vl.get("loader", "")
            key = (gv, loader)
            if key not in all_top_vl:
                all_top_vl[key] = {
                    "game_version": gv,
                    "loader": loader,
                    "delta_downloads": 0,
                    "project_count": 0,
                    "top_project_id": "",
                    "top_project_title": "",
                    "top_project_delta": 0,
                    "types": {},
                }
            stat = all_top_vl[key]
            stat["delta_downloads"] += vl.get("delta_downloads", 0)
            stat["project_count"] += vl.get("project_count", 0)
            # Track the single top project across all types for this VL pair
            if vl.get("top_project_delta", 0) > stat["top_project_delta"]:
                stat["top_project_delta"] = vl.get("top_project_delta", 0)
                stat["top_project_id"] = vl.get("top_project_id", "")
                stat["top_project_title"] = vl.get("top_project_title", "")
            stat["types"][pt] = {
                "delta_downloads": vl.get("delta_downloads", 0),
                "project_count": vl.get("project_count", 0),
            }

        # Merge category rankings
        for cat in analysis.get("category_rankings", []):
            key = cat["category"]
            if key not in all_category_rankings:
                all_category_rankings[key] = {
                    "category": key,
                    "projects": 0,
                    "total_downloads": 0,
                    "new_downloads": 0,
                    "growth_pct": 0,
                    "types": {},
                }
            all_category_rankings[key]["projects"] += cat.get("projects", 0)
            all_category_rankings[key]["total_downloads"] += cat.get("total_downloads", 0)
            all_category_rankings[key]["new_downloads"] += cat.get("new_downloads", 0)
            all_category_rankings[key]["types"][pt] = {
                "projects": cat.get("projects", 0),
                "total_downloads": cat.get("total_downloads", 0),
                "new_downloads": cat.get("new_downloads", 0),
            }

        # Merge loader rankings
        for ld in analysis.get("loader_rankings", []):
            key = ld["loader"]
            if key not in all_loader_rankings:
                all_loader_rankings[key] = {
                    "loader": key,
                    "projects": 0,
                    "total_downloads": 0,
                    "new_downloads": 0,
                }
            all_loader_rankings[key]["projects"] += ld.get("projects", 0)
            all_loader_rankings[key]["total_downloads"] += ld.get("total_downloads", 0)
            all_loader_rankings[key]["new_downloads"] += ld.get("new_downloads", 0)

        # Use mod's opportunity analysis (most relevant for mod building decisions)
        if pt == "mod" and analysis.get("opportunity_analysis"):
            report["opportunity_analysis"] = analysis["opportunity_analysis"]

    # Sort and deduplicate
    all_top_projects.sort(key=lambda x: x.get("delta_downloads", 0), reverse=True)
    report["combined"]["top_projects"] = all_top_projects[:50]

    all_growth_ranking.sort(key=lambda x: x.get("growth_pct", 0), reverse=True)
    report["combined"]["growth_ranking"] = all_growth_ranking[:50]

    all_rising_stars.sort(key=lambda x: x.get("growth_pct", 0), reverse=True)
    report["combined"]["rising_stars"] = all_rising_stars[:50]

    # Sort VL pairs by total delta_downloads across all types
    report["combined"]["top_version_loaders"] = sorted(
        all_top_vl.values(),
        key=lambda x: x["delta_downloads"],
        reverse=True,
    )[:50]

    # Sort category rankings by new_downloads
    report["combined"]["category_rankings"] = sorted(
        all_category_rankings.values(),
        key=lambda x: x["new_downloads"],
        reverse=True,
    )[:20]

    # Sort loader rankings by new_downloads
    report["combined"]["loader_rankings"] = sorted(
        all_loader_rankings.values(),
        key=lambda x: x["new_downloads"],
        reverse=True,
    )[:20]

    # Compute total stats
    total_projects = sum(
        pt.get("summary", {}).get("total_projects", 0)
        for pt in report["project_types"].values()
        if isinstance(pt, dict)
    )
    total_downloads = sum(
        pt.get("summary", {}).get("total_downloads", 0)
        for pt in report["project_types"].values()
        if isinstance(pt, dict)
    )
    total_versions = sum(
        pt.get("summary", {}).get("total_versions", 0)
        for pt in report["project_types"].values()
        if isinstance(pt, dict)
    )

    report["totals"] = {
        "projects": total_projects,
        "downloads": total_downloads,
        "versions": total_versions,
    }

    # Collect run history
    report["run_history"] = collect_run_history(data_dir)

    return report


def collect_run_history(data_dir):
    """Collect run history from all analysis and raw snapshot files.

    Groups entries within 15-minute windows into actual workflow runs.
    Each run shows: start time, end time, duration, how much data was evaluated.

    Returns a list of run entries sorted by start time descending.
    """
    data_dir = Path(data_dir)
    WINDOW_MINUTES = 15  # Group entries within this window as one run

    all_entries = []
    for pt in PROJECT_TYPES:
        analysis_dir = data_dir / pt / "analysis"
        raw_dir = data_dir / pt / "raw"
        if not analysis_dir.exists():
            continue

        # Build a map of timestamp -> raw snapshot file size for this type
        raw_size_map = {}
        if raw_dir.exists():
            for f in raw_dir.glob("*.json.gz"):
                raw_size_map[f.stem.replace(".json", "")] = f.stat().st_size

        for f in analysis_dir.glob("*.json"):
            data = load_json(str(f))
            if not data:
                continue
            ts = data.get("timestamp", "")
            if not ts:
                continue
            summary = data.get("summary", {})
            file_size = f.stat().st_size

            # Find matching raw snapshot size
            raw_size = 0
            for raw_key, rs in raw_size_map.items():
                if raw_key in ts or ts in raw_key:
                    raw_size = rs
                    break

            all_entries.append({
                "timestamp": ts,
                "project_type": pt,
                "analysis_type": data.get("analysis_type", "hourly"),
                "date": data.get("date", ""),
                "baseline_date": data.get("baseline_date", ""),
                "hours_between": data.get("hours_between", 0),
                "summary": {
                    "total_projects": summary.get("total_projects", 0),
                    "total_versions": summary.get("total_versions", 0),
                    "total_downloads": summary.get("total_downloads", 0),
                    "new_downloads_since_baseline": summary.get("new_downloads_since_baseline", 0),
                },
                "file_size": file_size,
                "raw_size": raw_size,
            })

    if not all_entries:
        return []

    # Sort by timestamp
    all_entries.sort(key=lambda e: e["timestamp"])

    # Group into time windows
    from datetime import datetime, timedelta
    groups = []
    current_group = [all_entries[0]]
    current_ts = _parse_ts(all_entries[0]["timestamp"])

    for entry in all_entries[1:]:
        entry_ts = _parse_ts(entry["timestamp"])
        if entry_ts and current_ts:
            diff = abs((entry_ts - current_ts).total_seconds()) / 60
            if diff <= WINDOW_MINUTES:
                current_group.append(entry)
                current_ts = entry_ts  # Update to latest timestamp in group
                continue
        # Start new group
        groups.append(current_group)
        current_group = [entry]
        current_ts = entry_ts

    if current_group:
        groups.append(current_group)

    # Build run entries
    runs = []
    for group in groups:
        # Sort group by timestamp
        group.sort(key=lambda e: e["timestamp"])
        start_ts = group[0]["timestamp"]
        end_ts = group[-1]["timestamp"]
        start_dt = _parse_ts(start_ts)
        end_dt = _parse_ts(end_ts)

        # Duration in seconds
        duration_sec = 0
        if start_dt and end_dt:
            duration_sec = int((end_dt - start_dt).total_seconds())

        # Deduplicate by project type (keep the latest entry per type in the group)
        type_map = {}
        for e in group:
            pt = e["project_type"]
            if pt not in type_map or e["timestamp"] > type_map[pt]["timestamp"]:
                type_map[pt] = e

        # Compute totals
        total_projects = sum(e["summary"]["total_projects"] for e in type_map.values())
        total_versions = sum(e["summary"]["total_versions"] for e in type_map.values())
        total_downloads = sum(e["summary"]["total_downloads"] for e in type_map.values())
        total_new = sum(e["summary"]["new_downloads_since_baseline"] for e in type_map.values())
        total_file_size = sum(e["file_size"] for e in type_map.values())
        total_raw_size = sum(e["raw_size"] for e in type_map.values())

        analysis_types = set(e["analysis_type"] for e in type_map.values())
        dominant_type = "daily" if "daily" in analysis_types else "hourly"

        runs.append({
            "start_time": start_ts,
            "end_time": end_ts,
            "duration_seconds": duration_sec,
            "analysis_type": dominant_type,
            "types": {pt: e for pt, e in type_map.items()},
            "totals": {
                "projects": total_projects,
                "versions": total_versions,
                "downloads": total_downloads,
                "new_downloads": total_new,
                "file_size": total_file_size,
                "raw_size": total_raw_size,
                "type_count": len(type_map),
            },
        })

    # Filter out empty runs (no data collected)
    runs = [r for r in runs if not (
        r["totals"]["projects"] == 0 and
        r["totals"]["versions"] == 0 and
        r["totals"]["downloads"] == 0
    )]

    # Sort by start time descending (newest first)
    runs.sort(key=lambda r: r["start_time"], reverse=True)
    return runs


def _parse_ts(ts_str):
    """Parse a timestamp string like '2026-07-15T09-57-35' into a datetime."""
    try:
        # Handle format: 2026-07-15T09-57-35
        # Split on T: date = 2026-07-15, time = 09-57-35
        date_part, time_part = ts_str.split("T")
        time_part = time_part.replace("-", ":")
        return datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def main():
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    data_dir = project_root / "data"
    reports_dir = project_root / "reports"

    ensure_dir(str(reports_dir))

    print("Generating combined website report...")
    report = combine_analyses(data_dir)

    output_path = reports_dir / "latest_analysis.json"
    save_json(str(output_path), report)
    print(f"  Saved to {output_path}")
    print(f"  Project types: {list(report['project_types'].keys())}")
    print(f"  Combined top projects: {len(report['combined']['top_projects'])}")
    print(f"  Combined top VL pairs: {len(report['combined']['top_version_loaders'])}")
    print(f"  Combined categories: {len(report['combined']['category_rankings'])}")
    has_opp = report.get("opportunity_analysis") and report["opportunity_analysis"].get("top_10_opportunities")
    print(f"  Opportunity analysis: {'yes' if has_opp else 'no'}")
    run_history = report.get("run_history", [])
    print(f"  Run history: {len(run_history)} runs recorded")


if __name__ == "__main__":
    main()