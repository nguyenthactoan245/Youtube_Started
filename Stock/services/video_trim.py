"""Trim a centered segment from a source video using FFmpeg."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def trim_video(source: Path, destination: Path, duration: float) -> Path:
    """Write a segment centered in source; round requested duration to tenths."""
    source, destination = Path(source), Path(destination)
    duration = round(float(duration), 1)
    if duration <= 0:
        raise ValueError("Duration must be greater than zero.")
    if not source.is_file():
        raise FileNotFoundError("Source video does not exist.")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe are required to trim video.")
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
    total = float(json.loads(probe.stdout)["format"]["duration"])
    if total + 1e-6 < duration:
        raise ValueError(f"Video is {total:.1f}s; requested segment is {duration:.1f}s.")
    start = max(0.0, (total - duration) / 2.0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".part" + destination.suffix)
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss",
            f"{start:.3f}", "-i", str(source), "-t", f"{duration:.1f}", "-map", "0:v:0?",
            "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-movflags", "+faststart", str(temporary)]
    try:
        subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=600,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
        if not temporary.is_file() or not temporary.stat().st_size:
            raise OSError("FFmpeg produced an empty output.")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination
