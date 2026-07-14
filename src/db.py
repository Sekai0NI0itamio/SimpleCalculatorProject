#!/usr/bin/env python3
import json
import sqlite3
from datetime import datetime


class Database:
    """SQLite database manager for the Modrinth Tracker.
    Each project type has its own separate database file."""

    def __init__(self, db_path: str):
        """Initialize database connection and create tables.
        db_path can be a full path or just a project_type (in which case
        data/{project_type}/{project_type}.db is used)."""
        # If just a project type is passed, build the full path
        if "/" not in db_path and not db_path.endswith(".db"):
            from utils import get_db_path, ensure_dir
            ptype = db_path
            db_path = get_db_path(ptype)
            ensure_dir(f"data/{ptype}")
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self):
        """Create all required tables if they don't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                categories TEXT,
                client_side TEXT,
                server_side TEXT,
                project_type TEXT DEFAULT 'mod',
                downloads INTEGER DEFAULT 0,
                follows INTEGER DEFAULT 0,
                icon_url TEXT,
                date_created TEXT,
                date_modified TEXT,
                first_fetched TEXT DEFAULT (datetime('now')),
                last_updated TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS versions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                version_number TEXT NOT NULL,
                name TEXT,
                version_type TEXT,
                game_versions TEXT,
                loaders TEXT,
                downloads INTEGER DEFAULT 0,
                files TEXT,
                date_published TEXT,
                first_fetched TEXT DEFAULT (datetime('now')),
                last_updated TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_project_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                date TEXT NOT NULL,
                downloads INTEGER DEFAULT 0,
                follows INTEGER DEFAULT 0,
                UNIQUE(project_id, date)
            );

            CREATE TABLE IF NOT EXISTS daily_version_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id TEXT NOT NULL,
                date TEXT NOT NULL,
                downloads INTEGER DEFAULT 0,
                UNIQUE(version_id, date)
            );

            CREATE TABLE IF NOT EXISTS daily_category_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                date TEXT NOT NULL,
                total_downloads INTEGER DEFAULT 0,
                project_count INTEGER DEFAULT 0,
                avg_downloads REAL DEFAULT 0,
                total_new_downloads INTEGER DEFAULT 0,
                UNIQUE(category, date)
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self.conn.commit()

    # ── Baseline tracking ──────────────────────────────────────────

    def get_baseline_date(self) -> str | None:
        """Get the baseline date from metadata, or None if not set."""
        return self.get_metadata("baseline_date")

    def set_baseline_date(self, date: str):
        """Set the baseline date in metadata."""
        self.set_metadata("baseline_date", date)

    def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key, or None if not set."""
        cursor = self.conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        return row["value"] if row else None

    def set_metadata(self, key: str, value: str):
        """Set a metadata value."""
        self.conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def reset_baseline(self, new_date: str):
        """Reset the baseline: clear all snapshots and category stats,
        then set the new baseline date. Used when the project type scope
        changes (e.g. adding datapacks, resourcepacks, etc.)."""
        self.conn.execute("DELETE FROM daily_project_snapshots")
        self.conn.execute("DELETE FROM daily_version_snapshots")
        self.conn.execute("DELETE FROM daily_category_stats")
        self.set_baseline_date(new_date)
        print(f"  Baseline reset to {new_date} — old snapshots cleared")

    # ── Project CRUD ──────────────────────────────────────────────

    def upsert_project(self, data: dict):
        """Insert or update a project record."""
        self.conn.execute("""
            INSERT INTO projects (
                project_id, slug, title, description, categories,
                client_side, server_side, project_type, downloads,
                follows, icon_url, date_created, date_modified,
                last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(project_id) DO UPDATE SET
                slug = excluded.slug,
                title = excluded.title,
                description = excluded.description,
                categories = excluded.categories,
                client_side = excluded.client_side,
                server_side = excluded.server_side,
                project_type = excluded.project_type,
                downloads = excluded.downloads,
                follows = excluded.follows,
                icon_url = excluded.icon_url,
                date_created = excluded.date_created,
                date_modified = excluded.date_modified,
                last_updated = datetime('now')
        """, (
            data.get("project_id"),
            data.get("slug"),
            data.get("title"),
            data.get("description"),
            data.get("categories"),
            data.get("client_side"),
            data.get("server_side"),
            data.get("project_type", "mod"),
            data.get("downloads", 0),
            data.get("follows", 0),
            data.get("icon_url"),
            data.get("date_created"),
            data.get("date_modified"),
        ))
        self.conn.commit()

    def upsert_version(self, data: dict):
        """Insert or update a version record."""
        self.conn.execute("""
            INSERT INTO versions (
                id, project_id, version_number, name, version_type,
                game_versions, loaders, downloads, files,
                date_published, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                project_id = excluded.project_id,
                version_number = excluded.version_number,
                name = excluded.name,
                version_type = excluded.version_type,
                game_versions = excluded.game_versions,
                loaders = excluded.loaders,
                downloads = excluded.downloads,
                files = excluded.files,
                date_published = excluded.date_published,
                last_updated = datetime('now')
        """, (
            data.get("id"),
            data.get("project_id"),
            data.get("version_number"),
            data.get("name"),
            data.get("version_type"),
            data.get("game_versions"),
            data.get("loaders"),
            data.get("downloads", 0),
            data.get("files"),
            data.get("date_published"),
        ))
        self.conn.commit()

    # ── Daily snapshots ───────────────────────────────────────────

    def record_project_snapshot(self, project_id, date, downloads, follows):
        """Record a daily snapshot for a project."""
        self.conn.execute("""
            INSERT INTO daily_project_snapshots (project_id, date, downloads, follows)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project_id, date) DO UPDATE SET
                downloads = excluded.downloads,
                follows = excluded.follows
        """, (project_id, date, downloads, follows))
        self.conn.commit()

    def record_version_snapshot(self, version_id, date, downloads):
        """Record a daily snapshot for a version."""
        self.conn.execute("""
            INSERT INTO daily_version_snapshots (version_id, date, downloads)
            VALUES (?, ?, ?)
            ON CONFLICT(version_id, date) DO UPDATE SET
                downloads = excluded.downloads
        """, (version_id, date, downloads))
        self.conn.commit()

    def record_category_stats(self, category, date, total_downloads, project_count, avg_downloads, new_downloads):
        """Record daily category statistics."""
        self.conn.execute("""
            INSERT INTO daily_category_stats (category, date, total_downloads, project_count, avg_downloads, total_new_downloads)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(category, date) DO UPDATE SET
                total_downloads = excluded.total_downloads,
                project_count = excluded.project_count,
                avg_downloads = excluded.avg_downloads,
                total_new_downloads = excluded.total_new_downloads
        """, (category, date, total_downloads, project_count, avg_downloads, new_downloads))
        self.conn.commit()

    # ── Baseline delta queries ────────────────────────────────────

    def get_baseline_project_snapshots(self) -> dict[str, int]:
        """Get the baseline download counts for all projects.
        Returns dict of {project_id: downloads_at_baseline}."""
        baseline = self.get_baseline_date()
        if not baseline:
            return {}
        cursor = self.conn.execute(
            "SELECT project_id, downloads FROM daily_project_snapshots WHERE date = ?",
            (baseline,),
        )
        return {row["project_id"]: row["downloads"] for row in cursor.fetchall()}

    def get_baseline_version_snapshots(self) -> dict[str, int]:
        """Get the baseline download counts for all versions.
        Returns dict of {version_id: downloads_at_baseline}."""
        baseline = self.get_baseline_date()
        if not baseline:
            return {}
        cursor = self.conn.execute(
            "SELECT version_id, downloads FROM daily_version_snapshots WHERE date = ?",
            (baseline,),
        )
        return {row["version_id"]: row["downloads"] for row in cursor.fetchall()}

    def get_baseline_category_stats(self) -> dict[str, dict]:
        """Get the baseline category stats.
        Returns dict of {category: {total_downloads, project_count, avg_downloads}}."""
        baseline = self.get_baseline_date()
        if not baseline:
            return {}
        cursor = self.conn.execute(
            "SELECT * FROM daily_category_stats WHERE date = ?",
            (baseline,),
        )
        return {row["category"]: dict(row) for row in cursor.fetchall()}

    def get_latest_project_snapshots(self, date: str) -> dict[str, int]:
        """Get the latest download counts for all projects on a given date."""
        cursor = self.conn.execute(
            "SELECT project_id, downloads FROM daily_project_snapshots WHERE date = ?",
            (date,),
        )
        return {row["project_id"]: row["downloads"] for row in cursor.fetchall()}

    def get_latest_version_snapshots(self, date: str) -> dict[str, int]:
        """Get the latest download counts for all versions on a given date."""
        cursor = self.conn.execute(
            "SELECT version_id, downloads FROM daily_version_snapshots WHERE date = ?",
            (date,),
        )
        return {row["version_id"]: row["downloads"] for row in cursor.fetchall()}

    # ── Query helpers ─────────────────────────────────────────────

    def get_project(self, project_id) -> dict | None:
        """Get a project by ID, returns dict or None."""
        cursor = self.conn.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def get_all_projects(self) -> list:
        """Get all projects as a list of dicts."""
        cursor = self.conn.execute("SELECT * FROM projects")
        return [dict(row) for row in cursor.fetchall()]

    def get_categories_for_date(self, date) -> list:
        """Get category stats for a specific date."""
        cursor = self.conn.execute(
            "SELECT * FROM daily_category_stats WHERE date = ? ORDER BY total_downloads DESC",
            (date,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_all_categories(self) -> list:
        """Get all distinct category names from projects."""
        cursor = self.conn.execute("SELECT DISTINCT categories FROM projects WHERE categories IS NOT NULL")
        categories = set()
        for row in cursor.fetchall():
            try:
                cats = json.loads(row["categories"])
                if isinstance(cats, list):
                    categories.update(cats)
            except (json.JSONDecodeError, TypeError):
                pass
        return sorted(categories)

    def get_project_download_history(self, project_id, days=30) -> list:
        """Get daily download snapshots for a project."""
        cursor = self.conn.execute("""
            SELECT date, downloads, follows
            FROM daily_project_snapshots
            WHERE project_id = ?
            ORDER BY date DESC
            LIMIT ?
        """, (project_id, days))
        return [dict(row) for row in cursor.fetchall()]

    def close(self):
        """Close the database connection."""
        self.conn.close()


if __name__ == "__main__":
    # Quick test
    db = Database(":memory:")
    print("Database initialized successfully.")
    db.close()