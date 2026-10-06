"""Trim a centered segment from a source video using FFmpeg."""
from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
from pathlib import Path


def _video_duration(source: Path, ffprobe: str) -> float:
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
    return float(json.loads(probe.stdout)["format"]["duration"])


def _write_segment(source: Path, destination: Path, start: float, duration: float,
                   ffmpeg: str) -> Path:
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
    total = _video_duration(source, ffprobe)
    if total + 1e-6 < duration:
        raise ValueError(f"Video is {total:.1f}s; requested segment is {duration:.1f}s.")
    start = max(0.0, (total - duration) / 2.0)
    return _write_segment(source, destination, start, duration, ffmpeg)


def trim_random_video(source: Path, destination: Path, minimum: float,
                      maximum: float, rng: random.Random | None = None) -> tuple[Path, float, float]:
    """Write one random segment and return its path, start time and duration."""
    source, destination = Path(source), Path(destination)
    minimum, maximum = round(float(minimum), 1), round(float(maximum), 1)
    if minimum <= 0 or maximum < minimum:
        raise ValueError("Duration range is invalid.")
    if not source.is_file():
        raise FileNotFoundError("Source video does not exist.")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe are required to trim video.")
    total = _video_duration(source, ffprobe)
    if total + 1e-6 < minimum:
        raise ValueError(f"Video is {total:.1f}s; minimum segment is {minimum:.1f}s.")
    upper = min(maximum, total)
    chooser = rng or random
    minimum_tick = int(round(minimum * 10))
    maximum_tick = int(math.floor((upper + 1e-9) * 10))
    duration = chooser.randint(minimum_tick, maximum_tick) / 10
    start = round(chooser.uniform(0.0, max(0.0, total - duration)), 3)
    return _write_segment(source, destination, start, duration, ffmpeg), start, duration
