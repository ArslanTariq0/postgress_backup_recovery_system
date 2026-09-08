"""
Wraps boto3 so backup_engine.py doesn't need to know S3 details directly.
Each call builds its own client from a StorageBackend's (decrypted) credentials -
there's no global AWS session, since different users/backends can use different
buckets, regions, or even different S3-compatible providers.
"""
from pathlib import Path

import models
from encryption import decrypt_password


def _client(storage_backend: models.StorageBackend):
    import boto3

    access_key = decrypt_password(storage_backend.s3_access_key_encrypted)
    secret_key = decrypt_password(storage_backend.s3_secret_key_encrypted)

    kwargs = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": storage_backend.s3_region,
    }
    if storage_backend.s3_endpoint_url:
        kwargs["endpoint_url"] = storage_backend.s3_endpoint_url

    return boto3.client("s3", **kwargs)


def build_key(storage_backend: models.StorageBackend, user_id: int, connection_id, backup_id) -> str:
    prefix = (storage_backend.s3_prefix or "").strip("/")
    parts = [p for p in [prefix, str(user_id), str(connection_id), f"{backup_id}.dump"] if p]
    return "/".join(parts)


def upload_file(storage_backend: models.StorageBackend, local_path: Path, key: str) -> None:
    client = _client(storage_backend)
    client.upload_file(str(local_path), storage_backend.s3_bucket, key)


def download_file(storage_backend: models.StorageBackend, key: str, dest_path: Path) -> None:
    client = _client(storage_backend)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(storage_backend.s3_bucket, key, str(dest_path))


def delete_file(storage_backend: models.StorageBackend, key: str) -> None:
    client = _client(storage_backend)
    client.delete_object(Bucket=storage_backend.s3_bucket, Key=key)


def generate_presigned_url(storage_backend: models.StorageBackend, key: str, expires_in: int = 3600) -> str:
    client = _client(storage_backend)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": storage_backend.s3_bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def check_bucket_access(storage_backend: models.StorageBackend) -> tuple[bool, str]:
    """Verifies the stored credentials can actually reach the configured bucket."""
    try:
        client = _client(storage_backend)
        client.head_bucket(Bucket=storage_backend.s3_bucket)
        return True, "Bucket is reachable"
    except Exception as exc:  # noqa: BLE001 - surface any botocore/credentials error as plain text
        return False, str(exc)[:2000]
