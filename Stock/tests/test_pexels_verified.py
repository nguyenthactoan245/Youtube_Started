from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from models import DownloadEvent, Video
from services.database import VideoDatabase
from services.downloader import footage_filename, safe_folder_name
from services.pexels_verified import (
    download_matching_pexels_videos,
    orientation_matches,
    probe_video_dimensions,
)


def video(video_id: int) -> Video:
    return Video(video_id, "landscape", 8, "author", "https://pexels.test/video",
                 "https://pexels.test/file.mp4", "", 1920, 1080, 100, "pexels")


class PexelsVerifiedTests(unittest.TestCase):
    def test_orientation_matches(self):
        self.assertTrue(orientation_matches(1920, 1080, "landscape"))
        self.assertFalse(orientation_matches(1080, 1920, "landscape"))
        self.assertTrue(orientation_matches(1080, 1920, "portrait"))
        self.assertFalse(orientation_matches(1080, 1080, "portrait"))
        self.assertTrue(orientation_matches(1080, 1080, "all"))

    def test_probe_reads_dimensions_and_applies_rotation(self):
        result = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=0,
            stdout=json.dumps({"streams": [{"width": 1920, "height": 1080,
                                            "side_data_list": [{"rotation": -90}]}]}),
            stderr="",
        )
        with patch("services.pexels_verified.subprocess.run", return_value=result) as run:
            self.assertEqual(probe_video_dimensions(Path("clip.mp4"), "ffprobe"), (1080, 1920))
        self.assertEqual(run.call_args.args[0][0], "ffprobe")

    def test_discards_wrong_orientation_then_keeps_verified_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = VideoDatabase(root / "stock.db")
            candidates = [video(1), video(2)]
            events: list[DownloadEvent] = []

            def fake_download(videos, library_root, keyword, cancel, report, database, owner):
                destination = library_root / safe_folder_name(keyword)
                destination.mkdir(parents=True, exist_ok=True)
                for item in videos:
                    path = destination / footage_filename(item)
                    path.write_bytes(b"video-data")
                    database.record(item.id, owner, "completed", source=item.source)
                    report(DownloadEvent(item.id, "done", 1, "saved"))
                return destination

            with db.batch() as owner:
                with patch("services.pexels_verified.find_ffprobe", return_value="ffprobe"), \
                     patch("services.pexels_verified.download_many", side_effect=fake_download), \
                     patch("services.pexels_verified.probe_video_dimensions",
                           side_effect=[(1080, 1920), (1920, 1080)]):
                    accepted, attempted, rejected, skipped = download_matching_pexels_videos(
                        candidates, 1, root / "Library", "test", "landscape", Event(),
                        db, owner, events.append)

            folder = root / "Library" / "test"
            self.assertEqual((attempted, rejected, skipped), (2, 1, 0))
            self.assertEqual([item.id for item in accepted], [2])
            self.assertEqual((accepted[0].width, accepted[0].height), (1920, 1080))
            self.assertFalse((folder / "pexels_1_1920x1080.mp4").exists())
            self.assertTrue((folder / "pexels_2_1920x1080.mp4").exists())
            records = db.library()
            self.assertEqual([row["video_id"] for row in records], [2])
            self.assertEqual((records[0]["details"]["width"], records[0]["details"]["height"]),
                             (1920, 1080))
            self.assertIn("rejected", [event.state for event in events])
            self.assertIn("verified", [event.state for event in events])


if __name__ == "__main__":
    unittest.main()
