from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from models import DownloadEvent, Video
from services.database import VideoDatabase
from services.downloader import footage_filename, safe_folder_name
from services.pexels_verified import (
    download_matching_pexels_videos,
    orientation_matches,
    probe_video_dimensions,
    quality_matches,
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

    def test_quality_matches_selected_resolution_floor(self):
        self.assertTrue(quality_matches(3840, 2160, "4K"))
        self.assertFalse(quality_matches(2560, 1440, "4K"))
        self.assertTrue(quality_matches(2560, 1440, "2K"))
        self.assertTrue(quality_matches(640, 360, "All"))

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

    def test_rejects_known_wrong_quality_before_download(self):
        events: list[DownloadEvent] = []
        database = MagicMock()
        with patch("services.pexels_verified.find_ffprobe", return_value="ffprobe"), \
             patch("services.pexels_verified.download_many") as download:
            accepted, attempted, rejected, skipped = download_matching_pexels_videos(
                [video(9)], 1, Path("Library"), "test", "landscape", Event(),
                database, "owner", events.append, quality="4K")

        self.assertEqual((accepted, attempted, rejected, skipped), ([], 0, 1, 0))
        database.reserve.assert_not_called()
        download.assert_not_called()
        self.assertIn("trước khi tải", events[0].message)

    def test_downloads_only_the_remaining_requested_amount_from_a_larger_batch(self):
        candidates = [video(video_id) for video_id in range(1, 5)]
        database = MagicMock()
        database.reserve.return_value = True

        with patch("services.pexels_verified.find_ffprobe", return_value="ffprobe"), \
             patch("services.pexels_verified.download_many") as download, \
             patch("services.pexels_verified.ThreadPoolExecutor") as pool, \
             patch("services.pexels_verified.probe_video_dimensions",
                   return_value=(1920, 1080)), \
             patch.object(Path, "is_file", return_value=True):
            accepted, attempted, rejected, skipped = download_matching_pexels_videos(
                candidates, 2, Path("Library"), "test", "landscape", Event(),
                database, "owner", lambda _event: None, workers=1)

        self.assertEqual([item.id for item in accepted], [1, 2])
        self.assertEqual((attempted, rejected, skipped), (2, 0, 0))
        self.assertEqual(database.reserve.call_count, 2)
        self.assertEqual(download.call_count, 2)
        pool.assert_not_called()

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
