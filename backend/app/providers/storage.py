"""Storage abstraction. Local impl for dev; OSS impl used in production."""

import os
import shutil
import uuid
from pathlib import Path
from typing import Protocol
from urllib.parse import quote


class StorageProvider(Protocol):
    async def upload(self, local_path: Path, key: str) -> None: ...
    async def signed_url(self, key: str, expires_seconds: int = 900) -> str: ...
    async def delete(self, key: str) -> None: ...
    async def open_local(self, key: str) -> Path | None:
        """Return a local path for direct serving, None when not applicable."""
        ...
    async def download_to(self, key: str, destination: Path) -> Path: ...


def build_key(env: str, user_part: str, kind: str, ext: str) -> str:
    return f"{env}/{user_part}/{kind}/{uuid.uuid4()}.{ext.lstrip('.')}"


class LocalStorageProvider:
    """Dev-only backend: files live under storage_dir, served by the api process."""

    def __init__(self, root: Path, public_base_url: str = "") -> None:
        self._root = root
        self._public_base_url = public_base_url.rstrip("/")
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        if ".." in Path(key).parts:
            raise ValueError("invalid storage key")
        return self._root / key

    async def upload(self, local_path: Path, key: str) -> None:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)

    async def signed_url(self, key: str, expires_seconds: int = 900) -> str:
        return f"{self._public_base_url}/files/{quote(key, safe='')}"

    async def delete(self, key: str) -> None:
        try:
            os.remove(self._resolve(key))
        except FileNotFoundError:
            pass

    async def open_local(self, key: str) -> Path | None:
        p = self._resolve(key)
        return p if p.is_file() else None

    async def download_to(self, key: str, destination: Path) -> Path:
        source = await self.open_local(key)
        if source is None:
            raise FileNotFoundError(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination


class OSSStorageProvider:
    """Production backend over Alibaba Cloud OSS. Requires oss2 installed."""

    def __init__(self, endpoint: str, bucket_name: str, access_key: str, secret_key: str) -> None:
        try:
            import oss2
        except ImportError as e:
            raise RuntimeError("oss2 is required for OSSStorageProvider") from e
        auth = oss2.Auth(access_key, secret_key)
        self._bucket = oss2.Bucket(auth, endpoint, bucket_name)

    async def upload(self, local_path: Path, key: str) -> None:
        self._bucket.put_object_from_file(key, str(local_path))

    async def signed_url(self, key: str, expires_seconds: int = 900) -> str:
        return str(self._bucket.sign_url("GET", key, expires_seconds))

    async def delete(self, key: str) -> None:
        self._bucket.delete_object(key)

    async def open_local(self, key: str) -> Path | None:
        return None

    async def download_to(self, key: str, destination: Path) -> Path:
        raise NotImplementedError("OSS download_to must be implemented with the object SDK")
