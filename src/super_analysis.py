#!/usr/bin/env python3
"""
Super Computer Analysis - Advanced analytics on full Modrinth database.
Runs intensive mathematical analysis with trend predictions and investment recommendations.
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

from utils import load_json, save_json, ensure_dir, get_current_date
from db import Database


def analyze_category_momentum(db, today):
    """Calculate 7-day and 30-day momentum for each category.

    Momentum = (current_downloads - previous_downloads) / previous_downloads * 100
    If only 1 snapshot exists, use simulated projections based on project count and avg downloads.
    Identifies "rising stars" (categories with >20% projected growth).
    """
    results = []

    try:
        # Get the latest category stats
        latest_cats = db.get_categories_for_date(today)
        if not latest_cats:
            # Try to get the most recent date
            cursor = db.conn.execute(
                "SELECT DISTINCT date FROM daily_category_stats ORDER BY date DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                latest_cats = db.get_categories_for_date(row["date"])

        if not latest_cats:
            return results

        # Get all dates for comparison
        cursor = db.conn.execute(
            "SELECT DISTINCT date FROM daily_category_stats ORDER BY date DESC"
        )
        dates = [r["date"] for r in cursor.fetchall()]

        for cat in latest_cats:
            cat_name = cat["category"]
            current_downloads = cat["total_downloads"]

            # Find 7-day and 30-day old data
            seven_day_old = None
            thirty_day_old = None

            if len(dates) >= 7:
                seven_date = dates[6]  # 7th date (0-indexed)
                seven_cats = db.get_categories_for_date(seven_date)
                for c in seven_cats:
                    if c["category"] == cat_name:
                        seven_day_old = c["total_downloads"]
                        break

            if len(dates) >= 30:
                thirty_date = dates[29] if len(dates) > 29 else dates[-1]
                thirty_cats = db.get_categories_for_date(thirty_date)
                for c in thirty_cats:
                    if c["category"] == cat_name:
                        thirty_day_old = c["total_downloads"]
                        break

            # Calculate momentum
            momentum_7d = 0
            momentum_30d = 0

            if seven_day_old and seven_day_old > 0:
                momentum_7d = ((current_downloads - seven_day_old) / seven_day_old) * 100

            if thirty_day_old and thirty_day_old > 0:
                momentum_30d = ((current_downloads - thirty_day_old) / thirty_day_old) * 100

            # If only 1 snapshot exists, use simulated projections
            if len(dates) <= 1:
                simulated_growth = (cat["project_count"] * 0.01) + (cat["avg_downloads"] * 0.0001)
                momentum_7d = simulated_growth
                momentum_30d = simulated_growth * 4

            # Determine stage based on average momentum
            avg_momentum = (momentum_7d + momentum_30d) / 2 if momentum_7d or momentum_30d else 0
            if avg_momentum > 20:
                stage = "rising"
            elif avg_momentum > 5:
                stage = "stable"
            elif avg_momentum > 0:
                stage = "slowing"
            else:
                stage = "declining"

            results.append({
                "category": cat_name,
                "momentum_7d": round(momentum_7d, 2),
                "momentum_30d": round(momentum_30d, 2),
                "momentum": round(avg_momentum, 1),
                "stage": stage,
                "current_downloads": current_downloads,
                "project_count": cat["project_count"]
            })

        results.sort(key=lambda x: x["momentum"], reverse=True)

    except Exception as e:
        print(f"  [WARN] Category momentum analysis failed: {e}")

    return results


def analyze_cross_category_correlation(db):
    """Find which categories tend to appear together in popular projects.

    For top 1000 projects by downloads, analyze their category combinations.
    Reports boost factor for combinations that outperform individual averages.
    """
    results = []

    try:
        projects = db.get_all_projects()
        if not projects:
            return results

        # Sort by downloads and take top 1000
        projects.sort(key=lambda p: p.get("downloads", 0), reverse=True)
        top_projects = projects[:1000]

        # Count category co-occurrences
        category_pairs = defaultdict(lambda: {"count": 0, "total_downloads": 0})
        category_singles = defaultdict(lambda: {"count": 0, "total_downloads": 0})

        for project in top_projects:
            try:
                cats = json.loads(project.get("categories", "[]"))
            except (json.JSONDecodeError, TypeError):
                continue

            if not isinstance(cats, list) or len(cats) < 2:
                continue

            downloads = project.get("downloads", 0)

            for i in range(len(cats)):
                cat = cats[i]
                if cat in ("fabric", "forge", "neoforge", "quilt"):
                    continue
                category_singles[cat]["count"] += 1
                category_singles[cat]["total_downloads"] += downloads
                for j in range(i + 1, len(cats)):
                    cat2 = cats[j]
                    if cat2 in ("fabric", "forge", "neoforge", "quilt"):
                        continue
                    pair = tuple(sorted([cat, cat2]))
                    category_pairs[pair]["count"] += 1
                    category_pairs[pair]["total_downloads"] += downloads

        # Calculate boost factor for each pair
        for pair, data in category_pairs.items():
            if data["count"] < 5:
                continue

            avg_pair_downloads = data["total_downloads"] / data["count"]

            # Calculate individual averages
            cat1_avg = 0
            cat2_avg = 0
            if pair[0] in category_singles and category_singles[pair[0]]["count"] > 0:
                cat1_avg = category_singles[pair[0]]["total_downloads"] / category_singles[pair[0]]["count"]
            if pair[1] in category_singles and category_singles[pair[1]]["count"] > 0:
                cat2_avg = category_singles[pair[1]]["total_downloads"] / category_singles[pair[1]]["count"]

            combined_avg = (cat1_avg + cat2_avg) / 2 if cat1_avg and cat2_avg else 0
            boost_factor = round(avg_pair_downloads / combined_avg, 1) if combined_avg > 0 else 0

            if boost_factor >= 1.5:
                results.append({
                    "combination": list(pair),
                    "avg_downloads": int(avg_pair_downloads),
                    "projects": data["count"],
                    "boost_factor": boost_factor
                })

        results.sort(key=lambda x: x["boost_factor"], reverse=True)
        results = results[:20]

    except Exception as e:
        print(f"  [WARN] Cross-category correlation failed: {e}")

    return results


def analyze_loader_market_fit(db):
    """Analyze loader market share, download share, and efficiency ratio.

    For each loader:
      - Market share = (loader_projects / total_projects) * 100
      - Download share = (loader_downloads / total_downloads) * 100
      - Efficiency ratio = download_share / market_share
      - Ratio > 1 means projects on this loader outperform per-project
    """
    results = []

    try:
        projects = db.get_all_projects()
        if not projects:
            return results

        total_projects = len(projects)
        total_downloads = sum(p.get("downloads", 0) for p in projects)

        loaders = ["fabric", "forge", "neoforge", "quilt"]
        loader_stats = {l: {"projects": 0, "downloads": 0} for l in loaders}

        for project in projects:
            try:
                cats = json.loads(project.get("categories", "[]"))
            except (json.JSONDecodeError, TypeError):
                continue

            if not isinstance(cats, list):
                continue

            downloads = project.get("downloads", 0)
            for loader in loaders:
                if loader in cats:
                    loader_stats[loader]["projects"] += 1
                    loader_stats[loader]["downloads"] += downloads

        for loader, stats in loader_stats.items():
            market_share = (stats["projects"] / total_projects) * 100 if total_projects > 0 else 0
            download_share = (stats["downloads"] / total_downloads) * 100 if total_downloads > 0 else 0
            efficiency_ratio = round(download_share / market_share, 2) if market_share > 0 else 0

            results.append({
                "loader": loader,
                "market_share": round(market_share, 1),
                "download_share": round(download_share, 1),
                "efficiency_ratio": efficiency_ratio,
                "project_count": stats["projects"],
                "total_downloads": stats["downloads"]
            })

        results.sort(key=lambda x: x["efficiency_ratio"], reverse=True)

    except Exception as e:
        print(f"  [WARN] Loader market fit analysis failed: {e}")

    return results


def analyze_version_lifecycle(db):
    """Analyze MC version lifecycle stages.

    For each MC version, determine lifecycle stage:
      - "Emerging" (< 6 months old, growing)
      - "Peak" (most projects, highest downloads)
      - "Mature" (stable, declining growth)
      - "Legacy" (old, few new projects)
    """
    results = []

    try:
        cursor = db.conn.execute("""
            SELECT project_id, game_versions, downloads FROM versions
            WHERE game_versions IS NOT NULL
        """)

        version_stats = defaultdict(
            lambda: {"project_count": 0, "total_downloads": 0, "projects": set()}
        )

        for row in cursor.fetchall():
            try:
                game_versions = json.loads(row["game_versions"])
            except (json.JSONDecodeError, TypeError):
                continue

            if not isinstance(game_versions, list):
                continue

            for gv in game_versions:
                parts = gv.split(".")
                if len(parts) >= 2:
                    major_key = ".".join(parts[:2]) if len(parts) == 2 else ".".join(parts[:3])
                    version_stats[major_key]["project_count"] += 1
                    version_stats[major_key]["total_downloads"] += row["downloads"]
                    version_stats[major_key]["projects"].add(row["project_id"])

        # Get project creation dates for determining age
        cursor = db.conn.execute(
            "SELECT project_id, date_created FROM projects WHERE date_created IS NOT NULL"
        )
        project_dates = {row["project_id"]: row["date_created"] for row in cursor.fetchall()}

        today = datetime.now()

        for version, stats in version_stats.items():
            project_count = stats["project_count"]
            total_downloads = stats["total_downloads"]

            # Estimate version age based on project dates
            version_dates = []
            for pid in stats["projects"]:
                if pid in project_dates:
                    try:
                        d = datetime.fromisoformat(project_dates[pid])
                        version_dates.append(d)
                    except (ValueError, TypeError):
                        pass

            # Determine lifecycle stage
            if version_dates:
                avg_date = sum(d.timestamp() for d in version_dates) / len(version_dates)
                avg_age_days = (today.timestamp() - avg_date) / 86400
            else:
                avg_age_days = 365  # Default assumption

            # Parse version number for rough stage heuristic
            try:
                major_minor = version.split(".")
                major = int(major_minor[0])
                minor = int(major_minor[1]) if len(major_minor) > 1 else 0
                if major == 1 and minor >= 21:
                    stage = "peak"
                elif major == 1 and minor >= 20:
                    if avg_age_days < 180:
                        stage = "emerging"
                    else:
                        stage = "peak"
                elif major == 1 and minor >= 18:
                    stage = "mature"
                else:
                    stage = "legacy"
            except (ValueError, IndexError):
                stage = "mature"

            # Refine based on age
            if stage == "peak" and avg_age_days > 365:
                stage = "mature"
            elif stage == "emerging" and avg_age_days > 180:
                stage = "peak"

            results.append({
                "version": version,
                "stage": stage,
                "project_count": project_count,
                "total_downloads": total_downloads
            })

        results.sort(key=lambda x: x["project_count"], reverse=True)
        results = results[:20]

    except Exception as e:
        print(f"  [WARN] Version lifecycle analysis failed: {e}")

    return results


def analyze_market_gaps(db, loader_fit_results):
    """Find underserved category+loader combinations.

    A gap exists when: category has high avg_downloads AND loader has < 10% of projects in that category.
    """
    results = []

    try:
        projects = db.get_all_projects()
        if not projects:
            return results

        # Get category averages from daily_category_stats
        cursor = db.conn.execute("""
            SELECT category, AVG(avg_downloads) as overall_avg,
                   MAX(total_downloads) as total_dl
            FROM daily_category_stats
            GROUP BY category
            ORDER BY overall_avg DESC
        """)
        category_avgs = {row["category"]: row["overall_avg"] for row in cursor.fetchall()}

        loaders = ["fabric", "forge", "neoforge", "quilt"]

        # Count projects per category per loader
        loader_category_counts = defaultdict(lambda: defaultdict(int))
        category_project_counts = defaultdict(int)

        for project in projects:
            try:
                cats = json.loads(project.get("categories", "[]"))
            except (json.JSONDecodeError, TypeError):
                continue

            if not isinstance(cats, list):
                continue

            project_loaders = [l for l in loaders if l in cats]
            project_categories = [c for c in cats if c not in loaders]

            for cat in project_categories:
                category_project_counts[cat] += 1
                for loader in project_loaders:
                    loader_category_counts[loader][cat] += 1

        # Find gaps: high avg_downloads category but <10% loader share
        for cat, avg_dl in category_avgs.items():
            if avg_dl < 10000:  # Skip low-value categories
                continue

            total_cat_projects = category_project_counts.get(cat, 0)
            if total_cat_projects == 0:
                continue

            for loader in loaders:
                loader_cat_count = loader_category_counts[loader].get(cat, 0)
                loader_share = (loader_cat_count / total_cat_projects) * 100

                if loader_share < 10 and loader_cat_count > 0:
                    # This is a gap: high-value category underserved by this loader
                    gap_score = round((avg_dl / 10000) * (10 - loader_share), 1)
                    results.append({
                        "category": cat,
                        "loader": loader,
                        "current_projects": loader_cat_count,
                        "total_category_projects": total_cat_projects,
                        "avg_downloads": int(avg_dl),
                        "loader_share_pct": round(loader_share, 1),
                        "gap_score": gap_score
                    })

        results.sort(key=lambda x: x["gap_score"], reverse=True)
        results = results[:15]

    except Exception as e:
        print(f"  [WARN] Market gap analysis failed: {e}")

    return results


def generate_investment_recommendations(market_gaps, category_momentum, loader_fit):
    """Generate a ranked list of 'what to build' with expected downloads, competition, risk, and ROI."""
    recommendations = []

    try:
        loader_growth = {}
        if loader_fit:
            for lf in loader_fit:
                loader_growth[lf["loader"]] = lf["efficiency_ratio"]

        rising_categories = {cm["category"] for cm in category_momentum if cm["stage"] == "rising"}

        for i, gap in enumerate(market_gaps[:10]):
            category = gap["category"]
            loader = gap["loader"]
            avg_dl = gap["avg_downloads"]
            expected_downloads = int(avg_dl * 0.5)  # 50% of avg for new entry

            # Competition level based on existing project count in this combo
            if gap["current_projects"] < 100:
                competition = "Low"
                risk = 3
            elif gap["current_projects"] < 500:
                competition = "Medium"
                risk = 5
            else:
                competition = "High"
                risk = 7

            # ROI calculation
            if category in rising_categories and competition == "Low":
                roi = "High"
                risk = max(risk - 1, 1)
            elif competition == "High":
                roi = "Low"
                risk = min(risk + 1, 10)
            else:
                roi = "Medium"

            reasoning_parts = []
            if category in rising_categories:
                reasoning_parts.append(f"'{category}' is a rising category")
            if loader_growth.get(loader, 0) > 1:
                reasoning_parts.append(f"{loader} projects outperform per-project averages")
            reasoning_parts.append(f"only {gap['current_projects']} existing projects in this combination")

            recommendations.append({
                "rank": i + 1,
                "category": category,
                "loader": loader,
                "expected_downloads": expected_downloads,
                "competition": competition,
                "risk_score": risk,
                "roi": roi,
                "reasoning": "; ".join(reasoning_parts)
            })

    except Exception as e:
        print(f"  [WARN] Investment recommendation generation failed: {e}")

    return recommendations


def main():
    print("=== Super Computer Analysis ===")

    db_path = "data/modrinth_tracker.db"
    if not os.path.exists(db_path):
        print("ERROR: No database found at data/modrinth_tracker.db")
        print("Please run the daily tracker workflow first to generate the database.")
        return 1

    today = get_current_date()
    db = Database(db_path)

    all_projects = db.get_all_projects()
    print(f"Analysis date: {today}")
    print(f"Total projects: {len(all_projects)}")

    # Count total versions
    cursor = db.conn.execute("SELECT COUNT(*) as cnt FROM versions")
    total_versions = cursor.fetchone()["cnt"]
    print(f"Total versions: {total_versions}")

    print("\n--- Running Category Momentum Analysis ---")
    category_momentum = analyze_category_momentum(db, today)
    print(f"  Found {len(category_momentum)} categories with momentum data")

    print("\n--- Running Cross-Category Correlation ---")
    cross_category = analyze_cross_category_correlation(db)
    print(f"  Found {len(cross_category)} cross-category insights")

    print("\n--- Running Loader Market Fit Analysis ---")
    loader_fit = analyze_loader_market_fit(db)
    print(f"  Analyzed {len(loader_fit)} loaders")

    print("\n--- Running Version Lifecycle Analysis ---")
    version_lifecycle = analyze_version_lifecycle(db)
    print(f"  Found {len(version_lifecycle)} version lifecycle stages")

    print("\n--- Running Market Gap Analysis ---")
    market_gaps = analyze_market_gaps(db, loader_fit)
    print(f"  Found {len(market_gaps)} market gaps")

    print("\n--- Generating Investment Recommendations ---")
    recommendations = generate_investment_recommendations(market_gaps, category_momentum, loader_fit)
    print(f"  Generated {len(recommendations)} recommendations")

    # Build output summary
    summary = {
        "analysis_date": today,
        "total_projects": len(all_projects),
        "total_versions": total_versions,
        "category_momentum": [
            {
                "category": c["category"],
                "momentum": c["momentum"],
                "stage": c["stage"]
            }
            for c in category_momentum
        ],
        "cross_category_insights": cross_category,
        "loader_market_fit": [
            {
                "loader": l["loader"],
                "market_share": l["market_share"],
                "download_share": l["download_share"],
                "efficiency_ratio": l["efficiency_ratio"]
            }
            for l in loader_fit
        ],
        "version_lifecycle": version_lifecycle,
        "market_gaps": [
            {
                "category": g["category"],
                "loader": g["loader"],
                "current_projects": g["current_projects"],
                "avg_downloads": g["avg_downloads"],
                "gap_score": g["gap_score"]
            }
            for g in market_gaps
        ],
        "investment_recommendations": recommendations
    }

    # Save JSON
    ensure_dir("reports/super_analysis")
    json_path = f"reports/super_analysis/super_analysis_{today}.json"
    save_json(json_path, summary)
    print(f"\nSaved JSON report to {json_path}")

    # Generate markdown report
    md_lines = [
        f"# Super Computer Analysis Report - {today}",
        "",
        f"**Total Projects:** {summary['total_projects']:,}",
        f"**Total Versions:** {summary['total_versions']:,}",
        "",
        "---",
        "",
        "## Category Momentum",
        "",
        "| Category | Momentum | Stage |",
        "|----------|----------|-------|"
    ]

    for cm in summary["category_momentum"]:
        md_lines.append(f"| {cm['category']} | {cm['momentum']:.1f}% | {cm['stage']} |")

    md_lines.extend(["", "---", "", "## Cross-Category Insights", ""])

    if cross_category:
        md_lines.extend([
            "| Combination | Avg Downloads | Projects | Boost Factor |",
            "|-------------|---------------|----------|-------------|"
        ])
        for cc in cross_category:
            comb = " + ".join(cc["combination"])
            md_lines.append(f"| {comb} | {cc['avg_downloads']:,} | {cc['projects']} | {cc['boost_factor']}x |")
    else:
        md_lines.append("No significant cross-category correlations found.")

    md_lines.extend(["", "---", "", "## Loader Market Fit", ""])

    if loader_fit:
        md_lines.extend([
            "| Loader | Market Share | Download Share | Efficiency Ratio |",
            "|--------|-------------|----------------|-----------------|"
        ])
        for lf in summary["loader_market_fit"]:
            md_lines.append(
                f"| {lf['loader']} | {lf['market_share']}% | {lf['download_share']}% | {lf['efficiency_ratio']} |"
            )
    else:
        md_lines.append("No loader data available.")

    md_lines.extend(["", "---", "", "## Version Lifecycle", ""])

    if version_lifecycle:
        md_lines.extend([
            "| Version | Stage | Project Count | Total Downloads |",
            "|---------|-------|--------------|----------------|"
        ])
        for vl in version_lifecycle:
            md_lines.append(f"| {vl['version']} | {vl['stage']} | {vl['project_count']:,} | {vl['total_downloads']:,} |")
    else:
        md_lines.append("No version lifecycle data available.")

    md_lines.extend(["", "---", "", "## Market Gaps", ""])

    if market_gaps:
        md_lines.extend([
            "| Category | Loader | Current Projects | Avg Downloads | Gap Score |",
            "|----------|--------|-----------------|---------------|-----------|"
        ])
        for mg in summary["market_gaps"]:
            md_lines.append(
                f"| {mg['category']} | {mg['loader']} | {mg['current_projects']:,} | {mg['avg_downloads']:,} | {mg['gap_score']} |"
            )
    else:
        md_lines.append("No market gaps identified.")

    md_lines.extend(["", "---", "", "## Investment Recommendations", ""])

    if recommendations:
        md_lines.extend([
            "| Rank | Category | Loader | Expected Downloads | Competition | Risk | ROI | Reasoning |",
            "|------|----------|--------|-------------------|-------------|------|-----|----------|"
        ])
        for rec in recommendations:
            md_lines.append(
                f"| {rec['rank']} | {rec['category']} | {rec['loader']} | "
                f"{rec['expected_downloads']:,} | {rec['competition']} | "
                f"{rec['risk_score']}/10 | {rec['roi']} | {rec['reasoning']} |"
            )
    else:
        md_lines.append("No investment recommendations available.")

    md_report = "\n".join(md_lines)

    md_path = f"reports/super_analysis/super_analysis_{today}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    print(f"Saved Markdown report to {md_path}")

    db.close()
    print("\n=== Super Analysis Complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())