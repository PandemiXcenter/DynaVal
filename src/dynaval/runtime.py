"""Shared services, local media routes, and orderly application shutdown."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException
from nicegui import app
from platformdirs import user_cache_path, user_data_path
from starlette.responses import FileResponse

from dynaval.services.media import MediaService
from dynaval.services.sessions import SessionManager

if TYPE_CHECKING:
    from dynaval.ui.workspace import Workspace

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, data_dir: Path | None = None, *, native: bool = True) -> None:
        self.data_dir = data_dir or user_data_path("DynaVal", appauthor=False)
        self.cache_dir = (
            data_dir / "cache" if data_dir else user_cache_path("DynaVal", appauthor=False)
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.native = native
        self.manager = SessionManager(self.data_dir / "sessions")
        self.media = MediaService(self.cache_dir / "images")
        self.workspaces: set[Workspace] = set()
        self.allowed_media_sessions: set[str] = set()
        self._uploads = tempfile.TemporaryDirectory(prefix="dynaval-import-")
        self.upload_dir = Path(self._uploads.name)

    def register_media_route(self) -> None:
        @app.get("/dynaval/media/{media_id:path}", include_in_schema=False)
        async def media_file(media_id: str) -> FileResponse:
            if self.media.media_session(media_id) not in self.allowed_media_sessions:
                raise HTTPException(status_code=404)
            path = self.media.resolve_media(media_id)
            if path is None or not path.is_file():
                raise HTTPException(status_code=404)
            return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})

    async def close(self) -> None:
        for workspace in list(self.workspaces):
            try:
                await workspace.close()
            except Exception as error:
                logger.error("Session shutdown failed (%s)", type(error).__name__)
        await self.media.close()
        await asyncio.to_thread(self._uploads.cleanup)
