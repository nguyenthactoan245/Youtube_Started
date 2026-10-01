from __future__ import annotations

import sys
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pexels_browser import PexelsBrowserSearchError, search_videos
from services.pexels_link import PexelsLinkError


class PexelsBrowserSearchTests(unittest.TestCase):
    def test_reports_download_resolution_error_when_every_video_fails(self):
        with patch("services.pexels_browser.search_video_links", return_value=[
            "https://www.pexels.com/video/sample-1/",
            "https://www.pexels.com/video/sample-2/",
        ]), patch("services.pexels_browser.resolve_pexels_video",
                  side_effect=PexelsLinkError("MP4 metadata unavailable")):
            with self.assertRaisesRegex(PexelsBrowserSearchError, "MP4 metadata unavailable"):
                search_videos("Moscow", 1, Event())


if __name__ == "__main__":
    unittest.main()
