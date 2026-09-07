"""Writable user directories outside the application and its bundle."""

from pathlib import Path

from platformdirs import PlatformDirs


def data_directory() -> Path:
    path = PlatformDirs("DynaVal", appauthor=False).user_data_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_directory() -> Path:
    path = data_directory() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_directory() -> Path:
    path = PlatformDirs("DynaVal", appauthor=False).user_cache_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_directory() -> Path:
    path = PlatformDirs("DynaVal", appauthor=False).user_log_path
    path.mkdir(parents=True, exist_ok=True)
    return path
