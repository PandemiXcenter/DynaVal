"""Synthetic images and mocked HTTP exercise media safety and durable identity."""

import asyncio
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from PIL import Image

from dynaval.domain.errors import MediaError
from dynaval.domain.models import Cell, MediaRecord, PathMapping
from dynaval.services.media import MediaLimits, MediaService
from dynaval.services.media_cache import ImageCache, digest
from dynaval.services.media_paths import resolve_local_reference


def image_bytes(
    color: str = "red", *, format: str = "PNG", size: tuple[int, int] = (4, 3)
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=format)
    return output.getvalue()


async def load(service: MediaService, reference: str, *, force: bool = False) -> MediaRecord:
    return (await service.load_row("session", [Cell(text=reference)], [0], None, [], force=force))[
        0
    ]


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
async def test_local_images_are_normalized_registered_and_source_unchanged(
    tmp_path: Path, format: str
) -> None:
    data = image_bytes(format=format)
    source = tmp_path / "source.image"
    source.write_bytes(data)
    service = MediaService(tmp_path / "cache")
    try:
        record = await load(service, str(source))
        assert record.error is None
        assert record.sha256 == digest(data)
        assert record.media_id and "/" not in record.media_id
        assert service.media_session(record.media_id) == "session"
        displayed = service.resolve_media(record.media_id)
        assert displayed and displayed != source
        with Image.open(displayed) as normalized:
            assert normalized.format == "PNG"
            assert normalized.size == (4, 3)
        cached_sources = list((tmp_path / "cache").rglob("*.source"))
        assert len(cached_sources) == 1
        assert cached_sources[0].read_bytes() == source.read_bytes() == data
        assert service.resolve_media(str(source)) is None
        assert service.resolve_media("../source.image") is None
        assert service.media_session("unknown") is None
    finally:
        await service.close()
    assert service.resolve_media(record.media_id) is None


async def test_exif_orientation_is_applied_only_to_display(tmp_path: Path) -> None:
    output = io.BytesIO()
    original = Image.new("RGB", (6, 2), "green")
    exif = original.getexif()
    exif[274] = 6
    original.save(output, format="JPEG", exif=exif)
    source = tmp_path / "rotated.jpg"
    source.write_bytes(output.getvalue())
    service = MediaService(tmp_path / "cache")
    try:
        record = await load(service, str(source))
        assert record.error is None and record.path
        with Image.open(record.path) as display:
            assert display.size == (2, 6)
        assert source.read_bytes() == output.getvalue()
    finally:
        await service.close()


async def test_remote_cache_is_available_offline_and_does_not_store_private_url(
    tmp_path: Path,
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=image_bytes())

    reference = "https://example.test/private/photo?secret=private-token"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client)
        first = await load(service, reference)
        second = await load(service, reference)
        assert first.error is None and second.media_id == first.media_id and calls == 1
        await service.close()

    def offline(request: httpx.Request) -> httpx.Response:
        raise AssertionError("A cache hit must not require network access.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
        service = MediaService(tmp_path, client)
        resumed = await load(service, reference)
        assert resumed.error is None and resumed.sha256 == first.sha256
        await service.close()
    index_text = next(tmp_path.rglob("index.json")).read_text()
    assert "private-token" not in index_text and "example.test" not in index_text


async def test_duplicate_reference_downloads_are_coalesced_and_columns_sorted(
    tmp_path: Path,
) -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, content=image_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client)
        references = [Cell(text="https://example.test/image")] * 3
        left, right = await asyncio.gather(
            service.load_row("session", references, [2, 0], None, []),
            service.load_row("session", references, [1], None, []),
        )
        assert calls == 1
        assert [record.reference_col for record in left] == [0, 2]
        assert {record.media_id for record in left + right} == {left[0].media_id}
        await service.close()


async def test_download_concurrency_is_bounded(tmp_path: Path) -> None:
    active = peak = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=image_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client, limits=MediaLimits(concurrent_loads=2))
        records = await service.load_row(
            "session",
            [Cell(text=f"https://example.test/{i}") for i in range(5)],
            list(range(5)),
            None,
            [],
        )
        assert peak == 2 and all(record.error is None for record in records)
        await service.close()


@pytest.mark.parametrize("failure", ["timeout", "transport", "503"])
async def test_transient_failures_retry_at_most_twice(tmp_path: Path, failure: str) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("private URL must not leak", request=request)
        if failure == "transport":
            raise httpx.ConnectError("private URL must not leak", request=request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client)
        record = await load(service, "https://example.test/private")
        assert calls == 3 and record.error and "retry" in record.error
        assert "private" not in record.error
        assert record.sha256 is None and record.media_id is None
        await service.close()


async def test_retry_can_recover(tmp_path: Path) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else httpx.Response(200, content=image_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client)
        record = await load(service, "https://example.test/image")
        assert record.error is None and calls == 2
        await service.close()


async def test_overall_timeout_covers_slow_stream(tmp_path: Path) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, content=image_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client, limits=MediaLimits(timeout_seconds=0.02))
        record = await load(service, "https://example.test/image")
        assert record.error and "timed out" in record.error
        await service.close()


@pytest.mark.parametrize("hops, succeeds", [(3, True), (4, False)])
async def test_redirect_limit(tmp_path: Path, hops: int, succeeds: bool) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        current = int(request.url.path.strip("/"))
        if current < hops:
            return httpx.Response(302, headers={"location": f"/{current + 1}"})
        return httpx.Response(200, content=image_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client)
        record = await load(service, "https://example.test/0")
        assert (record.error is None) is succeeds
        assert calls == 4
        await service.close()


@pytest.mark.parametrize(
    "location", ["file:///etc/passwd", "ftp://example.test/file", "data:image/png,abc"]
)
async def test_redirects_cannot_fetch_other_schemes(tmp_path: Path, location: str) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": location})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client)
        record = await load(service, "https://example.test/image")
        assert record.error and ("HTTP(S)" in record.error or "malformed" in record.error)
        assert calls == 1 and record.sha256 is None
        await service.close()


async def test_http_failures_do_not_retry_or_expose_response_body(tmp_path: Path) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="private patient information")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client)
        record = await load(service, "https://example.test/private")
        assert (
            record.error and "403" in record.error and "private" not in record.error and calls == 1
        )
        await service.close()


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"x" * 8
        yield b"y" * 8


@pytest.mark.parametrize("announced", [True, False])
async def test_download_size_limit_enforced_for_headers_and_stream(
    tmp_path: Path, announced: bool
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": "16"} if announced else {}, stream=ChunkStream()
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = MediaService(tmp_path, client, limits=MediaLimits(max_download_bytes=10))
        record = await load(service, "https://example.test/image")
        assert record.error and "download limit" in record.error
        assert not list(tmp_path.rglob("*.source"))
        await service.close()


@pytest.mark.parametrize(
    "content, limits, expected",
    [
        (b"not an image", MediaLimits(), "damaged"),
        (image_bytes(format="GIF"), MediaLimits(), "Unsupported"),
        (image_bytes(size=(8, 8)), MediaLimits(max_pixels=63), "pixel limit"),
        (image_bytes(), MediaLimits(max_download_bytes=10), "file-size limit"),
    ],
)
async def test_bad_images_fail_without_registered_media(
    tmp_path: Path, content: bytes, limits: MediaLimits, expected: str
) -> None:
    source = tmp_path / "image"
    source.write_bytes(content)
    service = MediaService(tmp_path / "cache", limits=limits)
    record = await load(service, str(source))
    assert record.error and expected in record.error
    assert record.path is None and record.media_id is None and record.sha256 is None
    await service.close()


async def test_blank_null_missing_and_unavailable_references(tmp_path: Path) -> None:
    service = MediaService(tmp_path / "cache")
    records = await service.load_row(
        "session",
        [
            Cell(text=""),
            Cell(text="null", kind="null"),
            Cell(text="", kind="missing"),
            Cell(text=str(tmp_path / "absent.png")),
        ],
        [0, 1, 2, 3],
        None,
        [],
    )
    assert len(records) == 4 and all(record.error and record.sha256 is None for record in records)
    assert "not found" in (records[3].error or "")
    await service.close()


def test_windows_mapping_uses_components_casefold_and_longest_prefix(tmp_path: Path) -> None:
    mappings = [
        PathMapping(source=r"\\SERVER\Share", target=str(tmp_path / "broad")),
        PathMapping(source=r"\\server\share\images", target=str(tmp_path / "specific")),
    ]
    assert (
        resolve_local_reference(r"\\Server\SHARE\IMAGES\a.jpg", None, mappings, platform="posix")
        == tmp_path / "specific" / "a.jpg"
    )
    assert (
        resolve_local_reference(r"\\server\share\images2\a.jpg", None, mappings, platform="posix")
        == tmp_path / "broad" / "images2" / "a.jpg"
    )
    with pytest.raises(MediaError, match="prefix mapping"):
        resolve_local_reference(r"\\server\share2\images\a.jpg", None, mappings, platform="posix")


def test_windows_mapping_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    mappings = [PathMapping(source=r"\\server\share", target=str(root))]
    with pytest.raises(MediaError, match="escapes"):
        resolve_local_reference(r"\\server\share\..\secret.jpg", None, mappings)
    assert (
        resolve_local_reference(r"\\server\share\folder\..\okay.jpg", None, mappings)
        == root / "okay.jpg"
    )


def test_windows_mapping_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    mappings = [PathMapping(source=r"\\server\share", target=str(root))]
    try:
        (root / "link").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows account does not have symlink creation permission.")
    with pytest.raises(MediaError, match="escapes"):
        resolve_local_reference(r"\\server\share\link\secret.jpg", None, mappings)


def test_native_drive_paths_and_relative_paths(tmp_path: Path) -> None:
    assert resolve_local_reference(r"C:\images\one.jpg", None, [], platform="nt") == Path(
        r"C:\images\one.jpg"
    )
    assert resolve_local_reference(r"\\server\share\one.jpg", None, [], platform="nt") == Path(
        r"\\server\share\one.jpg"
    )
    with pytest.raises(MediaError, match="Drive-relative"):
        resolve_local_reference(r"C:one.jpg", None, [], platform="nt")
    assert (
        resolve_local_reference("folder/one.jpg", str(tmp_path), [])
        == tmp_path / "folder" / "one.jpg"
    )
    assert (
        resolve_local_reference(r"folder\one.jpg", str(tmp_path), [])
        == tmp_path / "folder" / "one.jpg"
    )
    with pytest.raises(MediaError, match="base directory"):
        resolve_local_reference("one.jpg", None, [])
    with pytest.raises(MediaError, match=r"HTTP\(S\)"):
        resolve_local_reference("file:///tmp/one.jpg", None, [])


async def test_changed_source_requires_explicit_acknowledgement_and_survives_restart(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes("red"))
    service = MediaService(tmp_path / "cache")
    original = await load(service, str(source))
    source.write_bytes(image_bytes("blue"))
    unchanged_cache = await load(service, str(source))
    assert unchanged_cache.sha256 == original.sha256
    replacement = await load(service, str(source), force=True)
    assert replacement.changed and replacement.error and replacement.path
    assert replacement.sha256 != original.sha256
    await service.close()
    service = MediaService(tmp_path / "cache")
    resumed = await load(service, str(source))
    assert resumed.changed and resumed.error
    with pytest.raises(MediaError, match="changed again"):
        await service.acknowledge_change("session", str(source), expected_sha256=original.sha256)
    await service.acknowledge_change("session", str(source), expected_sha256=resumed.sha256)
    accepted = await load(service, str(source))
    assert accepted.error is None and not accepted.changed and accepted.sha256 == replacement.sha256
    await service.close()


async def test_eviction_keeps_expected_hash_and_pins_current_and_next_rows(tmp_path: Path) -> None:
    sources = [tmp_path / f"{i}.png" for i in range(3)]
    for source in sources:
        source.write_bytes(image_bytes())
    service = MediaService(tmp_path / "cache", limits=MediaLimits(cache_bytes=1))
    first = await load(service, str(sources[0]))
    second = await load(service, str(sources[1]))
    assert first.media_id and service.resolve_media(first.media_id)
    assert second.media_id and service.resolve_media(second.media_id)
    await load(service, str(sources[2]))
    assert service.resolve_media(first.media_id) is None
    index = json.loads(next((tmp_path / "cache").rglob("index.json")).read_text())
    entry = index["entries"][digest(str(sources[0]))]
    assert entry["expected"] == first.sha256 and entry["sha256"] is None
    sources[0].write_bytes(image_bytes("blue"))
    restored = await load(service, str(sources[0]))
    assert restored.changed and restored.error
    await service.close()


async def test_mapping_repair_invalidates_old_tokens_but_preserves_identity(tmp_path: Path) -> None:
    reference = r"\\server\share\image.png"
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "image.png").write_bytes(image_bytes("red"))
    (second / "image.png").write_bytes(image_bytes("blue"))
    service = MediaService(tmp_path / "cache")
    row = [Cell(text=reference)]
    before = (
        await service.load_row(
            "session", row, [0], None, [PathMapping(source=r"\\server\share", target=str(first))]
        )
    )[0]
    assert before.error is None and before.media_id
    await service.invalidate_session("session")
    assert service.resolve_media(before.media_id) is None
    after = (
        await service.load_row(
            "session", row, [0], None, [PathMapping(source=r"\\server\share", target=str(second))]
        )
    )[0]
    assert after.changed and after.sha256 != before.sha256
    await service.close()


async def test_corrupt_cached_bytes_are_reloaded_without_forgetting_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes())
    service = MediaService(tmp_path / "cache")
    first = await load(service, str(source))
    next((tmp_path / "cache").rglob("*.source")).write_bytes(b"corrupt")
    source.write_bytes(image_bytes("blue"))
    next_load = await load(service, str(source))
    assert next_load.sha256 != first.sha256 and next_load.changed
    await service.close()


async def test_corrupt_cache_metadata_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes())
    service = MediaService(tmp_path / "cache")
    await load(service, str(source))
    next((tmp_path / "cache").rglob("index.json")).write_text("{broken", encoding="utf-8")
    broken = await load(service, str(source))
    assert broken.error and "metadata" in broken.error and broken.media_id is None
    await service.close()


async def test_read_only_cache_failure_is_actionable_and_not_a_success(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes())
    service = MediaService(tmp_path / "cache")
    with patch.object(ImageCache, "publish", side_effect=PermissionError):
        failed = await load(service, str(source))
    assert failed.error and "permissions" in failed.error and failed.media_id is None
    recovered = await load(service, str(source))
    assert recovered.error is None
    await service.close()


async def test_cache_atomic_write_failure_retains_previous_identity(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes())
    service = MediaService(tmp_path / "cache")
    first = await load(service, str(source))
    index_path = next((tmp_path / "cache").rglob("index.json"))
    before = index_path.read_bytes()
    source.write_bytes(image_bytes("blue"))
    with patch("dynaval.services.media_cache.os.replace", side_effect=OSError("disk full")):
        failed = await load(service, str(source), force=True)
    assert failed.error and index_path.read_bytes() == before
    recovered = await load(service, str(source), force=True)
    assert recovered.sha256 != first.sha256 and recovered.changed
    assert not list((tmp_path / "cache").rglob(".write-*"))
    await service.close()


async def test_session_registration_isolated_and_session_ids_cannot_escape_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes())
    service = MediaService(tmp_path / "cache")
    records = await service.load_row("../../escape", [Cell(text=str(source))], [0], None, [])
    record = records[0]
    assert record.error is None and record.media_id and record.path
    assert service.media_session(record.media_id) == "../../escape"
    assert Path(record.path).is_relative_to(tmp_path / "cache")
    await service.close()


async def test_closed_service_and_invalid_columns_are_rejected(tmp_path: Path) -> None:
    service = MediaService(tmp_path)
    with pytest.raises(MediaError, match="outside"):
        await service.load_row("session", [Cell(text="x")], [-1], None, [])
    with pytest.raises(MediaError, match="Load the replacement"):
        await service.acknowledge_change("session", "unknown")
    await service.close()
    with pytest.raises(MediaError, match="closed"):
        await load(service, "https://example.test/image")
