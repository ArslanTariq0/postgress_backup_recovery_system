import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Directory where .dump files produced by pg_dump are stored.
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "./backup_storage")).resolve()
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# Allow overriding the binaries if they're not on PATH (e.g. "/usr/lib/postgresql/16/bin/pg_dump")
PG_DUMP_BIN = os.getenv("PG_DUMP_BIN", "pg_dump")
PG_RESTORE_BIN = os.getenv("PG_RESTORE_BIN", "pg_restore")
PSQL_BIN = os.getenv("PSQL_BIN", "psql")

# Safety timeout (seconds) so a hung connection can't wedge a worker forever.
BACKUP_TIMEOUT_SECONDS = int(os.getenv("BACKUP_TIMEOUT_SECONDS", 3600))
RESTORE_TIMEOUT_SECONDS = int(os.getenv("RESTORE_TIMEOUT_SECONDS", 3600))

# How often (minutes) the scheduler wakes up to check for due schedules / retention cleanup.
SCHEDULER_INTERVAL_MINUTES = int(os.getenv("SCHEDULER_INTERVAL_MINUTES", 1))


def backup_file_path(user_id: int, connection_id, backup_id) -> Path:
    """Where a given backup's .dump file lives on disk."""
    user_dir = BACKUP_DIR / str(user_id) / str(connection_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{backup_id}.dump"
