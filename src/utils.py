#!/usr/bin/env python3
import json
import os
import time
from datetime import datetime, timezone, timedelta
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Beijing timezone (UTC+8) — all dates in the tracker use Beijing time
BEIJING_TZ = timezone(timedelta(hours=8))

MODRINTH_API_BASE = "https://api.modrinth.com/v2"
PAGE_SIZE = 100
MAX_OFFSET = 10000
RATE_LIMIT = 300


def create_session() -> requests.Session:
    """Create a requests Session with retry configuration and proper User-Agent."""
    session = requests.Session()

    # Configure retry strategy with exponential backoff
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": "itamio/AnalyticalMinecraftBasedRevenueEngineering/1.0.0 (contact: github.com/Sekai0NI0itamio)"
    })

    return session


def rate_limit_sleep(resp_headers):
    """Sleep if we're approaching the rate limit based on response headers."""
    # Parse rate limit headers
    remaining = resp_headers.get("X-Ratelimit-Remaining")
    reset = resp_headers.get("X-Ratelimit-Reset")

    if remaining is not None and reset is not None:
        try:
            remaining = int(remaining)
            reset_time = int(reset)
            current_time = int(time.time())

            # If we have less than 10 requests remaining, wait until reset
            if remaining < 10:
                wait_time = reset_time - current_time
                if wait_time > 0:
                    time.sleep(wait_time + 1)
        except (ValueError, TypeError):
            pass


def load_json(path):
    """Load JSON from a file."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    """Save JSON to a file with proper formatting."""
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def ensure_dir(path):
    """Create directory if it doesn't exist."""
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def get_current_date() -> str:
    """Get current date in YYYY-MM-DD format (Beijing time, UTC+8)."""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def get_current_datetime() -> str:
    """Get current datetime in ISO format (Beijing time, UTC+8)."""
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def get_timestamp() -> str:
    """Get filesystem-safe timestamp: YYYY-MM-DDTHH-MM-SS (Beijing time)."""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%dT%H-%M-%S")


def get_project_type_dir(project_type: str) -> str:
    """Get the data directory for a specific project type."""
    return f"data/{project_type}"


def get_raw_dir(project_type: str) -> str:
    """Get the raw snapshots directory for a project type."""
    return f"data/{project_type}/raw"


def get_analysis_dir(project_type: str) -> str:
    """Get the analysis stack directory for a project type."""
    return f"data/{project_type}/analysis"


def get_db_path(project_type: str) -> str:
    """Get the SQLite DB path for a project type."""
    return f"data/{project_type}/{project_type}.db"
