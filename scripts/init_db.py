"""
scripts/init_db.py
Run database migrations against Supabase (or local Postgres).

Usage:
    python scripts/init_db.py

Reads DATABASE_URL_SYNC from .env
"""

import os
import sys
import logging
from pathlib import Path

# Add project root to path so we can import shared
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = project_root / "migrations"
DATABASE_URL = os.getenv("DATABASE_URL_SYNC", "")


def get_migration_files() -> list[Path]:
    """Return migration SQL files in sorted order."""
    if not MIGRATIONS_DIR.exists():
        logger.error("migrations/ directory not found at %s", MIGRATIONS_DIR)
        sys.exit(1)
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def ensure_migrations_table(cursor) -> None:
    """Create a migrations tracking table if it doesn't exist."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id          SERIAL PRIMARY KEY,
            filename    VARCHAR(255) NOT NULL UNIQUE,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


def get_applied_migrations(cursor) -> set[str]:
    """Return set of already-applied migration filenames."""
    cursor.execute("SELECT filename FROM _migrations ORDER BY filename;")
    return {row[0] for row in cursor.fetchall()}


def apply_migration(cursor, migration_file: Path) -> None:
    """Execute a single migration file."""
    sql = migration_file.read_text(encoding="utf-8")
    logger.info("Applying migration: %s", migration_file.name)
    cursor.execute(sql)
    cursor.execute(
        "INSERT INTO _migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING;",
        (migration_file.name,)
    )


def main() -> None:
    if not DATABASE_URL:
        logger.error(
            "DATABASE_URL_SYNC not set. "
            "Copy .env.example to .env and fill in your database credentials."
        )
        sys.exit(1)

    logger.info("Connecting to database...")
    logger.info("URL: %s", DATABASE_URL.split("@")[-1])  # Log host only (not credentials)

    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
    except Exception as e:
        logger.error("Failed to connect to database: %s", e)
        sys.exit(1)

    try:
        with conn.cursor() as cursor:
            ensure_migrations_table(cursor)
            conn.commit()

            applied = get_applied_migrations(cursor)
            migration_files = get_migration_files()

            if not migration_files:
                logger.warning("No migration files found in %s", MIGRATIONS_DIR)
                return

            pending = [f for f in migration_files if f.name not in applied]

            if not pending:
                logger.info("All %d migrations already applied. Nothing to do.", len(migration_files))
                return

            logger.info(
                "Found %d migrations (%d pending): %s",
                len(migration_files),
                len(pending),
                [f.name for f in pending]
            )

            for migration_file in pending:
                apply_migration(cursor, migration_file)
                conn.commit()
                logger.info("✓ Applied: %s", migration_file.name)

            logger.info("All migrations applied successfully! ✓")

    except Exception as e:
        conn.rollback()
        logger.error("Migration failed: %s", e)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
