from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.video_trim import trim_random_video, trim_video


class VideoTrimTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / ".test-work" / "video-trim"
        self.folder = root / uuid.uuid4().hex
        self.folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)

    def test_trim_uses_rounded_duration_and_centered_start(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg and ffprobe are required")
        source, output = self.folder / "source.mp4", self.folder / "trim.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc=size=160x90:rate=25:duration=8", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-y", str(source)], check=True, capture_output=True)
        from services import video_trim
        with patch("services.video_trim.shutil.which", side_effect=[ffmpeg, ffprobe]), \
             patch("services.video_trim._run_process", wraps=video_trim._run_process) as run:
            trim_video(source, output, 2.34)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-ss") + 1], "2.850")
        self.assertEqual(args[args.index("-t") + 1], "2.3")
        probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "json", str(output)], capture_output=True, text=True, check=True)
        self.assertAlmostEqual(float(json.loads(probe.stdout)["format"]["duration"]), 2.3, delta=0.08)

    def test_rejects_duration_longer_than_source(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg and ffprobe are required")
        source = self.folder / "short.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "color=c=blue:s=160x90:d=1", "-c:v", "libx264", "-y",
                        str(source)], check=True, capture_output=True)
        with patch("services.video_trim.shutil.which", side_effect=[ffmpeg, ffprobe]):
            with self.assertRaisesRegex(ValueError, "requested segment"):
                trim_video(source, self.folder / "too-long.mp4", 2.0)

    def test_random_trim_writes_one_clip_inside_requested_range(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg and ffprobe are required")
        source, output = self.folder / "random-source.mp4", self.folder / "random-trim.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc=size=160x90:rate=25:duration=8", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-y", str(source)], check=True, capture_output=True)
        with patch("services.video_trim.shutil.which", side_effect=[ffmpeg, ffprobe]):
            result, start, duration = trim_random_video(
                source, output, 2.0, 3.0, random.Random(7))
        self.assertEqual(result, output)
        self.assertTrue(output.is_file())
        self.assertGreaterEqual(duration, 2.0)
        self.assertLessEqual(duration, 3.0)
        self.assertEqual(duration * 10, round(duration * 10))
        self.assertGreaterEqual(start, 0.0)
        self.assertLessEqual(start + duration, 8.05)

    def test_two_ffmpeg_trims_can_run_concurrently(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg and ffprobe are required")
        source = self.folder / "parallel-source.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc=size=160x90:rate=25:duration=4", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-y", str(source)], check=True,
                       capture_output=True)
        outputs = [self.folder / f"parallel-{index}.mp4" for index in range(2)]
        with patch("services.video_trim.shutil.which",
                   side_effect=lambda name: ffmpeg if name == "ffmpeg" else ffprobe):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda output: trim_video(source, output, 1.5), outputs))

        self.assertEqual(results, outputs)
        self.assertTrue(all(output.is_file() and output.stat().st_size for output in outputs))

    def test_nvenc_failure_retries_with_cpu_encoder(self):
        from services import video_trim
        source = self.folder / "source.mp4"
        destination = self.folder / "fallback.mp4"
        source.write_bytes(b"source")
        commands = []

        def run(args, timeout, text=False):
            commands.append(args)
            if "h264_nvenc" in args:
                Path(args[-1]).write_bytes(b"partial")
                raise subprocess.CalledProcessError(1, args)
            Path(args[-1]).write_bytes(b"cpu-output")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        with patch.object(video_trim, "_nvenc_available", return_value=True), \
             patch.object(video_trim, "_run_process", side_effect=run):
            result = video_trim._write_segment(source, destination, 1.0, 2.0, "ffmpeg")

        self.assertEqual(result, destination)
        self.assertIn("h264_nvenc", commands[0])
        self.assertIn("libx264", commands[1])
        self.assertEqual(destination.read_bytes(), b"cpu-output")

    def test_random_trim_can_use_a_fixed_start_time(self):
        source = self.folder / "fixed-source.mp4"
        destination = self.folder / "fixed-output.mp4"
        source.write_bytes(b"source")
        with patch("services.video_trim.shutil.which", side_effect=["ffmpeg", "ffprobe"]), \
             patch("services.video_trim._video_duration", return_value=10.0), \
             patch("services.video_trim._write_segment", return_value=destination) as write:
            result, start, duration = trim_random_video(
                source, destination, 2.0, 3.0, random.Random(7), start_at=2.0)

        self.assertEqual((result, start), (destination, 2.0))
        self.assertGreaterEqual(duration, 2.0)
        self.assertLessEqual(duration, 3.0)
        self.assertEqual(write.call_args.args[2], 2.0)


if __name__ == "__main__":
    unittest.main()
