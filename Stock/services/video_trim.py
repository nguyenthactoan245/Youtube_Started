"""Trim a centered segment from a source video using FFmpeg."""
from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from threading import Lock


_PROCESS_START_LOCK = Lock()


def _creation_flags() -> int:
    return (getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_DEFAULT_ERROR_MODE", 0))


def _run_process(args: list[str], timeout: float, *, text: bool = False) -> subprocess.CompletedProcess:
    """Start child processes one at a time, then let them run concurrently.

    Serializing only ``Popen`` avoids Windows handle/signal races in a multi-threaded
    Flet process. The lock is released before waiting, so FFmpeg jobs still overlap.
    """
    with _PROCESS_START_LOCK:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=text, creationflags=_creation_flags(), close_fds=True,
        )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, args, stdout, stderr)
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _video_duration(source: Path, ffprobe: str) -> float:
    probe = _run_process(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
        timeout=30, text=True)
    return float(json.loads(probe.stdout)["format"]["duration"])


@lru_cache(maxsize=4)
def _nvenc_available(ffmpeg: str) -> bool:
    """Return whether this FFmpeg build advertises NVIDIA H.264 encoding."""
    try:
        result = _run_process([ffmpeg, "-hide_banner", "-encoders"], timeout=30, text=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return "h264_nvenc" in (result.stdout or "")


def _write_segment(source: Path, destination: Path, start: float, duration: float,
                   ffmpeg: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".part" + destination.suffix)
    input_args = ["-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.1f}",
                  "-map", "0:v:0?", "-map", "0:a?"]
    output_args = ["-c:a", "aac", "-movflags", "+faststart", str(temporary)]
    cpu_args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                *input_args, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                *output_args]
    try:
        if _nvenc_available(ffmpeg):
            gpu_args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                        *input_args, "-c:v", "h264_nvenc", "-preset", "p1",
                        "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0",
                        *output_args]
            try:
                _run_process(gpu_args, timeout=600)
            except subprocess.CalledProcessError:
                temporary.unlink(missing_ok=True)
                _run_process(cpu_args, timeout=600)
        else:
            _run_process(cpu_args, timeout=600)
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
                      maximum: float, rng: random.Random | None = None, *,
                      start_at: float | None = None) -> tuple[Path, float, float]:
    """Write one segment and return its path, start time and duration."""
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
    fixed_start = None if start_at is None else round(float(start_at), 3)
    if fixed_start is not None and fixed_start < 0:
        raise ValueError("Start time must be zero or greater.")
    available = total if fixed_start is None else total - fixed_start
    if available + 1e-6 < minimum:
        raise ValueError(f"Video is {total:.1f}s; minimum segment is {minimum:.1f}s.")
    upper = min(maximum, available)
    chooser = rng or random
    minimum_tick = int(round(minimum * 10))
    maximum_tick = int(math.floor((upper + 1e-9) * 10))
    duration = chooser.randint(minimum_tick, maximum_tick) / 10
    start = (fixed_start if fixed_start is not None else
             round(chooser.uniform(0.0, max(0.0, total - duration)), 3))
    return _write_segment(source, destination, start, duration, ffmpeg), start, duration
