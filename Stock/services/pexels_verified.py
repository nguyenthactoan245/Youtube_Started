"""Download Pexels candidates, probe completed files, and keep matching orientation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Callable

from models import DownloadEvent, Video
from services.database import VideoDatabase
from services.downloader import download_many, footage_filename, safe_folder_name


def find_ffprobe() -> str:
    """Find ffprobe from PATH, a source checkout, or the frozen app bundle."""
    executable = shutil.which("ffprobe")
    if executable:
        return executable
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    roots = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.extend((Path(bundle_root) / "ffprobe" / name,
                      Path(bundle_root) / name))
    roots.append(Path(__file__).resolve().parents[1] / "ffprobe" / name)
    for candidate in roots:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("ffprobe was not found. Install FFmpeg or rebuild the app with ffprobe bundled.")


def probe_video_dimensions(path: Path, ffprobe: str | None = None) -> tuple[int, int]:
    """Read display dimensions from a completed video using ffprobe."""
    executable = ffprobe or find_ffprobe()
    result = subprocess.run(
        [executable, "-v", "error", "-select_streams", "v:0", "-show_streams",
         "-of", "json", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError("ffprobe did not find a video stream.")
    stream = streams[0]
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    rotation = 0
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            rotation = int(round(float(side_data["rotation"]))) % 360
            break
    if rotation in (90, 270):
        width, height = height, width
    if width <= 0 or height <= 0:
        raise ValueError("ffprobe returned invalid video dimensions.")
    return width, height


def orientation_matches(width: int, height: int, orientation: str) -> bool:
    if orientation == "all":
        return True
    if orientation == "landscape":
        return width > height
    if orientation == "portrait":
        return height > width
    return False


def quality_matches(width: int, height: int, quality: str) -> bool:
    """Return whether a rendition reaches the selected resolution tier."""
    minimum = {"HD": 1280, "2K": 2560, "4K": 3840}.get(quality)
    return minimum is None or max(width, height) >= minimum


def download_matching_pexels_videos(
    videos: list[Video], amount: int, root: Path, keyword: str, orientation: str,
    cancel: Event, database: VideoDatabase, owner: str,
    report: Callable[[DownloadEvent], None], workers: int = 3,
    quality: str = "All",
) -> tuple[list[Video], int, int, int]:
    """Download candidates and keep only files matching orientation and quality."""
    ffprobe = find_ffprobe()
    destination = Path(root) / safe_folder_name(keyword)
    accepted: list[Video] = []
    attempted = rejected = skipped = 0

    pending: list[tuple[Video, Path]] = []
    for candidate in videos:
        if cancel.is_set() or len(pending) >= amount:
            break
        if (candidate.width > 0 and candidate.height > 0
                and (not orientation_matches(candidate.width, candidate.height, orientation)
                     or not quality_matches(candidate.width, candidate.height, quality))):
            rejected += 1
            report(DownloadEvent(
                candidate.id, "rejected", 0,
                f"Bỏ qua trước khi tải: không đúng {orientation}/{quality} "
                f"({candidate.width}×{candidate.height}).",
            ))
            continue
        target = destination / footage_filename(candidate)
        if not database.reserve(candidate, keyword, target, owner):
            skipped += 1
            continue
        attempted += 1
        pending.append((candidate, target))

    effective_workers = min(max(1, workers), max(1, len(pending)))
    if effective_workers == 1:
        for candidate, _target in pending:
            download_many([candidate], Path(root), keyword, cancel, report, database, owner)
    else:
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {executor.submit(download_many, [candidate], Path(root), keyword,
                                         cancel, report, database, owner): (candidate, target)
                       for candidate, target in pending}
            for future in futures:
                future.result()

    for candidate, target in pending:
        if cancel.is_set() or len(accepted) >= amount:
            break
        if cancel.is_set() or not target.is_file():
            continue

        try:
            width, height = probe_video_dimensions(target, ffprobe)
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            target.unlink(missing_ok=True)
            target.with_suffix(".jpg").unlink(missing_ok=True)
            target.with_suffix(".thumb.jpg").unlink(missing_ok=True)
            database.discard_completed_download(candidate.source, candidate.id)
            rejected += 1
            report(DownloadEvent(candidate.id, "rejected", 1, f"Không đọc được video: {exc}"))
            continue

        if (not orientation_matches(width, height, orientation)
                or not quality_matches(width, height, quality)):
            target.unlink(missing_ok=True)
            target.with_suffix(".jpg").unlink(missing_ok=True)
            target.with_suffix(".thumb.jpg").unlink(missing_ok=True)
            database.discard_completed_download(candidate.source, candidate.id)
            rejected += 1
            report(DownloadEvent(candidate.id, "rejected", 1,
                                 f"Đã bỏ video không đúng {orientation}/{quality} "
                                 f"({width}×{height})."))
            continue

        verified = replace(candidate, width=width, height=height)
        verified_target = destination / footage_filename(verified)
        if verified_target != target:
            if verified_target.exists():
                target.unlink(missing_ok=True)
                database.discard_completed_download(candidate.source, candidate.id)
                rejected += 1
                report(DownloadEvent(candidate.id, "rejected", 1,
                                     "Tên file đã tồn tại; video này đã được bỏ qua."))
                continue
            target.replace(verified_target)
            for suffix in (".jpg", ".thumb.jpg"):
                old_sidecar = target.with_suffix(suffix)
                if old_sidecar.exists():
                    old_sidecar.replace(verified_target.with_suffix(suffix))
        database.update_completed_video_metadata(verified, verified_target)
        accepted.append(verified)
        report(DownloadEvent(candidate.id, "verified", 1,
                             f"Đã xác thực video {orientation}/{quality} "
                             f"({width}×{height})."))

    return accepted, attempted, rejected, skipped
