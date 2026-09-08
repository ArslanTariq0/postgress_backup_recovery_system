from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from croniter import croniter
from pydantic import BaseModel, EmailStr, field_validator, model_validator


class UserCreate(BaseModel):
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: EmailStr
    created_at: datetime

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class BackupStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class RestoreStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class StorageType(str, Enum):
    LOCAL = "local"
    S3 = "s3"


class StorageBackendBase(BaseModel):
    name: str
    type: StorageType = StorageType.LOCAL
    s3_bucket: Optional[str] = None
    s3_region: Optional[str] = None
    s3_prefix: Optional[str] = None
    s3_endpoint_url: Optional[str] = None  # for S3-compatible services (MinIO, R2, etc.)
    is_default: bool = False

    @model_validator(mode="after")
    def validate_s3_fields(self):
        # field_validator would skip this check when s3_bucket is simply omitted (defaults
        # aren't re-validated), so this cross-field rule lives in a model_validator instead.
        if self.type == StorageType.S3 and not self.s3_bucket:
            raise ValueError("s3_bucket is required when type is 's3'")
        return self


class StorageBackendCreate(StorageBackendBase):
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None

    @model_validator(mode="after")
    def validate_s3_credentials(self):
        if self.type == StorageType.S3 and (not self.s3_access_key or not self.s3_secret_key):
            raise ValueError("s3_access_key and s3_secret_key are required when type is 's3'")
        return self


class StorageBackendOut(StorageBackendBase):
    storage_backend_id: UUID
    user_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class DatabaseConnectionBase(BaseModel):
    name: str
    host: str
    port: int = 5432
    db_name: str
    db_username: str
    ssl_mode: str = "prefer"
    is_active: bool = True


class DatabaseConnectionCreate(DatabaseConnectionBase):
    db_password: str


class DatabaseConnectionUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    db_name: Optional[str] = None
    db_username: Optional[str] = None
    db_password: Optional[str] = None
    ssl_mode: Optional[str] = None
    is_active: Optional[bool] = None


class DatabaseConnectionOut(DatabaseConnectionBase):
    db_connection_id: UUID
    user_id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ConnectionTestResult(BaseModel):
    ok: bool
    detail: str


class BackupScheduleBase(BaseModel):
    cron_expression: str
    retention_days: int = 30
    is_active: bool = True
    storage_backend_id: Optional[UUID] = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError(
                "cron_expression must be a valid 5-field cron string, e.g. '0 2 * * *' for daily at 2am"
            )
        return value

    @field_validator("retention_days")
    @classmethod
    def validate_retention(cls, value: int) -> int:
        if value < 1:
            raise ValueError("retention_days must be at least 1")
        return value


class BackupScheduleCreate(BackupScheduleBase):
    """next_run_at is computed server-side from cron_expression - clients don't set it."""
    pass


class BackupScheduleUpdate(BaseModel):
    cron_expression: Optional[str] = None
    retention_days: Optional[int] = None
    is_active: Optional[bool] = None
    storage_backend_id: Optional[UUID] = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not croniter.is_valid(value):
            raise ValueError("cron_expression must be a valid 5-field cron string")
        return value


class BackupScheduleOut(BackupScheduleBase):
    schedule_id: UUID
    connection_id: UUID
    user_id: int
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class BackupCreate(BaseModel):
    """Optionally choose where this full database dump gets stored - defaults to local disk."""
    storage_backend_id: Optional[UUID] = None


class BackupOut(BaseModel):
    backup_id: UUID
    connection_id: UUID
    user_id: int
    schedule_id: Optional[UUID] = None
    storage_backend_id: Optional[UUID] = None
    storage_type: StorageType
    status: BackupStatus
    file_path: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_key: Optional[str] = None
    size_bytes: Optional[int] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class RestoreTargetAdHoc(BaseModel):
    """One-off destination credentials, for restoring into a database that isn't a saved connection."""
    host: str
    port: int = 5432
    db_name: str
    db_username: str
    db_password: str
    ssl_mode: str = "prefer"


class RestoreJobCreate(BaseModel):
    """
    Choose exactly ONE way to specify the restore target:
      - target_connection_id: restore into a previously registered connection, OR
      - target: one-off credentials for a database you haven't registered.
    If neither is given, restores back into the backup's original connection.
    """
    target_connection_id: Optional[UUID] = None
    target: Optional[RestoreTargetAdHoc] = None

    @model_validator(mode="after")
    def validate_single_target(self):
        if self.target is not None and self.target_connection_id is not None:
            raise ValueError("Provide either target_connection_id or target, not both")
        return self


class RestoreJobOut(BaseModel):
    restore_job_id: UUID
    backup_id: UUID
    user_id: int
    target_connection_id: Optional[UUID] = None
    target_host: Optional[str] = None
    target_db_name: Optional[str] = None
    status: RestoreStatus
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True
