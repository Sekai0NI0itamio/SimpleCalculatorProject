#!/usr/bin/env python3
"""
Generate a combined website report from all project type analyses.

Loads the latest analysis for each project type, combines them into a
single JSON report served to the website at reports/latest_analysis.json.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone

from utils import load_json, save_json, ensure_dir

PROJECT_TYPES = ["mod", "modpack", "resourcepack", "shader", "datapack", "plugin"]


def load_latest_analysis(data_dir, project_type):
    """Load the latest analysis for a project type."""
    analysis_dir = Path(data_dir) / project_type / "analysis"
    if not analysis_dir.exists():
        return None

    files = sorted(analysis_dir.glob("*.json"), reverse=True)
    for f in files:
        data = load_json(str(f))
        if data:
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
            "category_trending": {},
            "loader_rankings": [],
        },
    }

    all_top_projects = []
    all_top_vl = {}  # key: (game_version, loader) -> aggregated stats
    all_category_rankings = {}
    all_loader_rankings = {}
    # Combined category trending: category -> list of trending projects (with type)
    all_category_trending = {}

    for pt in PROJECT_TYPES:
        analysis = load_latest_analysis(data_dir, pt)
        if not analysis:
            report["project_types"][pt] = {"note": "no_analysis"}
            continue

        # Load predictive analysis if available (velocity, predictions, anomalies)
        sub_analysis_path = Path(data_dir) / pt / "latest_sub_analysis.json"
        sub_analysis = load_json(str(sub_analysis_path)) if sub_analysis_path.exists() else None

        # Store the analysis data
        report["project_types"][pt] = {
            "date": analysis.get("date", ""),
            "analysis_type": analysis.get("analysis_type", "hourly"),
            "analysis_quality": analysis.get("analysis_quality", "normal"),
            "actual_hours_between": analysis.get("actual_hours_between", 0),
            "data_quality": analysis.get("data_quality", {}),
            "summary": analysis.get("summary", {}),
            "category_rankings": analysis.get("category_rankings", [])[:10],
            "category_trending": analysis.get("category_trending", {}),
            "loader_rankings": analysis.get("loader_rankings", [])[:10],
            "top_projects": analysis.get("top_projects", [])[:20],
            "declining_projects": analysis.get("declining_projects", [])[:20],
            "top_version_loaders": analysis.get("top_version_loaders", [])[:200],
            "all_project_deltas": analysis.get("all_project_deltas", [])[:500],
            "trend_history": analysis.get("trend_history", []),
            "category_trend_history": analysis.get("category_trend_history", {}),
            "vl_trend_history": analysis.get("vl_trend_history", {}),
            # Predictive analysis (may be None if not enough snapshots)
            "predictive": sub_analysis if sub_analysis else None,
        }

        # Collect top projects with type prefix
        for p in analysis.get("top_projects", []):
            all_top_projects.append({**p, "project_type": pt})

        # Aggregate VL pairs by (game_version, loader) across ALL project types
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

        # Merge category trending (top 50 per category across all project types)
        cat_trending = analysis.get("category_trending", {}) or {}
        for cat, trending_list in cat_trending.items():
            if cat not in all_category_trending:
                all_category_trending[cat] = []
            for p in trending_list:
                all_category_trending[cat].append({**p, "project_type": pt})

    # Sort and assign
    all_top_projects.sort(key=lambda x: x.get("delta_downloads", 0), reverse=True)
    report["combined"]["top_projects"] = all_top_projects[:50]

    report["combined"]["top_version_loaders"] = sorted(
        all_top_vl.values(),
        key=lambda x: x["delta_downloads"],
        reverse=True,
    )[:50]

    report["combined"]["category_rankings"] = sorted(
        all_category_rankings.values(),
        key=lambda x: x["new_downloads"],
        reverse=True,
    )[:20]

    report["combined"]["loader_rankings"] = sorted(
        all_loader_rankings.values(),
        key=lambda x: x["new_downloads"],
        reverse=True,
    )[:20]

    # Combined category trending: for each category, take the top 50 trending
    # projects across ALL project types (sorted by delta_downloads descending).
    combined_trending = {}
    for cat, trending_list in all_category_trending.items():
        trending_list.sort(key=lambda x: x.get("delta_downloads", 0), reverse=True)
        combined_trending[cat] = trending_list[:50]
    report["combined"]["category_trending"] = combined_trending

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
    """Collect run history from all analysis files."""
    data_dir = Path(data_dir)
    WINDOW_MINUTES = 15

    all_entries = []
    for pt in PROJECT_TYPES:
        analysis_dir = data_dir / pt / "analysis"
        raw_dir = data_dir / pt / "raw"
        if not analysis_dir.exists():
            continue

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

    all_entries.sort(key=lambda e: e["timestamp"])

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
                current_ts = entry_ts
                continue
        groups.append(current_group)
        current_group = [entry]
        current_ts = entry_ts

    if current_group:
        groups.append(current_group)

    runs = []
    for group in groups:
        group.sort(key=lambda e: e["timestamp"])
        start_ts = group[0]["timestamp"]
        end_ts = group[-1]["timestamp"]
        start_dt = _parse_ts(start_ts)
        end_dt = _parse_ts(end_ts)

        duration_sec = 0
        if start_dt and end_dt:
            duration_sec = int((end_dt - start_dt).total_seconds())

        type_map = {}
        for e in group:
            pt = e["project_type"]
            if pt not in type_map or e["timestamp"] > type_map[pt]["timestamp"]:
                type_map[pt] = e

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

    runs = [r for r in runs if not (
        r["totals"]["projects"] == 0 and
        r["totals"]["versions"] == 0 and
        r["totals"]["downloads"] == 0
    )]

    runs.sort(key=lambda r: r["start_time"], reverse=True)
    return runs


def _parse_ts(ts_str):
    """Parse a timestamp string like '2026-07-15T09-57-35' into a datetime."""
    try:
        date_part, time_part = ts_str.split("T")
        time_part = time_part.replace("-", "")
        dt = datetime.strptime(f"{date_part}{time_part}", "%Y-%m-%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def write_streaming_chunks(report, reports_dir, data_dir="."):
    """Write per-type JSON chunks + a small summary file for streaming frontends.

    Layout:
      reports/summary.json          — small, totals + project_types index (no project lists)
      reports/type/{pt}.json        — per-type data (top projects, categories, VL pairs, trending)
      reports/combined.json         — combined top projects / VL / categories
      reports/run_history.json      — run history (loaded on demand)
      reports/charts/{pt}/*.png     — rendered chart images (lazy-loaded by frontend)
    """
    reports_dir = Path(reports_dir)
    type_dir = reports_dir / "type"
    charts_dir = reports_dir / "charts"
    ensure_dir(str(type_dir))
    ensure_dir(str(charts_dir))

    # 1. Summary — tiny file, fetched first by frontend
    summary = {
        "generated_at": report.get("generated_at"),
        "totals": report.get("totals", {}),
        "project_types": {},
    }
    for pt, info in report.get("project_types", {}).items():
        if not isinstance(info, dict):
            continue
        s = info.get("summary", {})
        dq = info.get("data_quality", {})
        summary["project_types"][pt] = {
            "total_projects": s.get("total_projects", 0),
            "total_versions": s.get("total_versions", 0),
            "total_downloads": s.get("total_downloads", 0),
            "new_downloads_since_baseline": s.get("new_downloads_since_baseline", 0),
            "lost_downloads_since_baseline": s.get("lost_downloads_since_baseline", 0),
            "net_download_change": s.get("net_download_change", 0),
            "downloads_per_hour": s.get("downloads_per_hour", 0),
            "growing_projects": s.get("growing_projects", 0),
            "declining_projects": s.get("declining_projects", 0),
            "analysis_type": info.get("analysis_type", "hourly"),
            "analysis_quality": info.get("analysis_quality", "normal"),
            "actual_hours_between": info.get("actual_hours_between", 0),
            "confidence": dq.get("confidence", "low"),
            "date": info.get("date", ""),
        }
    save_json(str(reports_dir / "summary.json"), summary)

    # 2. Per-type chunks — fetched in parallel by frontend, rendered as they arrive
    for pt, info in report.get("project_types", {}).items():
        if not isinstance(info, dict):
            continue

        # Copy charts from data/{pt}/charts/ to reports/charts/{pt}/
        # Build a chart list (path + display name) so the frontend can render a gallery.
        src_charts = Path(data_dir) / pt / "charts"
        chart_list = []
        if src_charts.exists():
            dst_charts = charts_dir / pt
            ensure_dir(str(dst_charts))
            for png in src_charts.glob("*.png"):
                shutil.copy2(str(png), str(dst_charts / png.name))
                name = png.stem.replace("_", " ").title()
                chart_list.append({
                    "path": f"reports/charts/{pt}/{png.name}",
                    "name": name,
                })

        save_json(str(type_dir / f"{pt}.json"), {
            "project_type": pt,
            "timestamp": info.get("timestamp", ""),
            "date": info.get("date", ""),
            "baseline_date": info.get("baseline_date", ""),
            "hours_between": info.get("hours_between", 0),
            "analysis_type": info.get("analysis_type", "hourly"),
            "analysis_quality": info.get("analysis_quality", "normal"),
            "actual_hours_between": info.get("actual_hours_between", 0),
            "data_quality": info.get("data_quality", {}),
            "downloads_per_hour": info.get("downloads_per_hour",
                                           info.get("summary", {}).get("downloads_per_hour", 0)),
            "summary": info.get("summary", {}),
            "category_rankings": info.get("category_rankings", [])[:10],
            "category_trending": info.get("category_trending", {}),
            "loader_rankings": info.get("loader_rankings", [])[:10],
            "top_projects": info.get("top_projects", [])[:20],
            "declining_projects": info.get("declining_projects", [])[:20],
            "top_version_loaders": info.get("top_version_loaders", [])[:200],
            "all_project_deltas": info.get("all_project_deltas", [])[:500],
            "predictive": info.get("predictive"),
            "charts": chart_list,
        })

    # 3. Combined rankings — fetched after summary, rendered for General tab
    save_json(str(reports_dir / "combined.json"), report.get("combined", {}))

    # 4. Run history — fetched only when user opens the History page
    save_json(str(reports_dir / "run_history.json"), report.get("run_history", []))

    # Count charts copied
    chart_count = sum(1 for _ in charts_dir.rglob("*.png"))

    print(f"  Streaming chunks: summary.json, "
          f"{len(report.get('project_types', {}))} type files, combined.json, run_history.json, "
          f"{chart_count} charts")


def main():
    data_dir = os.environ.get("DATA_DIR", "data")
    report = combine_analyses(data_dir)

    reports_dir = Path("reports")
    ensure_dir(str(reports_dir))
    output_path = reports_dir / "latest_analysis.json"
    save_json(str(output_path), report)

    # Also write streaming chunks (per-type + summary + combined + run_history + charts)
    write_streaming_chunks(report, reports_dir, data_dir)

    print(f"Report generated at {output_path}")
    print(f"  Total projects: {report['totals']['projects']:,}")
    print(f"  Total downloads: {report['totals']['downloads']:,}")
    print(f"  Combined top projects: {len(report['combined']['top_projects'])}")
    print(f"  Combined top VL pairs: {len(report['combined']['top_version_loaders'])}")
    print(f"  Combined categories: {len(report['combined']['category_rankings'])}")
    cat_trending = report["combined"].get("category_trending", {})
    trending_total = sum(len(v) for v in cat_trending.values())
    print(f"  Combined category trending: {len(cat_trending)} categories, {trending_total} trending projects")
    print(f"  Run history: {len(report.get('run_history', []))} runs recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
