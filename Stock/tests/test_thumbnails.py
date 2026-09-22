from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.thumbnails import ensure_thumbnail, is_jpeg, thumbnail_bytes


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent / '.test-work'
        self.folder = test_root / 'thumbnail-tests' / uuid.uuid4().hex
        self.folder.mkdir(parents=True)
        self.assertTrue(self.folder.resolve().is_relative_to(test_root.resolve()))
        self.addCleanup(shutil.rmtree, self.folder)

    def test_missing_poster_is_extracted_from_mp4_and_cached(self):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg not installed')
        video = self.folder / '123_1280x720.mp4'
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'color=c=blue:s=320x180:d=1', '-an', '-c:v', 'mpeg4',
                        '-y', str(video)], check=True, capture_output=True)
        self.assertIsNone(ensure_thumbnail(self.folder / 'missing.mp4'))
        thumb = ensure_thumbnail(video)
        self.assertEqual(thumb, video.with_suffix('.thumb.jpg'))
        self.assertTrue(is_jpeg(video.with_suffix('.jpg')))
        self.assertTrue(is_jpeg(thumb))
        self.assertGreater(len(thumbnail_bytes(video)), 100)
        with patch('services.thumbnails._frame') as render:
            self.assertEqual(ensure_thumbnail(video), thumb)
            render.assert_not_called()

    def test_without_ffmpeg_uses_saved_poster(self):
        video = self.folder / '123_1280x720.mp4'
        video.write_bytes(b'video')
        poster = video.with_suffix('.jpg')
        poster.write_bytes(b'\xff\xd8\xffposter')
        with patch('services.thumbnails.shutil.which', return_value=None):
            self.assertEqual(ensure_thumbnail(video), poster)
            self.assertEqual(thumbnail_bytes(video), b'\xff\xd8\xffposter')


if __name__ == '__main__':
    unittest.main()
