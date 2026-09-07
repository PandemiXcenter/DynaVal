"""Bounded asynchronous image loading and opaque session-owned display registration."""

import asyncio
import io
import secrets
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from dynaval.domain.errors import MediaError
from dynaval.domain.models import Cell, MediaRecord, PathMapping
from dynaval.services.media_cache import CachedImage, ImageCache
from dynaval.services.media_paths import is_remote_reference, resolve_local_reference


@dataclass(frozen=True)
class MediaLimits:
    max_download_bytes: int = 20 * 1024 * 1024
    max_pixels: int = 50_000_000
    timeout_seconds: float = 30
    retries: int = 2
    max_redirects: int = 3
    concurrent_loads: int = 3
    cache_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            min(self.max_download_bytes, self.max_pixels, self.concurrent_loads, self.cache_bytes)
            <= 0
        ):
            raise ValueError("Image size, concurrency, and cache limits must be positive.")
        if self.timeout_seconds <= 0 or min(self.retries, self.max_redirects) < 0:
            raise ValueError("Timeout must be positive and retry/redirect limits nonnegative.")


@dataclass(frozen=True)
class _Registration:
    session_id: str
    reference: str
    path: Path


class _RetryableError(Exception):
    pass


def _read_local(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as file:
            data = file.read(limit + 1)
    except FileNotFoundError as error:
        raise MediaError(
            "Image not found. Mount its share or pause to repair the image path."
        ) from error
    except PermissionError as error:
        raise MediaError(
            "Image access was denied. Check filesystem permissions and retry."
        ) from error
    except OSError as error:
        raise MediaError(
            "The image could not be read. Check its path and mounted share, then retry."
        ) from error
    if len(data) > limit:
        raise MediaError("This image exceeds the configured file-size limit. Use a smaller image.")
    return data


def _display_image(source: bytes, max_pixels: int) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise MediaError("Unsupported image format. Use JPEG, PNG, or WebP.")
                if image.width * image.height > max_pixels:
                    raise MediaError(
                        "This image exceeds the configured pixel limit. Use a smaller image."
                    )
                image.load()
                oriented = ImageOps.exif_transpose(image)
                alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                display = oriented.convert("RGBA" if alpha else "RGB")
                output = io.BytesIO()
                # Metadata is deliberately excluded from the display copy.
                display.save(output, format="PNG")
                return output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise MediaError("This image exceeds the safe pixel limit. Use a smaller image.") from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise MediaError(
            "Image data is damaged or unsupported. Replace it with a JPEG, PNG, or WebP."
        ) from error


class MediaService:
    def __init__(
        self,
        cache_root: Path,
        http_client: httpx.AsyncClient | None = None,
        *,
        limits: MediaLimits | None = None,
    ) -> None:
        self.limits = limits or MediaLimits()
        self.cache = ImageCache(cache_root, self.limits.cache_bytes)
        self._client = http_client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._owns_client = http_client is None
        self._slots = asyncio.Semaphore(self.limits.concurrent_loads)
        self._inflight: dict[tuple[object, ...], asyncio.Task[MediaRecord]] = {}
        self._registrations: dict[str, _Registration] = {}
        self._closed = False

    async def load_row(
        self,
        session_id: str,
        cells: list[Cell],
        reference_columns: list[int],
        reference_base: str | None,
        path_mappings: list[PathMapping],
        *,
        force: bool = False,
    ) -> list[MediaRecord]:
        if self._closed:
            raise MediaError("The image loader is closed. Restart the application.")
        if any(column < 0 or column >= len(cells) for column in reference_columns):
            raise MediaError("The selected image column is outside the imported dataset schema.")
        await asyncio.to_thread(
            self.cache.pin_row, session_id, [cells[column].text for column in reference_columns]
        )

        async def load(column: int) -> MediaRecord:
            cell = cells[column]
            if cell.kind != "string" or not cell.text.strip():
                return MediaRecord(
                    reference_col=column,
                    reference=cell.text,
                    error="Image reference is empty or is not text. Repair the source or skip.",
                )
            key = (
                session_id,
                cell.text,
                reference_base,
                tuple((mapping.source, mapping.target) for mapping in path_mappings),
                force,
            )
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._load_reference(
                        session_id, cell.text, reference_base, path_mappings, force
                    )
                )
                self._inflight[key] = task
                task.add_done_callback(lambda completed: self._forget_task(key, completed))
            result = await asyncio.shield(task)
            return result.model_copy(update={"reference_col": column})

        return list(await asyncio.gather(*(load(column) for column in sorted(reference_columns))))

    def _forget_task(self, key: tuple[object, ...], task: asyncio.Task[MediaRecord]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]

    async def _load_reference(
        self,
        session_id: str,
        reference: str,
        reference_base: str | None,
        mappings: list[PathMapping],
        force: bool,
    ) -> MediaRecord:
        try:
            async with asyncio.timeout(self.limits.timeout_seconds), self._slots:
                cached = (
                    None
                    if force
                    else await asyncio.to_thread(self.cache.lookup, session_id, reference)
                )
                if cached is None:
                    if is_remote_reference(reference):
                        source = await self._download(reference)
                    else:
                        path = await asyncio.to_thread(
                            resolve_local_reference, reference, reference_base, mappings
                        )
                        source = await asyncio.to_thread(
                            _read_local, path, self.limits.max_download_bytes
                        )
                    display = await asyncio.to_thread(
                        _display_image, source, self.limits.max_pixels
                    )
                    cached = await asyncio.to_thread(
                        self.cache.publish, session_id, reference, source, display
                    )
                return self._register(session_id, reference, cached)
        except TimeoutError:
            message = "Image loading timed out. Check the connection or mounted share and retry."
        except MediaError as error:
            message = str(error)
        except OSError:
            message = (
                "The image cache could not be saved. "
                "Check free disk space and permissions, then retry."
            )
        except (ValueError, httpx.InvalidURL):
            message = "The image reference is malformed. Pause and repair its path or URL."
        except httpx.RequestError:
            message = "The image server returned unreadable data. Check its image URL and retry."
        return MediaRecord(reference_col=0, reference=reference, error=message)

    def _register(self, session_id: str, reference: str, cached: CachedImage) -> MediaRecord:
        media_id = next(
            (
                token
                for token, registration in self._registrations.items()
                if registration.session_id == session_id
                and registration.reference == reference
                and registration.path == cached.display_path
            ),
            None,
        )
        if media_id is None:
            media_id = secrets.token_urlsafe(24)
            self._registrations[media_id] = _Registration(
                session_id, reference, cached.display_path
            )
        return MediaRecord(
            reference_col=0,
            reference=reference,
            sha256=cached.sha256,
            media_id=media_id,
            path=str(cached.display_path),
            changed=cached.changed,
            error=(
                "The source image changed since it was first loaded. Inspect and acknowledge "
                "the replacement before continuing."
                if cached.changed
                else None
            ),
        )

    async def _download(self, reference: str) -> bytes:
        for attempt in range(self.limits.retries + 1):
            try:
                return await self._download_attempt(reference)
            except (httpx.TimeoutException, httpx.TransportError, _RetryableError) as error:
                if attempt == self.limits.retries:
                    raise MediaError(
                        "The image download failed after retrying. Check the connection and retry."
                    ) from error
                await asyncio.sleep(0.25 * 2**attempt)
        raise AssertionError("The bounded retry loop must return or raise.")

    async def _download_attempt(self, reference: str) -> bytes:
        url = reference
        for redirects in range(self.limits.max_redirects + 1):
            parsed = urlsplit(url)
            if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
                raise MediaError("Image downloads and redirects must use a valid HTTP(S) URL.")
            async with self._client.stream(
                "GET", url, follow_redirects=False, timeout=self.limits.timeout_seconds
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise MediaError(
                            "The image server returned a redirect without a destination."
                        )
                    if redirects >= self.limits.max_redirects:
                        raise MediaError(
                            "The image server redirected too many times. Use its final image URL."
                        )
                    url = urljoin(url, location)
                    continue
                if response.status_code in {408, 429, 500, 502, 503, 504}:
                    raise _RetryableError
                if not response.is_success:
                    raise MediaError(
                        f"The image server returned HTTP {response.status_code}. "
                        "Check access and retry."
                    )
                length = response.headers.get("content-length")
                if length and length.isdecimal() and int(length) > self.limits.max_download_bytes:
                    raise MediaError(
                        "This image exceeds the configured download limit. Use a smaller image."
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > self.limits.max_download_bytes:
                        raise MediaError(
                            "This image exceeds the configured download limit. Use a smaller image."
                        )
                    data.extend(chunk)
                return bytes(data)
        raise AssertionError("The bounded redirect loop must return or raise.")

    def resolve_media(self, media_id: str) -> Path | None:
        registration = self._registrations.get(media_id)
        if registration and registration.path.is_file():
            return registration.path
        return None

    def media_session(self, media_id: str) -> str | None:
        registration = self._registrations.get(media_id)
        return registration.session_id if registration else None

    async def acknowledge_change(
        self, session_id: str, reference: str, *, expected_sha256: str | None = None
    ) -> None:
        await asyncio.to_thread(self.cache.acknowledge, session_id, reference, expected_sha256)

    async def invalidate_session(self, session_id: str) -> None:
        pending = [task for key, task in self._inflight.items() if key[0] == session_id]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.to_thread(self.cache.invalidate, session_id)
        self._registrations = {
            token: record
            for token, record in self._registrations.items()
            if record.session_id != session_id
        }

    async def close(self) -> None:
        self._closed = True
        pending = list(self._inflight.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._registrations.clear()
        if self._owns_client:
            await self._client.aclose()
