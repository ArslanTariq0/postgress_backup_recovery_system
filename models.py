import enum
import uuid
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    database_connections = relationship(
        "DatabaseConnection", back_populates="user", cascade="all, delete-orphan"
    )


class BackupStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class RestoreStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class StorageType(str, enum.Enum):
    LOCAL = "local"
    S3 = "s3"


class StorageBackend(Base):
    """Where backup files get written: local disk, or an S3 bucket."""
    __tablename__ = "storage_backends"

    storage_backend_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    name = Column(String, nullable=False)
    type = Column(Enum(StorageType), nullable=False, default=StorageType.LOCAL)

    # Only used when type == S3
    s3_bucket = Column(String, nullable=True)
    s3_region = Column(String, nullable=True)
    s3_prefix = Column(String, nullable=True)  # optional folder prefix inside the bucket
    s3_access_key_encrypted = Column(Text, nullable=True)
    s3_secret_key_encrypted = Column(Text, nullable=True)
    s3_endpoint_url = Column(String, nullable=True)  # optional, for S3-compatible services

    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DatabaseConnection(Base):
    """A Postgres target a user has registered for backups."""
    __tablename__ = "database_connections"

    db_connection_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    name = Column(String, nullable=False)
    host = Column(String, nullable=False)
    port = Column(Integer, default=5432, nullable=False)
    db_name = Column(String, nullable=False)
    db_username = Column(String, nullable=False)
    db_password_encrypted = Column(Text, nullable=False)
    ssl_mode = Column(String, default="prefer")

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="database_connections")
    schedules = relationship("BackupSchedule", back_populates="connection", cascade="all, delete-orphan")
    backups = relationship(
        "Backup", back_populates="connection", cascade="all, delete-orphan",
        foreign_keys="Backup.connection_id",
    )


class BackupSchedule(Base):
    """Recurring backup config for one connection (e.g. 'daily at 2am')."""
    __tablename__ = "backup_schedules"

    schedule_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id = Column(UUID(as_uuid=True), ForeignKey("database_connections.db_connection_id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    storage_backend_id = Column(UUID(as_uuid=True), ForeignKey("storage_backends.storage_backend_id"), nullable=True)

    cron_expression = Column(String, nullable=False)
    retention_days = Column(Integer, default=30)
    is_active = Column(Boolean, default=True)

    next_run_at = Column(DateTime(timezone=True), index=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    connection = relationship("DatabaseConnection", back_populates="schedules")


class Backup(Base):
    """A single backup run - either scheduled or manually triggered."""
    __tablename__ = "backups"

    backup_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id = Column(UUID(as_uuid=True), ForeignKey("database_connections.db_connection_id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    schedule_id = Column(UUID(as_uuid=True), ForeignKey("backup_schedules.schedule_id"), nullable=True)
    storage_backend_id = Column(UUID(as_uuid=True), ForeignKey("storage_backends.storage_backend_id"), nullable=True)

    status = Column(Enum(BackupStatus), default=BackupStatus.PENDING, nullable=False)

    # Always a full pg_dump (-F c, custom format) of the whole database - no table filtering.
    storage_type = Column(Enum(StorageType), default=StorageType.LOCAL, nullable=False)
    file_path = Column(String, nullable=True)     # populated when storage_type == LOCAL
    s3_bucket = Column(String, nullable=True)      # populated when storage_type == S3
    s3_key = Column(String, nullable=True)         # populated when storage_type == S3

    size_bytes = Column(BigInteger, nullable=True)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    connection = relationship("DatabaseConnection", back_populates="backups", foreign_keys=[connection_id])
    storage_backend = relationship("StorageBackend", foreign_keys=[storage_backend_id])
    restore_jobs = relationship(
        "RestoreJob", back_populates="backup", cascade="all, delete-orphan",
        foreign_keys="RestoreJob.backup_id",
    )


class RestoreJob(Base):
    """
    A restore attempt using a specific backup file.

    The restore target is EITHER:
      - target_connection_id: a previously-registered DatabaseConnection, OR
      - the ad-hoc target_* columns: one-off credentials supplied just for this restore,
        for restoring into a database that was never registered as a connection.
    """
    __tablename__ = "restore_jobs"

    restore_job_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    backup_id = Column(UUID(as_uuid=True), ForeignKey("backups.backup_id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    target_connection_id = Column(UUID(as_uuid=True), ForeignKey("database_connections.db_connection_id"), nullable=True, index=True)

    # Ad-hoc target (used when target_connection_id is null)
    target_host = Column(String, nullable=True)
    target_port = Column(Integer, nullable=True)
    target_db_name = Column(String, nullable=True)
    target_db_username = Column(String, nullable=True)
    target_db_password_encrypted = Column(Text, nullable=True)
    target_ssl_mode = Column(String, nullable=True)

    status = Column(Enum(RestoreStatus), default=RestoreStatus.PENDING, nullable=False)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    backup = relationship("Backup", back_populates="restore_jobs", foreign_keys=[backup_id])
    target_connection = relationship("DatabaseConnection", foreign_keys=[target_connection_id])
