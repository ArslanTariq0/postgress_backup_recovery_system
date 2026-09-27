"""
Runs the actual pg_dump / pg_restore subprocesses and keeps the DB rows
(Backup / RestoreJob) in sync with what happened.

Every function here opens its OWN short-lived DB session rather than reusing
a request's session, because these are meant to be invoked from
BackgroundTasks (after the HTTP response is sent) or from the scheduler -
both of which outlive the original request.
"""
import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

import models
import s3_storage
from config import (
    PG_DUMP_BIN, PG_RESTORE_BIN, BACKUP_TIMEOUT_SECONDS, RESTORE_TIMEOUT_SECONDS,
    backup_file_path,
)
from database import SessionLocal
from encryption import decrypt_password

logger = logging.getLogger("backup_engine")


class CommandError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


async def _run_subprocess(args: list[str], env: dict, timeout: int) -> None:
    """Run a subprocess, raise CommandError with captured stderr on failure/timeout."""
    process = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise CommandError(f"Command timed out after {timeout}s: {' '.join(args[:1])}")

    if process.returncode != 0:
        raise CommandError(
            f"{args[0]} exited with code {process.returncode}",
            stderr=stderr.decode(errors="replace"),
        )


def _pg_env(password: str) -> dict:
    import os
    env = os.environ.copy()
    env["PGPASSWORD"] = password  # avoids putting the password on the command line
    return env


async def perform_backup(backup_id: UUID) -> None:
    """
    Runs a FULL pg_dump (whole database, custom format) for a Backup row.
    Always dumps to a local staging file first (pg_dump needs a filesystem path),
    then - if the backup's storage backend is S3 - uploads it and removes the
    local copy, leaving only s3_bucket/s3_key populated.
    """
    db = SessionLocal()
    try:
        backup = db.query(models.Backup).filter(models.Backup.backup_id == backup_id).first()
        if not backup:
            logger.error("perform_backup: backup %s not found", backup_id)
            return

        connection = backup.connection or db.query(models.DatabaseConnection).filter(
            models.DatabaseConnection.db_connection_id == backup.connection_id
        ).first()
        if not connection:
            backup.status = models.BackupStatus.FAILED
            backup.error_message = "Database connection no longer exists"
            backup.completed_at = datetime.now(timezone.utc)
            db.commit()
            return

        storage_backend: Optional[models.StorageBackend] = None
        if backup.storage_backend_id:
            storage_backend = db.query(models.StorageBackend).filter(
                models.StorageBackend.storage_backend_id == backup.storage_backend_id
            ).first()
            if not storage_backend:
                backup.status = models.BackupStatus.FAILED
                backup.error_message = "Selected storage backend no longer exists"
                backup.completed_at = datetime.now(timezone.utc)
                db.commit()
                return

        use_s3 = storage_backend is not None and storage_backend.type == models.StorageType.S3

        backup.status = models.BackupStatus.RUNNING
        backup.started_at = datetime.now(timezone.utc)
        db.commit()

        # Always dump locally first - a staging path when going to S3, the permanent
        # location when storing locally.
        staging_path: Path = backup_file_path(backup.user_id, backup.connection_id, backup.backup_id)

        try:
            password = decrypt_password(connection.db_password_encrypted)
            args = [
                PG_DUMP_BIN,
                "-h", connection.host,
                "-p", str(connection.port),
                "-U", connection.db_username,
                "-d", connection.db_name,
                "-F", "c",              # custom format - required for pg_restore
                "--no-password",
                "-f", str(staging_path),
                # No -t/--table or -n/--schema filters: this is always a full database dump.
            ]
            # pg_dump has no --sslmode flag; libpq tools read it from PGSSLMODE instead.
            env = _pg_env(password)
            env["PGSSLMODE"] = connection.ssl_mode or "prefer"

            await _run_subprocess(args, env, BACKUP_TIMEOUT_SECONDS)

            size_bytes = staging_path.stat().st_size if staging_path.exists() else None

            if use_s3:
                key = s3_storage.build_key(storage_backend, backup.user_id, backup.connection_id, backup.backup_id)
                s3_storage.upload_file(storage_backend, staging_path, key)
                staging_path.unlink(missing_ok=True)  # don't keep a local copy once it's in S3

                backup.storage_type = models.StorageType.S3
                backup.s3_bucket = storage_backend.s3_bucket
                backup.s3_key = key
                backup.file_path = None
            else:
                backup.storage_type = models.StorageType.LOCAL
                backup.file_path = str(staging_path)
                backup.s3_bucket = None
                backup.s3_key = None

            backup.status = models.BackupStatus.SUCCESS
            backup.size_bytes = size_bytes
            backup.error_message = None
        except CommandError as exc:
            backup.status = models.BackupStatus.FAILED
            backup.error_message = (str(exc) + (f": {exc.stderr}" if exc.stderr else ""))[:4000]
            if staging_path.exists():
                staging_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - want any unexpected failure recorded, not raised into a background task
            backup.status = models.BackupStatus.FAILED
            backup.error_message = f"Unexpected error: {exc}"[:4000]
            if staging_path.exists():
                staging_path.unlink(missing_ok=True)
        finally:
            backup.completed_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


async def perform_restore(restore_job_id: UUID) -> None:
    """
    Runs pg_restore for a RestoreJob row. The restore target is either a saved
    DatabaseConnection or ad-hoc credentials supplied just for this job. If the
    source backup lives in S3, it's downloaded to a temp file first.
    """
    db = SessionLocal()
    try:
        restore_job = db.query(models.RestoreJob).filter(
            models.RestoreJob.restore_job_id == restore_job_id
        ).first()
        if not restore_job:
            logger.error("perform_restore: restore job %s not found", restore_job_id)
            return

        backup = db.query(models.Backup).filter(models.Backup.backup_id == restore_job.backup_id).first()

        if not backup or backup.status != models.BackupStatus.SUCCESS:
            restore_job.status = models.RestoreStatus.FAILED
            restore_job.error_message = "Source backup is missing or was not completed successfully"
            restore_job.completed_at = datetime.now(timezone.utc)
            db.commit()
            return

        # Resolve target: saved connection, or ad-hoc credentials on the restore job itself.
        target_host = target_port = target_db_name = target_db_username = target_ssl_mode = None
        target_password = None

        if restore_job.target_connection_id:
            target = db.query(models.DatabaseConnection).filter(
                models.DatabaseConnection.db_connection_id == restore_job.target_connection_id
            ).first()
            if not target:
                restore_job.status = models.RestoreStatus.FAILED
                restore_job.error_message = "Target database connection no longer exists"
                restore_job.completed_at = datetime.now(timezone.utc)
                db.commit()
                return
            target_host, target_port = target.host, target.port
            target_db_name, target_db_username = target.db_name, target.db_username
            target_ssl_mode = target.ssl_mode
            target_password = decrypt_password(target.db_password_encrypted)
        else:
            target_host, target_port = restore_job.target_host, restore_job.target_port
            target_db_name, target_db_username = restore_job.target_db_name, restore_job.target_db_username
            target_ssl_mode = restore_job.target_ssl_mode
            target_password = decrypt_password(restore_job.target_db_password_encrypted)

        restore_job.status = models.RestoreStatus.RUNNING
        restore_job.started_at = datetime.now(timezone.utc)
        db.commit()

        # Resolve source file: local path directly, or pull down from S3 into a temp file.
        source_path: Optional[Path] = None
        temp_download_path: Optional[Path] = None
        try:
            if backup.storage_type == models.StorageType.S3:
                storage_backend = db.query(models.StorageBackend).filter(
                    models.StorageBackend.storage_backend_id == backup.storage_backend_id
                ).first()
                if not storage_backend:
                    raise CommandError("The S3 storage backend used for this backup no longer exists")
                temp_download_path = Path(tempfile.gettempdir()) / f"restore-{backup.backup_id}.dump"
                s3_storage.download_file(storage_backend, backup.s3_key, temp_download_path)
                source_path = temp_download_path
            else:
                if not backup.file_path or not Path(backup.file_path).exists():
                    raise CommandError(f"Backup file not found on disk: {backup.file_path}")
                source_path = Path(backup.file_path)

            args = [
                PG_RESTORE_BIN,
                "-h", target_host,
                "-p", str(target_port),
                "-U", target_db_username,
                "-d", target_db_name,
                "--no-password",
                "--clean", "--if-exists",  # drop existing objects before recreating, so a restore is idempotent
                str(source_path),
            ]
            env = _pg_env(target_password)
            env["PGSSLMODE"] = target_ssl_mode or "prefer"

            await _run_subprocess(args, env, RESTORE_TIMEOUT_SECONDS)

            restore_job.status = models.RestoreStatus.SUCCESS
            restore_job.error_message = None
        except CommandError as exc:
            restore_job.status = models.RestoreStatus.FAILED
            restore_job.error_message = (str(exc) + (f": {exc.stderr}" if exc.stderr else ""))[:4000]
        except Exception as exc:  # noqa: BLE001
            restore_job.status = models.RestoreStatus.FAILED
            restore_job.error_message = f"Unexpected error: {exc}"[:4000]
        finally:
            if temp_download_path and temp_download_path.exists():
                temp_download_path.unlink(missing_ok=True)
            restore_job.completed_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


async def test_connection(connection: models.DatabaseConnection) -> tuple[bool, str]:
    """Quick connectivity check using psql, without touching any real data."""
    from config import PSQL_BIN

    try:
        password = decrypt_password(connection.db_password_encrypted)
    except Exception:  # noqa: BLE001 - e.g. ENCRYPTION_KEY rotated since the password was stored
        return False, "Stored credentials could not be decrypted. Re-save this connection's password."

    args = [
        PSQL_BIN,
        "-h", connection.host,
        "-p", str(connection.port),
        "-U", connection.db_username,
        "-d", connection.db_name,
        "--no-password",
        "-c", "SELECT 1;",
    ]
    env = _pg_env(password)
    env["PGSSLMODE"] = connection.ssl_mode or "prefer"
    try:
        await _run_subprocess(args, env, timeout=15)
        return True, "Connection successful"
    except CommandError as exc:
        return False, (str(exc) + (f": {exc.stderr}" if exc.stderr else ""))[:2000]
    except FileNotFoundError:
        return False, f"'{PSQL_BIN}' was not found on PATH. Install the PostgreSQL client tools."
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return False, f"Unexpected error: {type(exc).__name__}: {exc}"[:2000]
