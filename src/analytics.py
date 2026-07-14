#!/usr/bin/env python3
"""
Modrinth Market Analytics Engine

Unified analytical system that combines opportunity analysis, cross-category
correlation, loader market fit, version lifecycle, market gap detection, and
investment recommendations into a single actionable report.

Outputs:
  - reports/analytics_{date}.md     (full markdown report)
  - reports/analytics_{date}.json   (structured data for dashboards)
  - reports/latest_analysis.json    (always points to latest)
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from utils import load_json, save_json, ensure_dir, get_current_date
from db import Database


# ── Configuration ──────────────────────────────────────────────────
MIN_PROJECTS_THRESHOLD = 5
TOP_N = 25


# ═══════════════════════════════════════════════════════════════════
#  MATH HELPERS
# ═══════════════════════════════════════════════════════════════════


def sigmoid(x):
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 1.0 if x > 0 else 0.0


def ema_smooth(values, alpha=0.3):
    if not values:
        return 0.0
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def min_max_normalize(values):
    if not values:
        return {}
    mn = min(values)
    mx = max(values)
    if mx == mn:
        return {v: 0.5 for v in values}
    return {v: (v - mn) / (mx - mn) for v in values}


# ═══════════════════════════════════════════════════════════════════
#  1. CATEGORY RANKINGS + OPPORTUNITY
# ═══════════════════════════════════════════════════════════════════


def compute_opportunity_score(avg_downloads, project_count, new_downloads):
    """Score niches: high avg downloads + high new downloads + low competition."""
    if project_count == 0:
        return 0.0
    demand = (avg_downloads**0.7) * (max(new_downloads, 1) ** 0.3)
    competition = project_count**0.5
    return demand / competition


def analyze_category_opportunity(db, today):
    """Rank categories by opportunity score and return raw data + markdown."""
    cats = db.get_categories_for_date(today)
    if not cats:
        return [], "No category data available."

    prev = db.conn.execute(
        "SELECT DISTINCT date FROM daily_category_stats WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today,),
    ).fetchone()
    prev_date = prev["date"] if prev else None
    prev_map = {}
    if prev_date:
        prev_map = {c["category"]: c for c in db.get_categories_for_date(prev_date)}

    rows = []
    for c in cats:
        if c["project_count"] < MIN_PROJECTS_THRESHOLD:
            continue
        score = compute_opportunity_score(
            c["avg_downloads"], c["project_count"], c["total_new_downloads"]
        )
        p = prev_map.get(c["category"], {})
        avg_change = (
            (c["avg_downloads"] - p.get("avg_downloads", 0))
            / max(p.get("avg_downloads", 0), 1)
        ) * 100
        rows.append(
            {
                "category": c["category"],
                "projects": c["project_count"],
                "total_downloads": c["total_downloads"],
                "avg_downloads": c["avg_downloads"],
                "new_downloads": c["total_new_downloads"],
                "opportunity_score": round(score, 1),
                "avg_change_pct": round(avg_change, 1),
            }
        )
    rows.sort(key=lambda r: r["opportunity_score"], reverse=True)

    lines = [
        "## Category Opportunity Matrix",
        "",
        "| Category | Projects | Avg Downloads | New Today | Opportunity Score | Avg Δ% |",
        "|----------|----------|--------------|-----------|-----------------|--------|",
    ]
    for r in rows[:TOP_N]:
        lines.append(
            f"| {r['category']} | {r['projects']} | {r['avg_downloads']:,.0f} | "
            f"{r['new_downloads']:,} | {r['opportunity_score']:,.1f} | {r['avg_change_pct']:+.1f}% |"
        )
    return rows, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  2. CATEGORY RANKINGS (by total downloads)
# ═══════════════════════════════════════════════════════════════════


def analyze_category_rankings(db, today):
    """Rank categories by total downloads with growth %."""
    cats = db.get_categories_for_date(today)
    if not cats:
        return [], "No category data."

    prev = db.conn.execute(
        "SELECT DISTINCT date FROM daily_category_stats WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today,),
    ).fetchone()
    prev_map = {}
    if prev:
        prev_map = {
            c["category"]: c["total_downloads"]
            for c in db.get_categories_for_date(prev["date"])
        }

    rows = []
    for c in cats:
        p = prev_map.get(c["category"], 0)
        growth = ((c["total_downloads"] - p) / max(p, 1)) * 100
        rows.append(
            {
                "category": c["category"],
                "projects": c["project_count"],
                "total_downloads": c["total_downloads"],
                "avg_downloads": c["avg_downloads"],
                "new_downloads": c["total_new_downloads"],
                "growth_pct": round(growth, 4),
            }
        )

    lines = [
        "## Category Rankings by Total Downloads",
        "",
        "| # | Category | Projects | Total Downloads | Avg Downloads | New Today | Growth % |",
        "|---|----------|----------|----------------|---------------|-----------|----------|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['category']} | {r['projects']} | {r['total_downloads']:,} | "
            f"{r['avg_downloads']:,.0f} | {r['new_downloads']:,} | {r['growth_pct']:+.4f}% |"
        )
    return rows, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  3. CROSS-CATEGORY CORRELATION (boost factors)
# ═══════════════════════════════════════════════════════════════════


def analyze_cross_category_correlation(db):
    """Find category pairs that boost downloads when combined."""
    projects = db.get_all_projects()
    if not projects:
        return [], "No project data."
    projects.sort(key=lambda p: p.get("downloads", 0), reverse=True)
    top = projects[:1000]

    pairs = defaultdict(lambda: {"count": 0, "total_downloads": 0})
    singles = defaultdict(lambda: {"count": 0, "total_downloads": 0})
    loaders = {"fabric", "forge", "neoforge", "quilt"}

    for p in top:
        try:
            cats = json.loads(p.get("categories", "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cats, list) or len(cats) < 2:
            continue
        dl = p.get("downloads", 0)
        real = [c for c in cats if c not in loaders]
        for c in real:
            singles[c]["count"] += 1
            singles[c]["total_downloads"] += dl
        for i in range(len(real)):
            for j in range(i + 1, len(real)):
                key = tuple(sorted([real[i], real[j]]))
                pairs[key]["count"] += 1
                pairs[key]["total_downloads"] += dl

    results = []
    for pair, data in pairs.items():
        if data["count"] < 5:
            continue
        avg_pair = data["total_downloads"] / data["count"]
        c1_avg = singles[pair[0]]["total_downloads"] / max(singles[pair[0]]["count"], 1)
        c2_avg = singles[pair[1]]["total_downloads"] / max(singles[pair[1]]["count"], 1)
        combined_avg = (c1_avg + c2_avg) / 2 if c1_avg and c2_avg else 0
        boost = round(avg_pair / combined_avg, 1) if combined_avg > 0 else 0
        if boost >= 1.5:
            results.append(
                {
                    "combination": list(pair),
                    "avg_downloads": int(avg_pair),
                    "projects": data["count"],
                    "boost_factor": boost,
                }
            )
    results.sort(key=lambda x: x["boost_factor"], reverse=True)

    lines = [
        "## Cross-Category Synergies (Boost Factor)",
        "",
        "_Combinations that outperform their individual category averages._",
        "",
        "| Combination | Avg Downloads | Projects | Boost |",
        "|-------------|---------------|----------|-------|",
    ]
    for r in results[:TOP_N]:
        comb = " + ".join(r["combination"])
        lines.append(
            f"| {comb} | {r['avg_downloads']:,} | {r['projects']} | {r['boost_factor']}x |"
        )
    return results, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  4. CATEGORY + LOADER CROSS-ANALYSIS
# ═══════════════════════════════════════════════════════════════════


def analyze_category_loader_combos(db):
    """Best (category, loader) combinations by total downloads."""
    cursor = db.conn.execute("""
        SELECT p.project_id, p.categories, p.downloads, v.loaders
        FROM projects p
        JOIN versions v ON p.project_id = v.project_id
        WHERE p.categories IS NOT NULL AND v.loaders IS NOT NULL
    """)
    stats = defaultdict(lambda: {"total_downloads": 0, "count": 0})
    for row in cursor.fetchall():
        try:
            cats = json.loads(row["categories"])
            loaders = json.loads(row["loaders"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cats, list) or not isinstance(loaders, list):
            continue
        dl = row["downloads"] or 0
        for cat in cats:
            for loader in loaders:
                k = (cat, loader)
                stats[k]["total_downloads"] += dl
                stats[k]["count"] += 1

    if not stats:
        return [], "No version data (run version fetch first)."
    sorted_stats = sorted(
        stats.items(), key=lambda x: x[1]["total_downloads"], reverse=True
    )

    lines = [
        "## Category + Loader Combinations",
        "",
        "| Category | Loader | Projects | Total Downloads | Avg / Project |",
        "|----------|--------|---------|----------------|---------------|",
    ]
    results = []
    for (cat, loader), s in sorted_stats[:TOP_N]:
        avg = s["total_downloads"] / max(s["count"], 1)
        results.append(
            {
                "category": cat,
                "loader": loader,
                "projects": s["count"],
                "total_downloads": s["total_downloads"],
                "avg_downloads": int(avg),
            }
        )
        lines.append(
            f"| {cat} | {loader} | {s['count']} | {s['total_downloads']:,} | {avg:,.0f} |"
        )
    return results, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  5. MARKET SATURATION
# ═══════════════════════════════════════════════════════════════════


def analyze_market_saturation(db, today):
    """Identify oversaturated and under-served categories."""
    cats = db.get_categories_for_date(today)
    if not cats:
        return {"oversaturated": [], "underserved": []}, "No data."

    saturated = [
        c
        for c in cats
        if c["project_count"] >= MIN_PROJECTS_THRESHOLD
        and c["project_count"] > 100
        and c["avg_downloads"] < 50000
    ]
    underserved = [
        c
        for c in cats
        if c["project_count"] >= MIN_PROJECTS_THRESHOLD
        and c["project_count"] < 50
        and c["avg_downloads"] > 200000
    ]
    saturated.sort(key=lambda x: x["project_count"], reverse=True)
    underserved.sort(key=lambda x: x["avg_downloads"], reverse=True)

    lines = [
        "## Market Saturation",
        "",
        "### 🔴 Oversaturated (Avoid)",
        "| Category | Projects | Avg Downloads |",
        "|----------|---------|--------------|",
    ]
    if saturated:
        for c in saturated[:10]:
            lines.append(
                f"| {c['category']} | {c['project_count']} | {c['avg_downloads']:,.0f} |"
            )
    else:
        lines.append("| _None found_ | | |")

    lines.extend(
        [
            "",
            "### 🟢 Under-Served (Opportunity)",
            "",
            "| Category | Projects | Avg Downloads |",
            "|----------|---------|--------------|",
        ]
    )
    if underserved:
        for c in underserved[:10]:
            lines.append(
                f"| {c['category']} | {c['project_count']} | {c['avg_downloads']:,.0f} |"
            )
    else:
        lines.append("| _None found_ | | |")

    return {
        "oversaturated": [
            {
                "category": c["category"],
                "projects": c["project_count"],
                "avg_downloads": c["avg_downloads"],
            }
            for c in saturated[:10]
        ],
        "underserved": [
            {
                "category": c["category"],
                "projects": c["project_count"],
                "avg_downloads": c["avg_downloads"],
            }
            for c in underserved[:10]
        ],
    }, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  6. GROWTH MOMENTUM
# ═══════════════════════════════════════════════════════════════════


def analyze_growth_momentum(db, today, lookback=7):
    """Track accelerating vs decelerating categories."""
    cursor = db.conn.execute(
        "SELECT DISTINCT date FROM daily_category_stats WHERE date <= ? ORDER BY date DESC LIMIT ?",
        (today, lookback + 1),
    )
    dates = [r["date"] for r in cursor.fetchall()]
    dates.reverse()
    if len(dates) < 2:
        return [], "Need at least 2 days of data."

    date_data = {}
    for d in dates:
        date_data[d] = {c["category"]: c for c in db.get_categories_for_date(d)}

    all_cats = set()
    for dd in date_data.values():
        all_cats.update(dd.keys())

    rows = []
    for cat in all_cats:
        daily = []
        for i in range(len(dates) - 1):
            c1 = date_data[dates[i]].get(cat)
            c2 = date_data[dates[i + 1]].get(cat)
            if c1 and c2:
                daily.append(c2["total_new_downloads"])
        if len(daily) < 2:
            continue
        mid = len(daily) // 2
        first = sum(daily[:mid]) / max(mid, 1)
        second = sum(daily[mid:]) / max(len(daily) - mid, 1)
        momentum = ((second - first) / max(first, 1)) * 100

        if momentum > 20:
            signal = "🚀 Accelerating"
        elif momentum > 5:
            signal = "📈 Growing"
        elif momentum > -5:
            signal = "➡️ Stable"
        elif momentum > -20:
            signal = "📉 Declining"
        else:
            signal = "🚨 Plunging"

        rows.append(
            {
                "category": cat,
                "momentum_pct": round(momentum, 1),
                "signal": signal,
                "avg_daily_new": round(sum(daily) / max(len(daily), 1)),
            }
        )
    rows.sort(key=lambda r: r["momentum_pct"], reverse=True)

    lines = [
        "## Growth Momentum",
        "",
        f"_Based on last {min(lookback, len(dates) - 1)} days._",
        "",
        "| Category | Momentum % | Signal | Avg Daily New Downloads |",
        "|----------|-----------|--------|------------------------|",
    ]
    for r in rows[:TOP_N]:
        lines.append(
            f"| {r['category']} | {r['momentum_pct']:+.1f}% | {r['signal']} | {r['avg_daily_new']:,} |"
        )
    return rows, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  7. LOADER MARKET FIT
# ═══════════════════════════════════════════════════════════════════


def analyze_loader_market_fit(db):
    """Loader market share vs download share and efficiency ratio."""
    projects = db.get_all_projects()
    if not projects:
        return [], "No project data."
    total_projects = len(projects)
    total_downloads = sum(p.get("downloads", 0) for p in projects)

    loaders = ["fabric", "forge", "neoforge", "quilt"]
    stats = {l: {"projects": 0, "downloads": 0} for l in loaders}
    for p in projects:
        try:
            cats = json.loads(p.get("categories", "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cats, list):
            continue
        dl = p.get("downloads", 0)
        for l in loaders:
            if l in cats:
                stats[l]["projects"] += 1
                stats[l]["downloads"] += dl

    results = []
    for loader, s in stats.items():
        mkt_share = (s["projects"] / total_projects * 100) if total_projects else 0
        dl_share = (s["downloads"] / total_downloads * 100) if total_downloads else 0
        efficiency = round(dl_share / max(mkt_share, 0.01), 2)
        results.append(
            {
                "loader": loader,
                "market_share": round(mkt_share, 1),
                "download_share": round(dl_share, 1),
                "efficiency_ratio": efficiency,
                "projects": s["projects"],
                "total_downloads": s["downloads"],
            }
        )
    results.sort(key=lambda x: x["efficiency_ratio"], reverse=True)

    lines = [
        "## Loader Market Fit",
        "",
        "_Efficiency > 1 means projects on this loader outperform average._",
        "",
        "| Loader | Projects | Market Share | Download Share | Efficiency |",
        "|--------|---------|-------------|----------------|------------|",
    ]
    for r in results:
        lines.append(
            f"| {r['loader']} | {r['projects']} | {r['market_share']}% | {r['download_share']}% | {r['efficiency_ratio']} |"
        )
    return results, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  8. VERSION LIFECYCLE
# ═══════════════════════════════════════════════════════════════════


def analyze_version_lifecycle(db):
    """MC version lifecycle stages."""
    cursor = db.conn.execute("""
        SELECT project_id, game_versions, downloads FROM versions WHERE game_versions IS NOT NULL
    """)
    vstats = defaultdict(
        lambda: {"project_count": 0, "total_downloads": 0, "projects": set()}
    )
    for row in cursor.fetchall():
        try:
            gv = json.loads(row["game_versions"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(gv, list):
            continue
        for v in gv:
            parts = v.split(".")
            if len(parts) >= 2:
                key = ".".join(parts[:2]) if len(parts) == 2 else ".".join(parts[:3])
                vstats[key]["project_count"] += 1
                vstats[key]["total_downloads"] += row["downloads"]
                vstats[key]["projects"].add(row["project_id"])

    now = datetime.now()
    cursor2 = db.conn.execute(
        "SELECT project_id, date_created FROM projects WHERE date_created IS NOT NULL"
    )
    proj_dates = {r["project_id"]: r["date_created"] for r in cursor2.fetchall()}

    results = []
    for version, s in vstats.items():
        dates = []
        for pid in s["projects"]:
            if pid in proj_dates:
                try:
                    dates.append(datetime.fromisoformat(proj_dates[pid]))
                except (ValueError, TypeError):
                    pass
        avg_age_days = (
            (now.timestamp() - sum(d.timestamp() for d in dates) / len(dates)) / 86400
            if dates else 365
        )

        try:
            parts = version.split(".")
            major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            if major == 1 and minor >= 21:
                stage = "peak"
            elif major == 1 and minor >= 20:
                stage = "emerging" if avg_age_days < 180 else "peak"
            elif major == 1 and minor >= 18:
                stage = "mature"
            else:
                stage = "legacy"
            if stage == "peak" and avg_age_days > 365:
                stage = "mature"
            if stage == "emerging" and avg_age_days > 180:
                stage = "peak"
        except (ValueError, IndexError):
            stage = "mature"

        results.append(
            {
                "version": version,
                "stage": stage,
                "projects": s["project_count"],
                "total_downloads": s["total_downloads"],
            }
        )
    results.sort(key=lambda x: x["projects"], reverse=True)

    lines = [
        "## Version Lifecycle",
        "",
        "| Version | Stage | Projects | Total Downloads |",
        "|---------|-------|---------|----------------|",
    ]
    for r in results[:TOP_N]:
        lines.append(
            f"| {r['version']} | {r['stage']} | {r['projects']:,} | {r['total_downloads']:,} |"
        )
    return results, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  9. MARKET GAP ANALYSIS
# ═══════════════════════════════════════════════════════════════════


def analyze_market_gaps(db):
    """Find underserved category+loader combos (high demand, low supply)."""
    projects = db.get_all_projects()
    if not projects:
        return [], "No project data."

    cursor = db.conn.execute("""
        SELECT category, AVG(avg_downloads) as overall_avg
        FROM daily_category_stats GROUP BY category ORDER BY overall_avg DESC
    """)
    cat_avgs = {r["category"]: r["overall_avg"] for r in cursor.fetchall()}

    loaders = ["fabric", "forge", "neoforge", "quilt"]
    lc_counts = defaultdict(lambda: defaultdict(int))
    cat_counts = defaultdict(int)

    for p in projects:
        try:
            cats = json.loads(p.get("categories", "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cats, list):
            continue
        pl = [l for l in loaders if l in cats]
        pc = [c for c in cats if c not in loaders]
        for c in pc:
            cat_counts[c] += 1
            for l in pl:
                lc_counts[l][c] += 1

    results = []
    for cat, avg_dl in cat_avgs.items():
        if avg_dl < 10000:
            continue
        total = cat_counts.get(cat, 0)
        if total == 0:
            continue
        for loader in loaders:
            lc = lc_counts[loader].get(cat, 0)
            share = (lc / total) * 100
            if share < 10 and lc > 0:
                gap_score = round((avg_dl / 10000) * (10 - share), 1)
                results.append(
                    {
                        "category": cat,
                        "loader": loader,
                        "current_projects": lc,
                        "total_projects": total,
                        "avg_downloads": int(avg_dl),
                        "loader_share": round(share, 1),
                        "gap_score": gap_score,
                    }
                )
    results.sort(key=lambda x: x["gap_score"], reverse=True)

    lines = [
        "## Market Gaps (Underserved Category+Loader Combos)",
        "",
        "| Category | Loader | Current Projects | Total in Category | Avg Downloads | Loader Share | Gap Score |",
        "|----------|--------|-----------------|-------------------|---------------|-------------|-----------|",
    ]
    for r in results[:TOP_N]:
        lines.append(
            f"| {r['category']} | {r['loader']} | {r['current_projects']} | "
            f"{r['total_projects']} | {r['avg_downloads']:,} | {r['loader_share']}% | {r['gap_score']} |"
        )
    return results, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  10. INVESTMENT RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════


def generate_investment_recommendations(market_gaps, momentum, loader_fit):
    """Ranked 'what to build' list with expected downloads, competition, risk, ROI."""
    if not market_gaps:
        return [], "Insufficient data for recommendations."

    rising = {
        m["category"]
        for m in momentum
        if m["signal"] in ("🚀 Accelerating", "📈 Growing")
    }
    loader_eff = {l["loader"]: l["efficiency_ratio"] for l in loader_fit}

    recommendations = []
    for i, gap in enumerate(market_gaps[:10]):
        expected = int(gap["avg_downloads"] * 0.5)
        if gap["current_projects"] < 100:
            competition, risk = "Low", 3
        elif gap["current_projects"] < 500:
            competition, risk = "Medium", 5
        else:
            competition, risk = "High", 7

        if gap["category"] in rising and competition == "Low":
            roi = "High"
            risk = max(risk - 1, 1)
        elif competition == "High":
            roi = "Low"
            risk = min(risk + 1, 10)
        else:
            roi = "Medium"

        reasoning = []
        if gap["category"] in rising:
            reasoning.append(f"'{gap['category']}' is trending up")
        if loader_eff.get(gap["loader"], 0) > 1:
            reasoning.append(
                f"{gap['loader']} projects outperform per-project averages"
            )
        reasoning.append(
            f"only {gap['current_projects']} existing projects in this combo"
        )

        recommendations.append(
            {
                "rank": i + 1,
                "category": gap["category"],
                "loader": gap["loader"],
                "expected_downloads": expected,
                "competition": competition,
                "risk_score": risk,
                "roi": roi,
                "reasoning": "; ".join(reasoning),
            }
        )

    lines = [
        "## Investment Recommendations",
        "",
        "| Rank | What to Build | Loader | Expected Downloads | Competition | Risk | ROI | Reasoning |",
        "|------|--------------|--------|-------------------|-------------|------|-----|----------|",
    ]
    for r in recommendations:
        lines.append(
            f"| {r['rank']} | {r['category']} | {r['loader']} | {r['expected_downloads']:,} | "
            f"{r['competition']} | {r['risk_score']}/10 | {r['roi']} | {r['reasoning']} |"
        )
    return recommendations, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  TOP GROWING PROJECTS
# ═══════════════════════════════════════════════════════════════════


def analyze_top_growing_projects(db, today, limit=50):
    cursor = db.conn.execute(
        "SELECT project_id, date, downloads FROM daily_project_snapshots WHERE date = ? ORDER BY downloads DESC LIMIT 500",
        (today,),
    )
    today_snaps = {r["project_id"]: r for r in cursor.fetchall()}
    if not today_snaps:
        return [], "No snapshot data."

    prev = db.conn.execute(
        "SELECT DISTINCT date FROM daily_project_snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today,),
    ).fetchone()
    if not prev:
        return [], "Need previous day for comparison."
    prev_snaps = {
        r["project_id"]: r["downloads"]
        for r in db.conn.execute(
            "SELECT project_id, downloads FROM daily_project_snapshots WHERE date = ?",
            (prev["date"],),
        ).fetchall()
    }

    gains = []
    for pid, snap in today_snaps.items():
        pd = prev_snaps.get(pid, 0)
        gain = snap["downloads"] - pd
        gains.append((pid, snap["downloads"], pd, gain))
    gains.sort(key=lambda x: x[3], reverse=True)

    results = []
    for pid, td, yd, gain in gains[:limit]:
        proj = db.get_project(pid)
        if proj:
            try:
                cats = json.loads(proj.get("categories", "[]"))
                cat = ", ".join(cats[:2]) if cats else "N/A"
            except (json.JSONDecodeError, TypeError):
                cat = "N/A"
            results.append(
                {
                    "project_id": pid,
                    "title": proj.get("title", pid),
                    "category": cat,
                    "yesterday_downloads": yd,
                    "today_downloads": td,
                    "gain": gain,
                }
            )

    lines = [
        "## Top Growing Projects by Daily Gain",
        "",
        "| Project | Category | Yesterday | Today | Gain |",
        "|---------|----------|-----------|-------|------|",
    ]
    for r in results[:limit]:
        lines.append(
            f"| {r['title']} | {r['category']} | {r['yesterday_downloads']:,} | {r['today_downloads']:,} | {r['gain']:+,} |"
        )
    return results, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  EXECUTIVE SUMMARY
# ═══════════════════════════════════════════════════════════════════


def generate_executive_summary(opportunity, momentum, recommendations):
    if not opportunity:
        return "_Data collection in progress. Check back after a few daily runs._"
    top = opportunity[:3]
    lines = ["### 🎯 Top 3 Opportunities"]
    for i, r in enumerate(top, 1):
        lines.append(
            f"  **{i}. {r['category'].title()}** — {r['avg_downloads']:,.0f} avg downloads, {r['projects']} competitors (score: {r['opportunity_score']})"
        )
    if momentum:
        fast = [m for m in momentum if m["momentum_pct"] > 10][:3]
        if fast:
            lines.append("")
            lines.append("### ⚡ Trending Up")
            for f in fast:
                lines.append(
                    f"  - **{f['category'].title()}**: {f['momentum_pct']:+.1f}% ({f['signal']})"
                )
    if recommendations:
        lines.append("")
        lines.append("### 💡 Best Bets")
        for r in recommendations[:3]:
            lines.append(
                f"  - Build **{r['category']}** for **{r['loader']}** — est. {r['expected_downloads']:,} downloads, {r['roi']} ROI, {r['competition']} competition"
            )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════


def main():
    print("=== Modrinth Market Analytics Engine ===")
    today = get_current_date()
    db = Database("data/modrinth_tracker.db")

    all_projects = db.get_all_projects()
    ver_count = db.conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    print(f"Projects: {len(all_projects)} | Versions: {ver_count}")

    # Run all analyses
    print("  Categories & Opportunity...")
    opp_rows, opp_md = analyze_category_opportunity(db, today)
    rank_rows, rank_md = analyze_category_rankings(db, today)

    print("  Cross-category correlation...")
    corr_rows, corr_md = analyze_cross_category_correlation(db)

    print("  Category+Loader combos...")
    combo_rows, combo_md = analyze_category_loader_combos(db)

    print("  Market saturation...")
    sat_data, sat_md = analyze_market_saturation(db, today)

    print("  Growth momentum...")
    mom_rows, mom_md = analyze_growth_momentum(db, today)

    print("  Loader market fit...")
    lmf_rows, lmf_md = analyze_loader_market_fit(db)

    print("  Version lifecycle...")
    vl_rows, vl_md = analyze_version_lifecycle(db)

    print("  Market gaps...")
    gap_rows, gap_md = analyze_market_gaps(db)

    print("  Investment recommendations...")
    rec_rows, rec_md = generate_investment_recommendations(gap_rows, mom_rows, lmf_rows)

    print("  Top growing projects...")
    tgp_rows, tgp_md = analyze_top_growing_projects(db, today)

    # Executive summary
    exec_summary = generate_executive_summary(opp_rows, mom_rows, rec_rows)

    # ── Build combined JSON ────────────────────────────────────────
    json_data = {
        "report_date": today,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_projects": len(all_projects),
        "total_versions": ver_count,
        "executive_summary": {
            "top_opportunities": [
                {
                    "category": r["category"],
                    "avg_downloads": r["avg_downloads"],
                    "projects": r["projects"],
                    "opportunity_score": r["opportunity_score"],
                }
                for r in opp_rows[:5]
            ],
            "top_trending": [
                {
                    "category": r["category"],
                    "momentum_pct": r["momentum_pct"],
                    "signal": r["signal"],
                }
                for r in mom_rows[:5]
            ],
            "best_bets": [
                {
                    "category": r["category"],
                    "loader": r["loader"],
                    "expected_downloads": r["expected_downloads"],
                    "roi": r["roi"],
                    "competition": r["competition"],
                }
                for r in rec_rows[:5]
            ],
        },
        "category_opportunity": opp_rows[:TOP_N],
        "category_rankings": rank_rows,
        "cross_category_synergies": corr_rows[:TOP_N],
        "category_loader_combos": combo_rows,
        "market_saturation": sat_data,
        "growth_momentum": mom_rows[:TOP_N],
        "loader_market_fit": lmf_rows,
        "version_lifecycle": vl_rows[:TOP_N],
        "market_gaps": gap_rows[:TOP_N],
        "investment_recommendations": rec_rows,
        "top_growing_projects": tgp_rows[:50],
    }

    ensure_dir("reports")
    save_json(f"reports/analytics_{today}.json", json_data)
    save_json("reports/latest_analysis.json", json_data)
    print("Saved JSON reports.")

    # ── Build markdown report ──────────────────────────────────────
    sections = [
        f"# Modrinth Market Analytics Report — {today}",
        "",
        f"**Projects Tracked:** {len(all_projects):,} | **Versions:** {ver_count:,}",
        "",
        "---",
        "## Executive Summary",
        "",
        exec_summary,
        "",
        "---",
        rank_md,
        "",
        "---",
        opp_md,
        "",
        "---",
        corr_md,
        "",
        "---",
        combo_md,
        "",
        "---",
        sat_md,
        "",
        "---",
        mom_md,
        "",
        "---",
        lmf_md,
        "",
        "---",
        vl_md,
        "",
        "---",
        gap_md,
        "",
        "---",
        rec_md,
        "",
        "---",
        tgp_md,
        "",
        "---",
        f"_Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} by Modrinth Analytics Engine_",
    ]

    report_path = f"reports/analytics_{today}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sections))
    print(f"Saved markdown report to {report_path}")

    db.close()
    print("=== Analytics Complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
