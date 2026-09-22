"""Explicit bulk file actions for selected, completed local videos."""
from __future__ import annotations

import shutil
import sqlite3
import zipfile
from pathlib import Path

from services.database import VideoDatabase


def export_videos(paths: list[Path], destination: Path) -> tuple[int, int]:
    """Copy MP4s to a chosen folder, never overwriting an existing filename."""
    if not destination.is_dir():
        raise ValueError("Thư mục đích không tồn tại.")
    copied = skipped = 0
    for path in paths:
        if not path.is_file():
            skipped += 1
            continue
        target = destination / path.name
        try:
            with path.open("rb") as source, target.open("xb") as output:
                try:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                except Exception:
                    output.close()
                    target.unlink(missing_ok=True)
                    raise
            copied += 1
        except FileExistsError:
            skipped += 1
    return copied, skipped


def export_videos_zip(paths: list[Path], destination: Path) -> tuple[int, int]:
    """Create a new ZIP archive from MP4 files without overwriting an existing file."""
    if destination.suffix.lower() != ".zip":
        raise ValueError("File xuất phải có đuôi .zip.")
    if not destination.parent.is_dir():
        raise ValueError("Thư mục đích không tồn tại.")
    archived = skipped = 0
    used_names: set[str] = set()
    try:
        with zipfile.ZipFile(destination, mode="x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                if not path.is_file():
                    skipped += 1
                    continue
                name = path.name
                index = 2
                while name.casefold() in used_names:
                    name = f"{path.stem} ({index}){path.suffix}"
                    index += 1
                try:
                    archive.write(path, arcname=name)
                    used_names.add(name.casefold())
                    archived += 1
                except OSError:
                    skipped += 1
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return archived, skipped


def delete_videos(database: VideoDatabase, records: list[dict],
                  allowed_roots: tuple[Path, ...]) -> tuple[list[int], list[int]]:
    """Permanently delete exact selected files, then free their Pixabay IDs."""
    roots = tuple(root.resolve() for root in allowed_roots)
    removed: list[int] = []
    failed: list[int] = []
    for record in records:
        path = Path(record["file_path"])
        assets = (path.with_suffix(".thumb.jpg"), path.with_suffix(".jpg"), path)
        try:
            if path.suffix.lower() != ".mp4" or not all(
                any(asset.resolve().is_relative_to(root) for root in roots) for asset in assets
            ):
                raise ValueError("File nằm ngoài Library hoặc không phải MP4.")
            for asset in assets:
                asset.unlink(missing_ok=True)
            if database.delete_video(record["id"]):
                removed.append(record["id"])
            else:
                failed.append(record["id"])
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            failed.append(record["id"])
    return removed, failed
