"""
simplex.py — a tiny async client for data.guythatlives.net (Simplex).

This wraps the handful of endpoints MrSimplex needs:

    GET /api/v1/me
    GET /api/v1/files?parent=&type=&search=&trashed=
    GET /api/v1/files/:id
        /api/v1/files/:id/raw      (we only build the URL; FFmpeg streams it)

The API is documented at https://data.guythatlives.net. Everything here is
intentionally defensive about response shapes, because the only thing we can
rely on is that a "file" has an id and a name.
"""

from __future__ import annotations

import aiohttp
from dataclasses import dataclass
from typing import Any, Optional


API_BASE = "https://data.guythatlives.net/api/v1"


@dataclass
class SimplexFile:
    """A normalized file/folder entry returned by the API."""

    id: str
    name: str
    is_folder: bool
    mime: str = ""
    size: int = 0
    parent: Optional[str] = None
    raw: dict[str, Any] = None  # the original payload, in case you want more

    @property
    def is_audio(self) -> bool:
        if self.is_folder:
            return False
        if self.mime.lower().startswith("audio"):
            return True
        lowered = self.name.lower()
        return lowered.endswith(
            (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma", ".webm")
        )


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _normalize(item: dict[str, Any]) -> SimplexFile:
    """Turn one raw API object into a SimplexFile, tolerating field-name drift."""
    fid = str(_first(item, "id", "_id", "fileId", "uuid", default=""))
    name = str(_first(item, "name", "filename", "title", default="(unnamed)"))
    mime = str(_first(item, "mimeType", "mime", "contentType", "mediaType", default=""))

    # Decide folder vs file from whatever signal is present.
    type_field = str(_first(item, "type", "kind", default="")).lower()
    is_folder = (
        bool(item.get("isFolder"))
        or type_field in ("folder", "dir", "directory")
        or mime in ("application/vnd.simplex.folder", "inode/directory")
    )

    size_val = _first(item, "size", "bytes", "fileSize", default=0)
    try:
        size = int(size_val)
    except (TypeError, ValueError):
        size = 0

    parent = _first(item, "parent", "parentId", "folder", default=None)
    parent = str(parent) if parent not in (None, "") else None

    return SimplexFile(
        id=fid, name=name, is_folder=is_folder, mime=mime,
        size=size, parent=parent, raw=item,
    )


def _extract_list(payload: Any) -> list[dict[str, Any]]:
    """Pull the array of file objects out of whatever wrapper the API used."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("files", "data", "items", "results", "children", "entries"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        # Some APIs wrap a single page under data.files etc.
        for key in ("data", "result", "page"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                found = _extract_list(inner)
                if found:
                    return found
    return []


class SimplexClient:
    """Async wrapper around the Simplex file API.

    Use as an async context manager, or call .close() yourself:

        async with SimplexClient(api_key) as client:
            files = await client.list_files(parent=folder_id)
    """

    def __init__(self, api_key: str, base_url: str = API_BASE):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "SimplexClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "X-API-Key": self.api_key,
                    "Accept": "application/json",
                }
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def me(self) -> dict[str, Any]:
        """Return the authenticated account info (used to verify the API key)."""
        data = await self._get_json("/me")
        return data if isinstance(data, dict) else {"raw": data}

    async def list_files(
        self,
        parent: Optional[str] = None,
        type_: Optional[str] = None,
        search: Optional[str] = None,
        trashed: bool = False,
    ) -> list[SimplexFile]:
        params: dict[str, Any] = {}
        if parent is not None:
            params["parent"] = parent
        if type_:
            params["type"] = type_
        if search:
            params["search"] = search
        if trashed:
            params["trashed"] = "true"

        payload = await self._get_json("/files", params=params or None)
        return [_normalize(x) for x in _extract_list(payload)]

    async def get_file(self, file_id: str) -> SimplexFile:
        payload = await self._get_json(f"/files/{file_id}")
        if isinstance(payload, dict):
            # Some APIs nest the object under "file"/"data".
            inner = payload.get("file") or payload.get("data") or payload
            if isinstance(inner, dict):
                return _normalize(inner)
        raise ValueError(f"Unexpected response for file {file_id!r}")

    def raw_url(self, file_id: str) -> str:
        """The streamable URL for a file (honors Range requests)."""
        return f"{self.base_url}/files/{file_id}/raw"
