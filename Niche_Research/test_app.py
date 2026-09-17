import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawler import CrawlJob, ROOT, export_metadata, normalize_channel
from main import ResearchApp, posted


class ResearchTests(unittest.TestCase):
    def test_channel_validation(self):
        for value in ('InfinitePlanet4K', '@InfinitePlanet4K', 'https://www.youtube.com/@InfinitePlanet4K/videos'):
            self.assertEqual(normalize_channel(value)[0], 'https://www.youtube.com/@InfinitePlanet4K')
        for value in ('', 'https://evil.example/@name', 'https://youtube.com/watch?v=123', '../..', '@foo & calc.exe'):
            with self.assertRaises(ValueError):
                normalize_channel(value)

    def test_export_preserves_missing_values_and_date_precision(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            run = Path(folder)
            (run / 'metadata').mkdir()
            (run / 'thumbnails').mkdir()
            data = {'id': 'abcdefghijk', 'title': 'Việt Nam, "Thiên nhiên"', 'upload_date': '20260102', 'view_count': 0}
            (run / 'metadata' / 'a.info.json').write_text(json.dumps(data), encoding='utf-8')
            (run / 'metadata' / 'broken.info.json').write_text('{', encoding='utf-8')
            rows, errors = export_metadata(run)
            self.assertEqual(rows[0]['created_time'], '2026-01-02')
            self.assertEqual(rows[0]['created_time_precision'], 'date_only')
            self.assertEqual(rows[0]['view_count'], 0)
            self.assertIsNone(rows[0]['thumbnail_file'])
            self.assertEqual(len(errors), 1)
            with (run / 'videos.csv').open(encoding='utf-8-sig', newline='') as file:
                self.assertEqual(next(csv.DictReader(file))['title'], data['title'])

    def test_cancel_saves_collected_metadata(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            (root / 'tools').mkdir()
            (root / 'tools' / 'yt-dlp.exe').touch()
            job = CrawlJob('@sample', root=root)
            (job.run_dir / 'metadata').mkdir(parents=True)
            (job.run_dir / 'metadata' / 'a.info.json').write_text(json.dumps({'id': 'abcdefghijk', 'title': 'Saved'}))
            process = SimpleNamespace(stdout=iter([]), poll=lambda: None, terminate=lambda: None, wait=lambda: -15)
            # stdout needs close(), like the subprocess pipe.
            import io
            process.stdout = io.StringIO('')
            job.stop()
            with patch('crawler.subprocess.Popen', return_value=process):
                job.run()
            kind, (_, summary) = job.events.get_nowait()
            self.assertEqual(kind, 'done')
            self.assertEqual(summary['status'], 'cancelled')
            self.assertEqual(summary['video_count'], 1)
            self.assertTrue((job.run_dir / 'videos.csv').exists())

    def test_ui_load_filter_sort_existing_data(self):
        page = SimpleNamespace(window=SimpleNamespace(), add=lambda *args: None, update=lambda: None)
        app = ResearchApp(page)
        app.load_run(ROOT / 'InfinitePlanet4K' / '20260910_145925_929')
        self.assertEqual(len(app.grid.controls), 31)
        app.search.value = 'NO MATCH 928374928374'
        app.render_cards()
        self.assertTrue(app.empty.visible)
        app.search.value = ''
        app.sort.value = 'views'
        app.render_cards()
        self.assertEqual(len(app.grid.controls), 31)
        self.assertEqual(posted({'created_time': '2026-01-02T20:00:00Z'}), '03/01/2026 · 03:00')


if __name__ == '__main__':
    unittest.main()
