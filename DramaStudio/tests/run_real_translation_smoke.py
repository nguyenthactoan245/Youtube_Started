"""Optional end-to-end smoke test using the downloaded MADLAD model and FFmpeg."""

import os
import subprocess
import sys
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from phimora_downloader import detect_gpu_encoder, get_video_duration  # noqa: E402
from subtitle_translator import TranslationManager  # noqa: E402


def main() -> int:
    portable_dir = PROJECT_DIR / "dist"
    work_dir = PROJECT_DIR / "tests" / ".tmp" / "real-smoke"
    work_dir.mkdir(parents=True, exist_ok=True)
    video = work_dir / "sample.mp4"
    source_srt = work_dir / "sample.vi.srt"

    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x202020:s=1280x720:d=7",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(video),
        ],
        check=True,
    )
    source_srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nXin chào, rất vui được gặp bạn.\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\nTôi sẽ không bao giờ rời xa em.\n",
        encoding="utf-8",
    )

    manager = TranslationManager(str(portable_dir), detect_gpu_encoder, get_video_duration)
    ok, error = manager.start_translation(
        str(video), str(source_srt), ["es"], burn_video=True
    )
    if not ok:
        raise RuntimeError(error)
    deadline = time.time() + 180
    while manager.snapshot()["translationRunning"] and time.time() < deadline:
        time.sleep(0.1)
    snapshot = manager.snapshot()
    if snapshot["translationRunning"]:
        raise TimeoutError("Translation smoke test timed out.")
    if not snapshot["translationResults"]:
        raise RuntimeError(snapshot["translationStatus"])
    result = snapshot["translationResults"][0]
    for key in ("out_srt", "out_ass", "out_mp4"):
        path = result[key]
        if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
            raise RuntimeError(f"Missing smoke-test output: {key}")
    print(snapshot["translationStatus"])
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
