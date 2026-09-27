import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from croniter import croniter

import models
from backup_engine import perform_backup
from config import SCHEDULER_INTERVAL_MINUTES
from database import SessionLocal

logger = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None


def compute_next_run(cron_expression: str, base: datetime | None = None) -> datetime:
    base = base or datetime.now(timezone.utc)
    return croniter(cron_expression, base).get_next(datetime)


async def check_due_schedules() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        due = (
            db.query(models.BackupSchedule)
            .filter(
                models.BackupSchedule.is_active.is_(True),
                models.BackupSchedule.next_run_at <= now,
            )
            .all()
        )
        for schedule in due:
            try:
                backup = models.Backup(
                    connection_id=schedule.connection_id,
                    user_id=schedule.user_id,
                    schedule_id=schedule.schedule_id,
                    storage_backend_id=schedule.storage_backend_id,
                    status=models.BackupStatus.PENDING,
                )
                db.add(backup)
                schedule.last_run_at = now
                schedule.next_run_at = compute_next_run(schedule.cron_expression, now)
                db.commit()
                db.refresh(backup)

                await perform_backup(backup.backup_id)
                cleanup_expired_backups(schedule.connection_id, schedule.retention_days)
            except Exception:  # noqa: BLE001 - one bad schedule shouldn't stop the others
                logger.exception("Failed processing schedule %s", schedule.schedule_id)
                db.rollback()
    finally:
        db.close()


def cleanup_expired_backups(connection_id, retention_days: int) -> None:
    """Deletes Backup rows older than retention_days for a connection - removing the
    underlying file too, whether it's on local disk or in S3."""
    import s3_storage

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        expired = (
            db.query(models.Backup)
            .filter(
                models.Backup.connection_id == connection_id,
                models.Backup.created_at < cutoff,
            )
            .all()
        )
        for backup in expired:
            if backup.storage_type == models.StorageType.S3 and backup.s3_key and backup.storage_backend_id:
                storage_backend = db.query(models.StorageBackend).filter(
                    models.StorageBackend.storage_backend_id == backup.storage_backend_id
                ).first()
                if storage_backend:
                    try:
                        s3_storage.delete_file(storage_backend, backup.s3_key)
                    except Exception:  # noqa: BLE001 - don't let an S3 hiccup block cleanup of the DB row
                        logger.exception("Failed to delete S3 object for backup %s", backup.backup_id)
            elif backup.file_path:
                Path(backup.file_path).unlink(missing_ok=True)
            db.delete(backup)
        if expired:
            db.commit()
    finally:
        db.close()


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        check_due_schedules,
        "interval",
        minutes=SCHEDULER_INTERVAL_MINUTES,
        id="check_due_schedules",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Backup scheduler started (interval=%s min)", SCHEDULER_INTERVAL_MINUTES)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
