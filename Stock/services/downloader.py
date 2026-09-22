from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.request import Request, urlopen

from models import DownloadEvent, Video
from services.database import VideoDatabase

ProgressCallback = Callable[[DownloadEvent], None]


def safe_folder_name(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    return cleaned[:80] or "untitled"


def _download_one(video: Video, destination: Path, cancel: Event, report: ProgressCallback,
                  database: VideoDatabase | None = None, owner: str | None = None) -> None:
    target = destination / f"{video.id}_{video.width}x{video.height}.mp4"
    temporary = target.with_suffix(".mp4.part")
    if target.exists() and target.stat().st_size > 0:
        _download_thumbnail(video, target)
        if database and owner:
            database.record(video.id, owner, "completed")
        report(DownloadEvent(video.id, "done", 1, "Đã có sẵn"))
        return

    if cancel.is_set():
        if database and owner:
            database.record(video.id, owner, "cancelled")
        report(DownloadEvent(video.id, "cancelled", 0, "Đã hủy"))
        return

    report(DownloadEvent(video.id, "downloading", 0, "Đang tải"))
    try:
        if database and owner:
            database.record(video.id, owner, "downloading")
        request = Request(video.url, headers={"User-Agent": "StockDownloader/1.0"})
        with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            total = int(response.headers.get("Content-Length", video.size) or 0)
            received = 0
            while chunk := response.read(256 * 1024):
                if cancel.is_set():
                    raise InterruptedError
                output.write(chunk)
                received += len(chunk)
                report(DownloadEvent(video.id, "downloading", received / total if total else 0, "Đang tải"))
        if cancel.is_set():
            raise InterruptedError
        if database and owner and not database.owns(video.id, owner):
            raise InterruptedError
        temporary.replace(target)
        _download_thumbnail(video, target)
        if database and owner:
            database.record(video.id, owner, "completed")
        report(DownloadEvent(video.id, "done", 1, f"Đã lưu: {target.name}"))
    except InterruptedError:
        temporary.unlink(missing_ok=True)
        if database and owner:
            database.record(video.id, owner, "cancelled")
        report(DownloadEvent(video.id, "cancelled", 0, "Đã hủy"))
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if database and owner:
            database.record(video.id, owner, "failed", str(exc))
        report(DownloadEvent(video.id, "error", 0, f"Lỗi tải: {exc}"))


def _download_thumbnail(video: Video, video_path: Path) -> None:
    """Store Pixabay's poster beside the MP4; a missing poster never fails a video download."""
    if not video.thumbnail:
        return
    poster = video_path.with_suffix(".jpg")
    if poster.exists() and poster.stat().st_size > 0:
        return
    temporary = poster.with_suffix(".jpg.part")
    try:
        request = Request(video.thumbnail, headers={"User-Agent": "StockDownloader/1.0"})
        with urlopen(request, timeout=30) as response, temporary.open("wb") as output:
            while chunk := response.read(256 * 1024):
                output.write(chunk)
        temporary.replace(poster)
    except Exception:
        temporary.unlink(missing_ok=True)


def download_many(videos: list[Video], root: Path, keyword: str, cancel: Event, report: ProgressCallback,
                  database: VideoDatabase | None = None, owner: str | None = None) -> Path:
    """Download at most three videos concurrently; completed files are never overwritten."""
    destination = root / safe_folder_name(keyword)
    destination.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(videos)))) as executor:
        futures = [executor.submit(_download_one, video, destination, cancel, report, database, owner) for video in videos]
        for future in as_completed(futures):
            future.result()
    return destination
