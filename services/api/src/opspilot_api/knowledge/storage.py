"""Private object-storage abstraction with a safe local implementation."""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4


class StorageError(Exception):
    pass


class ObjectStorage(Protocol):
    def put(self, workspace_id: UUID, content: bytes, expected_hash: str) -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def delete_object(self, **kwargs: Any) -> Any: ...


class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if key.startswith(("/", "\\")) or ".." in Path(key).parts:
            raise StorageError("invalid storage key")
        target = (self.root / key).resolve()
        if self.root != target and self.root not in target.parents:
            raise StorageError("storage key escapes configured root")
        return target

    def put(self, workspace_id: UUID, content: bytes, expected_hash: str) -> str:
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != expected_hash:
            raise StorageError("content hash verification failed")
        key = f"{workspace_id}/{actual_hash[:2]}/{uuid4().hex}.bin"
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError("stored object not found")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3CompatibleStorage:
    """Adapter over an injected boto-style client; no AWS dependency is required."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def put(self, workspace_id: UUID, content: bytes, expected_hash: str) -> str:
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise StorageError("content hash verification failed")
        key = f"knowledge/{workspace_id}/{expected_hash[:2]}/{uuid4().hex}.bin"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content)
        return key

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return bytes(response["Body"].read())

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
