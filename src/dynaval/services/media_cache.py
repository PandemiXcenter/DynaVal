"""Atomic per-session image cache with hashes retained after byte eviction."""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dynaval.domain.errors import MediaError

_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class CachedImage:
    sha256: str
    expected_hash: str
    display_path: Path

    @property
    def changed(self) -> bool:
        return self.sha256 != self.expected_hash


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".write-", delete=False) as file:
            temporary = file.name
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


class ImageCache:
    """Serialize index/file mutations across worker threads, without storing URLs."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._pinned_rows: dict[str, list[tuple[str, ...]]] = {}

    def session_directory(self, session_id: str) -> Path:
        return self.root / digest(session_id)

    def pin_row(self, session_id: str, references: list[str]) -> None:
        """Keep the current row and one prefetched row available while displayed."""
        with self._lock:
            row = tuple(sorted({digest(reference) for reference in references}))
            rows = self._pinned_rows.setdefault(digest(session_id), [])
            if row in rows:
                rows.remove(row)
            rows.append(row)
            del rows[:-2]

    def _index(self, directory: Path) -> dict[str, Any]:
        path = directory / "index.json"
        if not path.exists():
            return {"version": 1, "entries": {}}
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
            if index.get("version") != 1 or not isinstance(index.get("entries"), dict):
                raise ValueError("Unsupported cache metadata.")
            for key, item in index["entries"].items():
                if not _HASH.fullmatch(key) or not isinstance(item, dict):
                    raise ValueError("Invalid reference metadata.")
                if not _HASH.fullmatch(item.get("expected", "")):
                    raise ValueError("Invalid expected hash.")
                for field in ("sha256", "display_sha256"):
                    if item.get(field) is not None and not _HASH.fullmatch(item[field]):
                        raise ValueError("Invalid content hash.")
                if not isinstance(item.get("access", 0), (int, float)):
                    raise ValueError("Invalid cache timestamp.")
            return index
        except (OSError, ValueError, AttributeError, TypeError) as error:
            raise MediaError(
                "Image cache metadata is unreadable. Pause and restore its index from a backup "
                "before loading images; saved review decisions remain in the session."
            ) from error

    def _save_index(self, directory: Path, index: dict[str, Any]) -> None:
        _atomic_write(
            directory / "index.json",
            json.dumps(index, sort_keys=True, separators=(",", ":")).encode(),
        )

    @staticmethod
    def _paths(directory: Path, key: str, sha256: str) -> tuple[Path, Path]:
        stem = f"{key}-{sha256}"
        return directory / f"{stem}.source", directory / f"{stem}.png"

    def lookup(self, session_id: str, reference: str) -> CachedImage | None:
        with self._lock:
            directory = self.session_directory(session_id)
            index = self._index(directory)
            key = digest(reference)
            entry = index["entries"].get(key)
            if not entry or not entry.get("sha256"):
                return None
            source, display = self._paths(directory, key, entry["sha256"])
            try:
                if digest(source.read_bytes()) != entry["sha256"] or digest(
                    display.read_bytes()
                ) != entry.get("display_sha256"):
                    return None
            except FileNotFoundError:
                return None
            entry["access"] = time.time_ns()
            self._save_index(directory, index)
            return CachedImage(entry["sha256"], entry["expected"], display)

    def publish(
        self, session_id: str, reference: str, source_bytes: bytes, display_bytes: bytes
    ) -> CachedImage:
        with self._lock:
            directory = self.session_directory(session_id)
            directory.mkdir(parents=True, exist_ok=True)
            index = self._index(directory)
            key, sha256 = digest(reference), digest(source_bytes)
            expected = index["entries"].get(key, {}).get("expected", sha256)
            source, display = self._paths(directory, key, sha256)
            _atomic_write(source, source_bytes)
            _atomic_write(display, display_bytes)
            index["entries"][key] = {
                "expected": expected,
                "sha256": sha256,
                "display_sha256": digest(display_bytes),
                "access": time.time_ns(),
            }
            self._save_index(directory, index)
            self._evict(directory, index, protected=key)
            return CachedImage(sha256, expected, display)

    def _evict(self, directory: Path, index: dict[str, Any], protected: str) -> None:
        """Evict old bytes; the current and next row may exceed the cache budget."""
        live: set[Path] = set()
        sizes: dict[str, int] = {}
        for key, entry in index["entries"].items():
            if entry.get("sha256"):
                paths = self._paths(directory, key, entry["sha256"])
                live.update(paths)
                sizes[key] = sum(path.stat().st_size for path in paths if path.exists())
        # Remove abandoned images from interrupted/changed publications.
        for pattern in ("*.source", "*.png", ".write-*"):
            for path in directory.glob(pattern):
                if path not in live:
                    path.unlink(missing_ok=True)
        total = sum(sizes.values())
        pinned = {key for row in self._pinned_rows.get(directory.name, []) for key in row}
        for key in sorted(sizes, key=lambda item: index["entries"][item]["access"]):
            if total <= self.max_bytes:
                break
            if key == protected or key in pinned:
                continue
            entry = index["entries"][key]
            for path in self._paths(directory, key, entry["sha256"]):
                path.unlink(missing_ok=True)
            total -= sizes[key]
            entry["sha256"] = None
            entry["display_sha256"] = None
        self._save_index(directory, index)

    def acknowledge(
        self, session_id: str, reference: str, expected_sha256: str | None = None
    ) -> None:
        with self._lock:
            directory = self.session_directory(session_id)
            index = self._index(directory)
            entry = index["entries"].get(digest(reference))
            if not entry or not entry.get("sha256"):
                raise MediaError("Load the replacement image before acknowledging its change.")
            if expected_sha256 is not None and entry["sha256"] != expected_sha256:
                raise MediaError(
                    "The replacement image changed again. Reload and inspect it first."
                )
            entry["expected"] = entry["sha256"]
            self._save_index(directory, index)

    def invalidate(self, session_id: str) -> None:
        """Path repairs invalidate bytes while retaining the original expected hashes."""
        with self._lock:
            directory = self.session_directory(session_id)
            index = self._index(directory)
            if not directory.exists():
                return
            for entry in index["entries"].values():
                entry["sha256"] = None
                entry["display_sha256"] = None
            self._save_index(directory, index)
            for pattern in ("*.source", "*.png"):
                for path in directory.glob(pattern):
                    path.unlink(missing_ok=True)
