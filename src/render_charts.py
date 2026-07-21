#!/usr/bin/env python3
"""
Render PNG charts from analysis data.

Both daily and hourly modes read from data/{type}/latest_analysis.json.
The same analysis file is used — the mode just selects which charts to render.

Daily mode  (--mode daily) : 7 charts — trend, categories, loaders,
    distribution, concentration, top_projects, recommendations

Hourly mode (--mode hourly): 5 charts — velocity, prediction,
    top_movers_2h, velocity_by_category, hourly_heatmap

Charts are saved to data/{project_type}/charts/ as PNG files.

Theme: dark background (#0a0a0a), white text, colored bars.
"""
import argparse
import sys
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import numpy as np

from utils import (
    load_json, ensure_dir, get_project_type_dir, get_raw_dir, BEIJING_TZ,
    list_snapshot_files,
)

# ── Theme & layout constants ──────────────────────────────────────
BG_COLOR = "#0a0a0a"
TEXT_COLOR = "#ffffff"
GRID_COLOR = "#333333"
BAR_COLORS = [
    "#4f9cff", "#ff6b6b", "#51cf66", "#ffd43b", "#cc5de8",
    "#ff922b", "#22b8cf", "#f06595", "#94d82d", "#5c7cfa",
]
FIG_SIZE = (10, 5)
DPI = 150


# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════


def format_number(n):
    """Format large numbers compactly: 55B, 1.2M, 45K."""
    try:
        n = float(n)
    except (ValueError, TypeError):
        return str(n)
    if abs(n) >= 1e9:
        return f"{n / 1e9:.1f}B"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.1f}M"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{n:.0f}"


def number_formatter(_x, _pos):
    return format_number(_x)


def parse_snapshot_time(timestamp_str):
    """Parse a snapshot timestamp 'YYYY-MM-DDTHH-MM-SS' as Beijing time."""
    if not timestamp_str:
        return None
    if "T" in timestamp_str:
        date_part, time_part = timestamp_str.split("T", 1)
        time_part = time_part.replace("-", ":")
        timestamp_str = f"{date_part}T{time_part}"
    try:
        dt = datetime.fromisoformat(timestamp_str)
        return dt.replace(tzinfo=BEIJING_TZ)
    except (ValueError, TypeError):
        return None


def load_latest_raw(project_type):
    """Load the most recent raw snapshot."""
    raw_dir = get_raw_dir(project_type)
    files = list_snapshot_files(raw_dir)
    if not files:
        return None
    return load_json(files[-1])


def load_recent_raw(project_type, hours=24):
    """Load raw snapshots from the last `hours`, sorted chronologically."""
    raw_dir = get_raw_dir(project_type)
    files = list_snapshot_files(raw_dir)
    cutoff = datetime.now(BEIJING_TZ) - timedelta(hours=hours)
    snaps = []
    for f in files:
        data = load_json(f)
        if not data:
            continue
        st = parse_snapshot_time(data.get("timestamp", ""))
        if st and st >= cutoff:
            snaps.append(data)
    return snaps


def style_axes(ax):
    """Apply dark-theme styling to an axes."""
    ax.set_facecolor(BG_COLOR)
    ax.tick_params(colors=TEXT_COLOR)
    for spine in ax.spines.values():
        spine.set_color(GRID_COLOR)
    ax.xaxis.label.set_color(TEXT_COLOR)
    ax.yaxis.label.set_color(TEXT_COLOR)
    ax.title.set_color(TEXT_COLOR)


def new_figure():
    """Create a new dark-themed figure and axes."""
    fig, ax = plt.subplots(figsize=FIG_SIZE, dpi=DPI)
    fig.patch.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)
    return fig, ax


def save_chart(fig, path):
    """Apply tight_layout and save the figure."""
    fig.tight_layout()
    fig.savefig(path, facecolor=BG_COLOR, dpi=DPI)
    plt.close(fig)
    print(f"  Saved {path}")


def show_no_data(ax, message="No data available"):
    """Render a 'No data available' message centered on the axes."""
    ax.text(0.5, 0.5, message, ha="center", va="center",
            color=TEXT_COLOR, fontsize=16, transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


def bar_color(i):
    return BAR_COLORS[i % len(BAR_COLORS)]


# ═══════════════════════════════════════════════════════════════════
#  DAILY MODE CHARTS (7 charts)
# ═══════════════════════════════════════════════════════════════════


def render_trend(analysis, charts_dir):
    """1. trend.png — total_downloads over time with baseline reference line."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Total Downloads Over Time (24h Daily)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Total Downloads")

    trend = analysis.get("trend", [])
    if not trend:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/trend.png")
        return

    dates = [t.get("date", "") for t in trend]
    downloads = [t.get("total_downloads", 0) for t in trend]
    ax.plot(dates, downloads, marker="o", color=BAR_COLORS[0],
            linewidth=2, label="Total downloads")

    if len(trend) == 1:
        ax.text(0.5, 0.7, "Baseline established", ha="center", va="center",
                color=TEXT_COLOR, fontsize=14, transform=ax.transAxes)
    else:
        ax.axhline(y=downloads[0], color=BAR_COLORS[1], linestyle="--",
                   linewidth=1.5, label="Baseline")

    ax.yaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    ax.legend(facecolor=BG_COLOR, edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR)
    save_chart(fig, f"{charts_dir}/trend.png")


def render_categories(analysis, charts_dir):
    """2. categories.png — top 10 content categories by total_downloads."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Top 10 Content Categories by Downloads")
    ax.set_xlabel("Total Downloads")
    ax.set_ylabel("Category")

    cats = analysis.get("category_rankings", [])[:10]
    if not cats:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/categories.png")
        return

    cats = list(reversed(cats))
    names = [c.get("category", "") for c in cats]
    totals = [c.get("total_downloads", 0) for c in cats]
    shares = [c.get("market_share", 0.0) for c in cats]
    colors = [bar_color(i) for i in range(len(cats))]

    bars = ax.barh(names, totals, color=colors)
    ax.xaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, axis="x", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)

    for bar, share in zip(bars, shares):
        w = bar.get_width()
        ax.text(w, bar.get_y() + bar.get_height() / 2,
                f"  {share:.1f}%", va="center", color=TEXT_COLOR, fontsize=9)
    save_chart(fig, f"{charts_dir}/categories.png")


def render_loaders(analysis, charts_dir):
    """3. loaders.png — top 10 loaders by total_downloads."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Top 10 Loaders by Downloads")
    ax.set_xlabel("Total Downloads")
    ax.set_ylabel("Loader")

    loaders = analysis.get("loader_rankings", [])[:10]
    if not loaders:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/loaders.png")
        return

    loaders = list(reversed(loaders))
    names = [l.get("loader", "") for l in loaders]
    totals = [l.get("total_downloads", 0) for l in loaders]
    colors = [bar_color(i) for i in range(len(loaders))]

    ax.barh(names, totals, color=colors)
    ax.xaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, axis="x", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
    save_chart(fig, f"{charts_dir}/loaders.png")


def render_distribution(analysis, project_type, charts_dir):
    """4. distribution.png — histogram of project downloads (log scale x)."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Distribution of Project Downloads")
    ax.set_xlabel("Downloads (log scale)")
    ax.set_ylabel("Number of Projects")

    raw = load_latest_raw(project_type)
    projects = (raw or {}).get("projects", [])
    downloads = [p.get("downloads", 0) for p in projects if p.get("downloads", 0) > 0]

    if not downloads:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/distribution.png")
        return

    lo = max(1, min(downloads))
    hi = max(downloads)
    bins = np.logspace(np.log10(lo), np.log10(hi), 50)
    ax.hist(downloads, bins=bins, color=BAR_COLORS[0], edgecolor=BG_COLOR)
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)

    dist = analysis.get("distribution", {})
    lines = [
        (dist.get("mean", 0), "Mean", BAR_COLORS[1]),
        (dist.get("median", 0), "Median", BAR_COLORS[2]),
        (dist.get("p95", 0), "P95", BAR_COLORS[3]),
        (dist.get("p99", 0), "P99", BAR_COLORS[4]),
    ]
    for val, label, color in lines:
        if val and val > 0:
            ax.axvline(val, color=color, linestyle="--", linewidth=1.5, label=label)
    ax.legend(facecolor=BG_COLOR, edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR)
    save_chart(fig, f"{charts_dir}/distribution.png")


def render_concentration(analysis, charts_dir):
    """5. concentration.png — HHI, CR4, CR10 with concentration-level annotation."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Market Concentration")
    ax.set_xlabel("Metric")
    ax.set_ylabel("Value")

    conc = analysis.get("concentration", {})
    hhi = conc.get("hhi", 0.0)
    cr4 = conc.get("cr4", 0.0)
    cr10 = conc.get("cr10", 0.0)

    if not conc:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/concentration.png")
        return

    labels = ["HHI", "CR4", "CR10"]
    values = [hhi, cr4, cr10]
    colors = [bar_color(0), bar_color(1), bar_color(2)]
    bars = ax.bar(labels, values, color=colors)
    ax.grid(True, axis="y", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)

    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"{val:.1f}", ha="center", va="bottom", color=TEXT_COLOR, fontsize=10)

    if hhi < 1500:
        level = "Low concentration"
    elif hhi < 2500:
        level = "Moderate concentration"
    else:
        level = "High concentration"
    ax.text(0.98, 0.95, level, ha="right", va="top", transform=ax.transAxes,
            color=TEXT_COLOR, fontsize=12,
            bbox=dict(facecolor=BG_COLOR, edgecolor=GRID_COLOR, boxstyle="round,pad=0.4"))
    save_chart(fig, f"{charts_dir}/concentration.png")


def render_top_projects(analysis, charts_dir):
    """6. top_projects.png — top 10 projects by delta_downloads."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Top 10 Projects by Growth")
    ax.set_xlabel("Downloads")
    ax.set_ylabel("Project")

    top = analysis.get("top_projects", [])[:10]
    if not top:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/top_projects.png")
        return

    use_delta = any(p.get("delta_downloads", 0) > 0 for p in top)
    metric = "delta_downloads" if use_delta else "current_downloads"
    label = "Delta Downloads" if use_delta else "Total Downloads"

    top = list(reversed(top))
    names = [p.get("title", "") or p.get("slug", "") for p in top]
    values = [p.get(metric, 0) for p in top]
    colors = [bar_color(i) for i in range(len(top))]

    ax.barh(names, values, color=colors)
    ax.xaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, axis="x", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
    ax.set_xlabel(label)
    save_chart(fig, f"{charts_dir}/top_projects.png")


def render_recommendations(analysis, charts_dir):
    """7. recommendations.png — top 5 categories by opportunity_score."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Top 5 Category Opportunities")
    ax.set_xlabel("Opportunity Score")
    ax.set_ylabel("Category")

    recs = analysis.get("recommendations", [])[:5]
    if not recs:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/recommendations.png")
        return

    recs = list(reversed(recs))
    names = [r.get("category", "") for r in recs]
    scores = [r.get("opportunity_score", 0) for r in recs]
    colors = [bar_color(i) for i in range(len(recs))]

    ax.barh(names, scores, color=colors)
    ax.grid(True, axis="x", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
    save_chart(fig, f"{charts_dir}/recommendations.png")


# ═══════════════════════════════════════════════════════════════════
#  HOURLY MODE CHARTS (5 charts)
# ═══════════════════════════════════════════════════════════════════


def render_velocity(analysis, charts_dir):
    """1. velocity.png — top 10 movers by downloads_per_hour in last 2h."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Top 10 Movers — Velocity (Last 2h)")
    ax.set_xlabel("Downloads per Hour")
    ax.set_ylabel("Project")

    movers = analysis.get("top_movers", [])[:10]
    if not movers:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/velocity.png")
        return

    movers = list(reversed(movers))
    names = [m.get("title", "") or m.get("slug", "") for m in movers]
    values = [m.get("downloads_per_hour", 0) for m in movers]
    colors = [bar_color(i) for i in range(len(movers))]

    ax.barh(names, values, color=colors)
    ax.xaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, axis="x", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
    save_chart(fig, f"{charts_dir}/velocity.png")


def render_prediction(analysis, project_type, charts_dir):
    """2. prediction.png — actual total_downloads + dashed predicted trajectory."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Download Trajectory & Prediction")
    ax.set_xlabel("Time (Beijing)")
    ax.set_ylabel("Total Downloads")

    snaps = load_recent_raw(project_type, hours=24)
    if not snaps:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/prediction.png")
        return

    times = [parse_snapshot_time(s.get("timestamp", "")) for s in snaps]
    totals = [s.get("total_downloads", 0) for s in snaps]

    pts = [(t, v) for t, v in zip(times, totals) if t is not None]
    if not pts:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/prediction.png")
        return
    times, totals = zip(*pts)

    ax.plot(times, totals, marker="o", color=BAR_COLORS[0],
            linewidth=2, label="Actual")

    # Predicted trajectory to the next main hour
    velocity = analysis.get("velocity", {})
    predicted_total = velocity.get("predicted_daily_total", totals[-1])
    last_time = times[-1]
    # Extend 2 hours ahead (to next recording)
    end_time = last_time + timedelta(hours=2)
    ax.plot([last_time, end_time], [totals[-1], predicted_total],
            linestyle="--", color=BAR_COLORS[1], linewidth=2, label="Predicted")
    ax.scatter([end_time], [predicted_total], color=BAR_COLORS[1], zorder=5)

    ax.yaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
    fig.autofmt_xdate()
    ax.legend(facecolor=BG_COLOR, edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR)
    save_chart(fig, f"{charts_dir}/prediction.png")


def render_top_movers(analysis, charts_dir):
    """3. top_movers_2h.png — top 10 projects by 2h download delta."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Top 10 Movers (Last 2h)")
    ax.set_xlabel("Downloads Gained (2h)")
    ax.set_ylabel("Project")

    movers = analysis.get("top_movers", [])[:10]
    if not movers:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/top_movers_2h.png")
        return

    movers = list(reversed(movers))
    names = [m.get("title", "") or m.get("slug", "") for m in movers]
    values = [m.get("delta_downloads", 0) for m in movers]
    colors = [bar_color(i) for i in range(len(movers))]

    ax.barh(names, values, color=colors)
    ax.xaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, axis="x", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
    save_chart(fig, f"{charts_dir}/top_movers_2h.png")


def render_velocity_by_category(analysis, charts_dir):
    """4. velocity_by_category.png — top 10 categories by velocity."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("Top 10 Categories by Velocity (2h)")
    ax.set_xlabel("Velocity (downloads/hour)")
    ax.set_ylabel("Category")

    cats = analysis.get("velocity_by_category", [])[:10]
    if not cats:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/velocity_by_category.png")
        return

    cats = list(reversed(cats))
    names = [c.get("category", "") for c in cats]
    values = [c.get("downloads_per_hour", 0) for c in cats]
    colors = [bar_color(i) for i in range(len(cats))]

    ax.barh(names, values, color=colors)
    ax.xaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, axis="x", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
    save_chart(fig, f"{charts_dir}/velocity_by_category.png")


def render_hourly_heatmap(analysis, charts_dir):
    """5. hourly_heatmap.png — velocity summary card."""
    fig, ax = new_figure()
    style_axes(ax)
    ax.set_title("2-Hour Velocity Summary")
    ax.set_xlabel("Metric")
    ax.set_ylabel("Value")

    v = analysis.get("velocity", {})
    if not v:
        show_no_data(ax)
        save_chart(fig, f"{charts_dir}/hourly_heatmap.png")
        return

    labels = ["Total Delta", "Downloads/Hour", "Predicted Daily"]
    values = [
        v.get("total_delta", 0),
        v.get("downloads_per_hour", 0),
        v.get("predicted_daily_total", 0),
    ]
    colors = [bar_color(0), bar_color(1), bar_color(2)]
    bars = ax.bar(labels, values, color=colors)
    ax.yaxis.set_major_formatter(FuncFormatter(number_formatter))
    ax.grid(True, axis="y", color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)

    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"{format_number(val)}", ha="center", va="bottom",
                color=TEXT_COLOR, fontsize=10)

    confidence = v.get("confidence", "low")
    ax.text(0.98, 0.95, f"Confidence: {confidence}", ha="right", va="top",
            transform=ax.transAxes, color=TEXT_COLOR, fontsize=12,
            bbox=dict(facecolor=BG_COLOR, edgecolor=GRID_COLOR, boxstyle="round,pad=0.4"))
    save_chart(fig, f"{charts_dir}/hourly_heatmap.png")


def render_growth_trend(analysis, charts_dir):
    """8. growth_trend.png — 7-day daily increases for totals, top categories, and top VL pairs."""
    trend_history = analysis.get("trend_history", [])
    category_trend_history = analysis.get("category_trend_history", {})
    vl_trend_history = analysis.get("vl_trend_history", {})

    if not trend_history or len(trend_history) < 2:
        fig, ax = plt.subplots(figsize=FIG_SIZE, dpi=DPI)
        fig.patch.set_facecolor(BG_COLOR)
        ax.set_facecolor(BG_COLOR)
        ax.text(0.5, 0.5, "Collecting data — need at least 2 daily snapshots",
                ha="center", va="center", color=TEXT_COLOR, fontsize=14, transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        save_chart(fig, f"{charts_dir}/growth_trend.png")
        return

    dates = [t.get("date", "") for t in trend_history]
    n_points = len(dates)

    # Select top 5 categories and top 5 VL pairs by total new_downloads across the window
    top_cats = sorted(
        category_trend_history.items(),
        key=lambda x: sum(e.get("new_downloads", 0) for e in x[1]),
        reverse=True,
    )[:5]

    top_vls = sorted(
        vl_trend_history.items(),
        key=lambda x: sum(e.get("delta_downloads", 0) for e in x[1]),
        reverse=True,
    )[:5]

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), dpi=DPI, sharex=True)
    fig.patch.set_facecolor(BG_COLOR)
    for ax in axes:
        ax.set_facecolor(BG_COLOR)
        ax.grid(True, color=GRID_COLOR, linestyle="-", linewidth=0.5, alpha=0.5)
        ax.tick_params(colors=TEXT_COLOR)
        for spine in ax.spines.values():
            spine.set_color(GRID_COLOR)
        ax.xaxis.label.set_color(TEXT_COLOR)
        ax.yaxis.label.set_color(TEXT_COLOR)
        ax.title.set_color(TEXT_COLOR)
        ax.yaxis.set_major_formatter(FuncFormatter(number_formatter))

    # ── Top: total daily increase ───────────────────────────────────
    ax = axes[0]
    ax.set_title("Total Daily Downloads Increase (7-day)", fontsize=12)
    ax.set_ylabel("Downloads gained")
    new_dls = [t.get("new_downloads", 0) for t in trend_history]
    ax.fill_between(range(n_points), new_dls, alpha=0.15, color=BAR_COLORS[0])
    ax.plot(range(n_points), new_dls, marker="o", color=BAR_COLORS[0], linewidth=2, markersize=5)
    ax.set_xticks(range(n_points))
    ax.set_xticklabels(dates, rotation=45, ha="right", fontsize=9)

    # ── Middle: top categories ──────────────────────────────────────
    ax = axes[1]
    ax.set_title("Top Categories — Daily Increase", fontsize=12)
    ax.set_ylabel("Downloads gained")
    for i, (cat, entries) in enumerate(top_cats):
        color = bar_color(i)
        vals = [e.get("new_downloads", 0) for e in entries]
        # Pad with NaN if this category has fewer data points
        padded = vals + [None] * (n_points - len(vals))
        ax.plot(range(n_points), padded, marker="o", color=color, linewidth=1.5, label=cat, markersize=4)
    ax.set_xticks(range(n_points))
    ax.set_xticklabels(dates, rotation=45, ha="right", fontsize=9)
    ax.legend(facecolor=BG_COLOR, edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR, fontsize=8)

    # ── Bottom: top VL pairs ────────────────────────────────────────
    ax = axes[2]
    ax.set_title("Top Version+Loader — Daily Increase", fontsize=12)
    ax.set_ylabel("Downloads gained")
    ax.set_xlabel("Date")
    for i, (vl_key, entries) in enumerate(top_vls):
        color = bar_color(i)
        gv, loader = vl_key.split("\u0001") if "\u0001" in vl_key else (vl_key, "")
        label = f"{gv} + {loader}" if loader else gv
        vals = [e.get("delta_downloads", 0) for e in entries]
        padded = vals + [None] * (n_points - len(vals))
        ax.plot(range(n_points), padded, marker="o", color=color, linewidth=1.5, label=label, markersize=4)
    ax.set_xticks(range(n_points))
    ax.set_xticklabels(dates, rotation=45, ha="right", fontsize=9)
    ax.legend(facecolor=BG_COLOR, edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR, fontsize=8)

    fig.tight_layout()
    fig.savefig(f"{charts_dir}/growth_trend.png", facecolor=BG_COLOR, dpi=DPI)
    plt.close(fig)
    print(f"  Saved {charts_dir}/growth_trend.png")


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Render PNG charts from analysis data")
    parser.add_argument(
        "--project-type", required=True,
        choices=["mod", "modpack", "resourcepack", "shader", "datapack", "plugin"],
        help="Project type to render charts for",
    )
    parser.add_argument(
        "--mode", required=True, choices=["daily", "hourly"],
        help="Analysis mode: daily (24h) or hourly (2h)",
    )
    args = parser.parse_args()
    project_type = args.project_type
    mode = args.mode

    print(f"=== Render Charts ({project_type}, mode={mode}) ===")

    type_dir = get_project_type_dir(project_type)
    analysis_path = f"{type_dir}/latest_analysis.json"

    analysis = load_json(analysis_path)
    if not analysis:
        print(f"Error: analysis file not found at {analysis_path}. "
              f"Run analyze.py first.")
        return 1

    charts_dir = f"{type_dir}/charts"
    ensure_dir(charts_dir)

    if mode == "daily":
        render_trend(analysis, charts_dir)
        render_categories(analysis, charts_dir)
        render_loaders(analysis, charts_dir)
        render_distribution(analysis, project_type, charts_dir)
        render_concentration(analysis, charts_dir)
        render_top_projects(analysis, charts_dir)
        render_recommendations(analysis, charts_dir)
        render_growth_trend(analysis, charts_dir)
    else:
        render_velocity(analysis, charts_dir)
        render_prediction(analysis, project_type, charts_dir)
        render_top_movers(analysis, charts_dir)
        render_velocity_by_category(analysis, charts_dir)
        render_hourly_heatmap(analysis, charts_dir)

    print(f"=== Render Charts ({project_type}, mode={mode}) complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())