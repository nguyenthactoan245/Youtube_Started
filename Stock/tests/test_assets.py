from __future__ import annotations

import shutil
import sys
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import Video
from services.assets import delete_videos, export_videos, export_videos_zip
from services.database import VideoDatabase


class AssetTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / ".test-work"
        base.mkdir(exist_ok=True)
        self.root = base / f"assets-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.library = self.root / "library"
        self.library.mkdir()
        self.db = VideoDatabase(self.root / "data" / "stock.db")

    def test_export_copies_mp4_without_overwriting_existing_file(self):
        source = self.library / "1_1280x720.mp4"
        source.write_bytes(b"new video")
        target = self.root / "export"
        target.mkdir()
        existing = target / source.name
        existing.write_bytes(b"keep me")
        self.assertEqual(export_videos([source, self.library / "missing.mp4"], target), (0, 2))
        self.assertEqual(existing.read_bytes(), b"keep me")
        existing.unlink()
        self.assertEqual(export_videos([source], target), (1, 0))
        self.assertEqual(existing.read_bytes(), b"new video")
        self.assertEqual(source.read_bytes(), b"new video")

    def test_export_multiple_videos_as_zip_without_overwriting(self):
        first = self.library / "first.mp4"
        second = self.library / "second.mp4"
        first.write_bytes(b"first video")
        second.write_bytes(b"second video")
        archive = self.root / "export.zip"
        self.assertEqual(export_videos_zip([first, second, self.library / "missing.mp4"], archive), (2, 1))
        with zipfile.ZipFile(archive) as contents:
            self.assertEqual(set(contents.namelist()), {"first.mp4", "second.mp4"})
            self.assertEqual(contents.read("first.mp4"), b"first video")
            self.assertEqual(contents.read("second.mp4"), b"second video")
        with self.assertRaises(FileExistsError):
            export_videos_zip([first, second], archive)
        self.assertEqual(first.read_bytes(), b"first video")

    def test_delete_removes_files_and_database_id_can_be_downloaded_again(self):
        source = self.library / "42_1280x720.mp4"
        poster = source.with_suffix(".jpg")
        thumbnail = source.with_suffix(".thumb.jpg")
        source.write_bytes(b"video")
        poster.write_bytes(b"poster")
        thumbnail.write_bytes(b"thumbnail")
        self.assertEqual(self.db.import_existing(self.library, self.root / "downloads"), 1)
        record = self.db.library()[0]
        removed, failed = delete_videos(self.db, [record], (self.library,))
        self.assertEqual((removed, failed), ([record["id"]], []))
        self.assertFalse(source.exists())
        self.assertFalse(poster.exists())
        self.assertFalse(thumbnail.exists())
        self.assertEqual(self.db.library(), [])
        with self.db.connect() as connection:
            row = connection.execute("SELECT id FROM videos WHERE id=?", (record["id"],)).fetchone()
        self.assertIsNone(row)
        video = Video(42, "clip 42", 5, "author", "https://pixabay.com/videos/id-42/",
                      "https://example.com/42.mp4", "", 1280, 720, 5)
        with self.db.batch() as owner:
            self.assertTrue(self.db.reserve(video, "Moscow", source, owner))

    def test_delete_refuses_file_outside_allowed_library(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"keep")
        removed, failed = delete_videos(self.db, [{"id": 99, "file_path": str(outside)}],
                                        (self.library,))
        self.assertEqual((removed, failed), ([], [99]))
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_failed_file_delete_keeps_database_record(self):
        source = self.library / "43_1280x720.mp4"
        source.write_bytes(b"video")
        self.db.import_existing(self.library, self.root / "downloads")
        record = self.db.library()[0]
        with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            removed, failed = delete_videos(self.db, [record], (self.library,))
        self.assertEqual((removed, failed), ([], [record["id"]]))
        self.assertTrue(source.is_file())
        self.assertEqual(len(self.db.library()), 1)


if __name__ == "__main__":
    unittest.main()
