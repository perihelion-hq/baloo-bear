"""Standalone database migration entrypoint.

Runs the same advisory-locked, idempotent migration path as application
startup (``init_db`` -> Alembic ``upgrade head``), reading ``DATABASE_URL``
from settings so it works with the Cloud Run Unix-socket connection. This
deliberately does NOT use the raw ``alembic`` CLI, which would read
``alembic.ini``'s hardcoded localhost URL instead of ``DATABASE_URL``.

Usage:
    python scripts/migrate.py
"""

from __future__ import annotations

import asyncio
import sys

from baloo.config.settings import get_settings
from baloo.db.engine import init_db


def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set; cannot run migrations.", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(init_db(settings.database_url))


if __name__ == "__main__":
    main()
