from __future__ import annotations

import os
from pathlib import Path
import sys
import shutil
import uuid
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_config


class AppConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ".test-work" / f"config-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)

    def test_frozen_app_uses_persistent_user_data(self):
        with patch.object(sys, "frozen", True, create=True), patch.dict(
            os.environ, {"LOCALAPPDATA": "C:/Users/Test/AppData/Local"}, clear=True
        ):
            self.assertEqual(app_config.data_root(), Path("C:/Users/Test/AppData/Local/StockDownloader"))

    def test_source_mode_retains_existing_library_location(self):
        with patch.object(sys, "frozen", False, create=True), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app_config.data_root(), app_config.RESOURCE_ROOT)

    def test_data_override_for_isolated_install_checks(self):
        with patch.dict(os.environ, {"STOCK_DATA_DIR": "./isolated-stock"}):
            self.assertEqual(app_config.data_root(), Path("isolated-stock").resolve())

    def test_save_key_preserves_other_settings_and_replaces_previous_key(self):
        with patch.dict(os.environ, {}, clear=True):
            root = self.root
            (root / ".env").write_text("# settings\nOTHER=value\nPIXABAY_API_KEY=old\n", encoding="utf-8")
            app_config.save_api_key(" new-test-key ", root)
            self.assertEqual((root / ".env").read_text(encoding="utf-8"),
                             "# settings\nOTHER=value\nPIXABAY_API_KEY=new-test-key\n")
            self.assertEqual(os.environ["PIXABAY_API_KEY"], "new-test-key")
            self.assertFalse((root / ".env.tmp").exists())

    def test_rejects_empty_key_or_multiline_config_injection(self):
        for value in ("", " ", "key\nOTHER=bad", 'key"bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app_config.save_api_key(value, self.root)
        self.assertFalse((self.root / ".env").exists())


if __name__ == "__main__":
    unittest.main()
