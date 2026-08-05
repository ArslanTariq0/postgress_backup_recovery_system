import enum
import uuid
from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, DateTime,
    ForeignKey, Enum, Text
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

class CloudBackups(str,enum.Enum):
    AWSS3="AwsS3"
    AzureBlobStorage="AzureBlobStorage"
    GoogleCloudStorage="GoogleCloudStorage"


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


class DatabaseConnection(Base):
    """A Postgres target a user has registered for backups."""
    __tablename__ = "database_connections"

    db_connection_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

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
    backups = relationship("Backup", back_populates="connection", cascade="all, delete-orphan")


class BackupSchedule(Base):
    """Recurring backup config for one connection (e.g. 'daily at 2am')."""
    __tablename__ = "backup_schedules"

    schedule_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id = Column(UUID(as_uuid=True), ForeignKey("database_connections.db_connection_id"), nullable=False, index=True)
    id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

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
    id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    schedule_id = Column(UUID(as_uuid=True), ForeignKey("backup_schedules.schedule_id"), nullable=True)

    status = Column(Enum(BackupStatus), default=BackupStatus.PENDING, nullable=False)
    file_path = Column(String, nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    connection = relationship("DatabaseConnection", back_populates="backups")
    restore_jobs = relationship("RestoreJob", back_populates="backup", cascade="all, delete-orphan")


class RestoreJob(Base):
    """A restore attempt using a specific backup file."""
    __tablename__ = "restore_jobs"

    restore_job_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    backup_id = Column(UUID(as_uuid=True), ForeignKey("backups.backup_id"), nullable=False, index=True)
    id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    status = Column(Enum(RestoreStatus), default=RestoreStatus.PENDING, nullable=False)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    backup = relationship("Backup", back_populates="restore_jobs")