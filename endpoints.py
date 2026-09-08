import uuid
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

import models
import s3_storage
import schemas
from auth import get_current_user
from backup_engine import perform_backup, perform_restore, test_connection
from database import get_db
from encryption import encrypt_password
from scheduler import compute_next_run

router = APIRouter(prefix="/api", tags=["api"])


# ---------------------------------------------------------------------------
# ownership-scoped lookups
# ---------------------------------------------------------------------------

def get_user_connection(db: Session, user: models.User, db_connection_id: uuid.UUID) -> models.DatabaseConnection:
    connection = (
        db.query(models.DatabaseConnection)
        .filter(
            models.DatabaseConnection.db_connection_id == db_connection_id,
            models.DatabaseConnection.user_id == user.id,
        )
        .first()
    )
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Database connection not found")
    return connection


def get_user_storage_backend(db: Session, user: models.User, storage_backend_id: uuid.UUID) -> models.StorageBackend:
    backend = (
        db.query(models.StorageBackend)
        .filter(
            models.StorageBackend.storage_backend_id == storage_backend_id,
            models.StorageBackend.user_id == user.id,
        )
        .first()
    )
    if not backend:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage backend not found")
    return backend


def get_user_schedule(db: Session, user: models.User, schedule_id: uuid.UUID) -> models.BackupSchedule:
    schedule = (
        db.query(models.BackupSchedule)
        .join(models.DatabaseConnection, models.BackupSchedule.connection_id == models.DatabaseConnection.db_connection_id)
        .filter(models.BackupSchedule.schedule_id == schedule_id, models.DatabaseConnection.user_id == user.id)
        .first()
    )
    if not schedule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup schedule not found")
    return schedule


def get_user_backup(db: Session, user: models.User, backup_id: uuid.UUID) -> models.Backup:
    backup = (
        db.query(models.Backup)
        .join(models.DatabaseConnection, models.Backup.connection_id == models.DatabaseConnection.db_connection_id)
        .filter(models.Backup.backup_id == backup_id, models.DatabaseConnection.user_id == user.id)
        .first()
    )
    if not backup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
    return backup


# ---------------------------------------------------------------------------
# database connections
# ---------------------------------------------------------------------------

@router.post("/connections", response_model=schemas.DatabaseConnectionOut, status_code=status.HTTP_201_CREATED)
def create_database_connection(
    connection_in: schemas.DatabaseConnectionCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    new_connection = models.DatabaseConnection(
        user_id=current_user.id,
        name=connection_in.name,
        host=connection_in.host,
        port=connection_in.port,
        db_name=connection_in.db_name,
        db_username=connection_in.db_username,
        db_password_encrypted=encrypt_password(connection_in.db_password),
        ssl_mode=connection_in.ssl_mode,
        is_active=connection_in.is_active,
    )
    db.add(new_connection)
    db.commit()
    db.refresh(new_connection)
    return new_connection


@router.get("/connections", response_model=List[schemas.DatabaseConnectionOut])
def list_database_connections(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.DatabaseConnection)
        .filter(models.DatabaseConnection.user_id == current_user.id)
        .all()
    )


@router.get("/connections/{db_connection_id}", response_model=schemas.DatabaseConnectionOut)
def get_database_connection(
    db_connection_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_user_connection(db, current_user, db_connection_id)


@router.put("/connections/{db_connection_id}", response_model=schemas.DatabaseConnectionOut)
def update_database_connection(
    db_connection_id: uuid.UUID,
    connection_in: schemas.DatabaseConnectionUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    connection = get_user_connection(db, current_user, db_connection_id)

    for field, value in connection_in.model_dump(exclude_unset=True).items():
        if field == "db_password":
            setattr(connection, "db_password_encrypted", encrypt_password(value))
        else:
            setattr(connection, field, value)

    db.commit()
    db.refresh(connection)
    return connection


@router.delete("/connections/{db_connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_database_connection(
    db_connection_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    connection = get_user_connection(db, current_user, db_connection_id)
    db.delete(connection)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connections/{db_connection_id}/test", response_model=schemas.ConnectionTestResult)
async def test_database_connection(
    db_connection_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Runs `SELECT 1` against the target so users can validate credentials before relying on it."""
    connection = get_user_connection(db, current_user, db_connection_id)
    ok, detail = await test_connection(connection)
    return schemas.ConnectionTestResult(ok=ok, detail=detail)


# ---------------------------------------------------------------------------
# storage backends (where backup files get written: local disk or S3)
# ---------------------------------------------------------------------------

@router.post("/storage-backends", response_model=schemas.StorageBackendOut, status_code=status.HTTP_201_CREATED)
def create_storage_backend(
    backend_in: schemas.StorageBackendCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Register a place backups can be stored. type='local' needs nothing else.
    type='s3' needs s3_bucket, s3_region, s3_access_key, s3_secret_key
    (optionally s3_prefix and s3_endpoint_url for S3-compatible services).
    """
    backend = models.StorageBackend(
        user_id=current_user.id,
        name=backend_in.name,
        type=backend_in.type,
        s3_bucket=backend_in.s3_bucket,
        s3_region=backend_in.s3_region,
        s3_prefix=backend_in.s3_prefix,
        s3_endpoint_url=backend_in.s3_endpoint_url,
        s3_access_key_encrypted=encrypt_password(backend_in.s3_access_key) if backend_in.s3_access_key else None,
        s3_secret_key_encrypted=encrypt_password(backend_in.s3_secret_key) if backend_in.s3_secret_key else None,
        is_default=backend_in.is_default,
    )
    db.add(backend)
    db.commit()
    db.refresh(backend)
    return backend


@router.get("/storage-backends", response_model=List[schemas.StorageBackendOut])
def list_storage_backends(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.StorageBackend)
        .filter(models.StorageBackend.user_id == current_user.id)
        .all()
    )


@router.get("/storage-backends/{storage_backend_id}", response_model=schemas.StorageBackendOut)
def get_storage_backend(
    storage_backend_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_user_storage_backend(db, current_user, storage_backend_id)


@router.delete("/storage-backends/{storage_backend_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_storage_backend(
    storage_backend_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    backend = get_user_storage_backend(db, current_user, storage_backend_id)
    db.delete(backend)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/storage-backends/{storage_backend_id}/test", response_model=schemas.ConnectionTestResult)
def test_storage_backend(
    storage_backend_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """For S3 backends, verifies the credentials can actually reach the bucket."""
    backend = get_user_storage_backend(db, current_user, storage_backend_id)
    if backend.type != models.StorageType.S3:
        return schemas.ConnectionTestResult(ok=True, detail="Local storage - nothing to test")
    ok, detail = s3_storage.check_bucket_access(backend)
    return schemas.ConnectionTestResult(ok=ok, detail=detail)


# ---------------------------------------------------------------------------
# backup schedules
# ---------------------------------------------------------------------------

@router.post("/connections/{db_connection_id}/schedules", response_model=schemas.BackupScheduleOut, status_code=status.HTTP_201_CREATED)
def create_backup_schedule(
    db_connection_id: uuid.UUID,
    schedule_in: schemas.BackupScheduleCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    connection = get_user_connection(db, current_user, db_connection_id)
    if schedule_in.storage_backend_id:
        get_user_storage_backend(db, current_user, schedule_in.storage_backend_id)  # 404s if not owned

    schedule = models.BackupSchedule(
        connection_id=connection.db_connection_id,
        user_id=current_user.id,
        cron_expression=schedule_in.cron_expression,
        retention_days=schedule_in.retention_days,
        is_active=schedule_in.is_active,
        storage_backend_id=schedule_in.storage_backend_id,
        next_run_at=compute_next_run(schedule_in.cron_expression),
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.get("/connections/{db_connection_id}/schedules", response_model=List[schemas.BackupScheduleOut])
def list_backup_schedules(
    db_connection_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    connection = get_user_connection(db, current_user, db_connection_id)
    return (
        db.query(models.BackupSchedule)
        .filter(models.BackupSchedule.connection_id == connection.db_connection_id)
        .all()
    )


@router.get("/schedules/{schedule_id}", response_model=schemas.BackupScheduleOut)
def get_backup_schedule(
    schedule_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_user_schedule(db, current_user, schedule_id)


@router.put("/schedules/{schedule_id}", response_model=schemas.BackupScheduleOut)
def update_backup_schedule(
    schedule_id: uuid.UUID,
    schedule_in: schemas.BackupScheduleUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    schedule = get_user_schedule(db, current_user, schedule_id)
    updates = schedule_in.model_dump(exclude_unset=True)
    if updates.get("storage_backend_id"):
        get_user_storage_backend(db, current_user, updates["storage_backend_id"])  # 404s if not owned
    for field, value in updates.items():
        setattr(schedule, field, value)

    # If the cron changed, next_run_at must be recomputed or it'll keep firing on the old schedule.
    if "cron_expression" in updates:
        schedule.next_run_at = compute_next_run(schedule.cron_expression)

    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_backup_schedule(
    schedule_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    schedule = get_user_schedule(db, current_user, schedule_id)
    db.delete(schedule)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# backups (instant + scheduled results)
# ---------------------------------------------------------------------------

@router.post("/connections/{db_connection_id}/backups", response_model=schemas.BackupOut, status_code=status.HTTP_201_CREATED)
def create_backup(
    db_connection_id: uuid.UUID,
    backup_in: schemas.BackupCreate,
    background_tasks: BackgroundTasks,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Triggers an instant, FULL backup of the whole database (pg_dump, custom format).
    Pass storage_backend_id to send it to a registered S3 bucket instead of local disk;
    omit it to store locally. Runs in the background - poll GET /backups/{id} for status.
    """
    connection = get_user_connection(db, current_user, db_connection_id)
    if backup_in.storage_backend_id:
        get_user_storage_backend(db, current_user, backup_in.storage_backend_id)  # 404s if not owned

    backup = models.Backup(
        connection_id=connection.db_connection_id,
        user_id=current_user.id,
        schedule_id=None,
        storage_backend_id=backup_in.storage_backend_id,
        status=models.BackupStatus.PENDING,
    )
    db.add(backup)
    db.commit()
    db.refresh(backup)

    background_tasks.add_task(perform_backup, backup.backup_id)
    return backup


@router.get("/connections/{db_connection_id}/backups", response_model=List[schemas.BackupOut])
def list_connection_backups(
    db_connection_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    connection = get_user_connection(db, current_user, db_connection_id)
    return (
        db.query(models.Backup)
        .filter(models.Backup.connection_id == connection.db_connection_id)
        .order_by(models.Backup.created_at.desc())
        .all()
    )


@router.get("/backups", response_model=List[schemas.BackupOut])
def list_backups(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Backup)
        .join(models.DatabaseConnection, models.Backup.connection_id == models.DatabaseConnection.db_connection_id)
        .filter(models.DatabaseConnection.user_id == current_user.id)
        .order_by(models.Backup.created_at.desc())
        .all()
    )


@router.get("/backups/{backup_id}", response_model=schemas.BackupOut)
def get_backup(
    backup_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_user_backup(db, current_user, backup_id)


@router.get("/backups/{backup_id}/download")
def download_backup(
    backup_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    backup = get_user_backup(db, current_user, backup_id)
    if backup.status != models.BackupStatus.SUCCESS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Backup is not complete")

    if backup.storage_type == models.StorageType.S3:
        storage_backend = db.query(models.StorageBackend).filter(
            models.StorageBackend.storage_backend_id == backup.storage_backend_id
        ).first()
        if not storage_backend:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Storage backend no longer exists")
        url = s3_storage.generate_presigned_url(storage_backend, backup.s3_key)
        return RedirectResponse(url)

    if not backup.file_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Backup file is missing")
    return FileResponse(
        path=backup.file_path,
        filename=f"{backup.connection.name}-{backup.backup_id}.dump",
        media_type="application/octet-stream",
    )


@router.delete("/backups/{backup_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_backup(
    backup_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import pathlib

    backup = get_user_backup(db, current_user, backup_id)
    if backup.storage_type == models.StorageType.S3 and backup.s3_key:
        storage_backend = db.query(models.StorageBackend).filter(
            models.StorageBackend.storage_backend_id == backup.storage_backend_id
        ).first()
        if storage_backend:
            try:
                s3_storage.delete_file(storage_backend, backup.s3_key)
            except Exception:  # noqa: BLE001 - don't block deleting the record if S3 cleanup fails
                pass
    elif backup.file_path:
        pathlib.Path(backup.file_path).unlink(missing_ok=True)

    db.delete(backup)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# restore jobs
# ---------------------------------------------------------------------------

@router.post("/backups/{backup_id}/restore-jobs", response_model=schemas.RestoreJobOut, status_code=status.HTTP_201_CREATED)
def create_restore_job(
    backup_id: uuid.UUID,
    restore_in: schemas.RestoreJobCreate,
    background_tasks: BackgroundTasks,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Triggers a pg_restore from this backup (pulled from S3 first if that's where it's stored).
    Choose the destination one of three ways:
      - omit both target_connection_id and target -> restores into the backup's original connection
      - target_connection_id -> restores into another connection you've already registered
      - target -> restores into a database you haven't registered, using credentials given right here
    """
    backup = get_user_backup(db, current_user, backup_id)

    restore_job = models.RestoreJob(
        backup_id=backup.backup_id,
        user_id=current_user.id,
        status=models.RestoreStatus.PENDING,
    )

    if restore_in.target is not None:
        t = restore_in.target
        restore_job.target_connection_id = None
        restore_job.target_host = t.host
        restore_job.target_port = t.port
        restore_job.target_db_name = t.db_name
        restore_job.target_db_username = t.db_username
        restore_job.target_db_password_encrypted = encrypt_password(t.db_password)
        restore_job.target_ssl_mode = t.ssl_mode
    else:
        target_connection_id = restore_in.target_connection_id or backup.connection_id
        # Validate the target belongs to this user too (whether it's the original or a different one).
        get_user_connection(db, current_user, target_connection_id)
        restore_job.target_connection_id = target_connection_id

    db.add(restore_job)
    db.commit()
    db.refresh(restore_job)

    background_tasks.add_task(perform_restore, restore_job.restore_job_id)
    return restore_job


@router.get("/restore-jobs", response_model=List[schemas.RestoreJobOut])
def list_restore_jobs(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.RestoreJob)
        .join(models.Backup, models.RestoreJob.backup_id == models.Backup.backup_id)
        .join(models.DatabaseConnection, models.Backup.connection_id == models.DatabaseConnection.db_connection_id)
        .filter(models.DatabaseConnection.user_id == current_user.id)
        .order_by(models.RestoreJob.created_at.desc())
        .all()
    )


@router.get("/restore-jobs/{restore_job_id}", response_model=schemas.RestoreJobOut)
def get_restore_job(
    restore_job_id: uuid.UUID,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    restore_job = (
        db.query(models.RestoreJob)
        .join(models.Backup, models.RestoreJob.backup_id == models.Backup.backup_id)
        .join(models.DatabaseConnection, models.Backup.connection_id == models.DatabaseConnection.db_connection_id)
        .filter(models.RestoreJob.restore_job_id == restore_job_id, models.DatabaseConnection.user_id == current_user.id)
        .first()
    )
    if not restore_job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restore job not found")
    return restore_job
