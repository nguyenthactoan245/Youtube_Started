"""Generate small local Library thumbnails without blocking the Flet event loop."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def is_jpeg(path: Path) -> bool:
    try:
        with path.open("rb") as image:
            return image.read(3) == b"\xff\xd8\xff"
    except OSError:
        return False


def _frame(source: Path, target: Path, ffmpeg: str, video: bool, width: int) -> bool:
    temporary = target.with_suffix(target.suffix + ".part")
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if video:
        args += ["-ss", "0.25"]
    args += ["-i", str(source), "-an", "-frames:v", "1", "-vf", f"scale={width}:-2",
             "-q:v", "4", "-f", "mjpeg", str(temporary)]
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0 and is_jpeg(temporary):
            temporary.replace(target)
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return False


def ensure_thumbnail(video_path: Path) -> Path | None:
    """Use a saved poster or extract a frame; preserve any pre-existing JPG."""
    if not video_path.is_file():
        return None
    poster = video_path.with_suffix(".jpg")
    thumb = video_path.with_suffix(".thumb.jpg")
    poster_valid = is_jpeg(poster)
    if is_jpeg(thumb):
        try:
            if thumb.stat().st_mtime >= max(video_path.stat().st_mtime,
                                            poster.stat().st_mtime if poster_valid else 0):
                return thumb
        except OSError:
            pass
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return poster if poster_valid else None
    if not poster_valid and not poster.exists():
        poster_valid = (_frame(video_path, poster, ffmpeg, video=True, width=1280)
                        or _frame(video_path, poster, ffmpeg, video=False, width=1280))
    if poster_valid and _frame(poster, thumb, ffmpeg, video=False, width=480):
        return thumb
    if (_frame(video_path, thumb, ffmpeg, video=True, width=480)
            or _frame(video_path, thumb, ffmpeg, video=False, width=480)):
        return thumb
    return poster if poster_valid else None


def thumbnail_bytes(video_path: Path) -> bytes | None:
    path = ensure_thumbnail(video_path)
    try:
        return path.read_bytes() if path else None
    except OSError:
        return None
