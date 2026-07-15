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
        },
        "opportunity_analysis": None,
    }

    all_top_projects = []
    all_top_vl = []
    all_category_rankings = {}
    all_loader_rankings = {}

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
        }

        # Collect top projects with type prefix
        for p in analysis.get("top_projects", []):
            all_top_projects.append({
                **p,
                "project_type": pt,
            })

        # Collect top VL pairs with type prefix
        for vl in analysis.get("top_version_loaders", []):
            all_top_vl.append({
                **vl,
                "project_type": pt,
            })

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

    all_top_vl.sort(key=lambda x: x.get("delta_downloads", 0), reverse=True)
    report["combined"]["top_version_loaders"] = all_top_vl[:50]

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
    """Collect run history from all analysis files across all project types.

    Returns a list of run entries, each with timestamp, per-type stats, and
    total data volume (projects, versions, downloads, file size).
    """
    data_dir = Path(data_dir)
    # Collect all analysis files with their metadata
    # Keyed by timestamp (rounded to nearest 2-hour slot) to group
    runs = []  # list of {timestamp, types: {pt: {summary, file_size, analysis_type}}}

    all_entries = []
    for pt in PROJECT_TYPES:
        analysis_dir = data_dir / pt / "analysis"
        if not analysis_dir.exists():
            continue
        for f in analysis_dir.glob("*.json"):
            data = load_json(str(f))
            if not data:
                continue
            ts = data.get("timestamp", "")
            if not ts:
                continue
            summary = data.get("summary", {})
            file_size = f.stat().st_size
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
            })

    # Group by timestamp
    from collections import defaultdict
    by_ts = defaultdict(dict)
    for entry in all_entries:
        ts = entry["timestamp"]
        pt = entry["project_type"]
        by_ts[ts][pt] = entry

    # Build run entries sorted by timestamp descending
    for ts in sorted(by_ts.keys(), reverse=True):
        types_data = by_ts[ts]
        # Compute totals for this run
        total_projects = sum(
            e["summary"]["total_projects"] for e in types_data.values()
        )
        total_versions = sum(
            e["summary"]["total_versions"] for e in types_data.values()
        )
        total_downloads = sum(
            e["summary"]["total_downloads"] for e in types_data.values()
        )
        total_file_size = sum(e["file_size"] for e in types_data.values())
        # Determine the dominant analysis type
        analysis_types = set(e["analysis_type"] for e in types_data.values())
        dominant_type = "daily" if "daily" in analysis_types else "hourly"

        runs.append({
            "timestamp": ts,
            "analysis_type": dominant_type,
            "types": types_data,
            "totals": {
                "projects": total_projects,
                "versions": total_versions,
                "downloads": total_downloads,
                "file_size": total_file_size,
                "type_count": len(types_data),
            },
        })

    return runs


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