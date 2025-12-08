"""Compatibility alias for legacy imports expecting utils.s3_manager."""

from .s3_storage import S3StorageManager, get_s3_manager, is_s3_enabled


class S3Manager(S3StorageManager):
    """Compatibility wrapper around S3StorageManager."""

    pass


__all__ = ["S3Manager", "get_s3_manager", "is_s3_enabled"]
