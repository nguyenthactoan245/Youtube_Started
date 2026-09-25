import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import diagnostics as d


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.object(d, '_enabled', True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_disabled_logging_never_writes(self):
        with patch.object(d, '_enabled', False), patch.object(Path, 'open') as output:
            d.record(stage='search')
            output.assert_not_called()

    def test_classification(self):
        self.assertEqual(d.http_fields(429, {'CF-Mitigated': 'challenge'})['kind'], 'cloudflare_challenge')
        self.assertEqual(d.http_fields(429, {}, b'API rate limit exceeded')['kind'], 'api_quota')
        self.assertEqual(d.http_fields(429, {'Content-Type': 'text/html'})['kind'], 'html_429')
        self.assertEqual(d.http_fields(429, {})['kind'], 'unknown_429')

    def test_export_has_metadata_but_no_raw_inputs(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(d, 'LOG_PATH', Path(folder)/'log.jsonl'):
            secret = 'never-export-this-key'
            fields = d.http_fields(429, {'X-RateLimit-Remaining': '0', 'Authorization': secret},
                                   ('API rate limit exceeded ' + secret).encode())
            d.record(stage='search', key_label=d.key_label(secret), **fields,
                     url='https://example.test/?key='+secret, query='private script')
            target = Path(folder)/'export.txt'
            d.export_log(target)
            result = target.read_text(encoding='utf-8')
            self.assertIn('api_quota', result)
            self.assertIn('remaining', result)
            for private in (secret, 'private script', 'https://'):
                self.assertNotIn(private, result)

    def test_rotation_parallel_writes_and_empty_export(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(d, 'LOG_PATH', Path(folder)/'log.jsonl'):
            target = Path(folder)/'export.txt'
            d.export_log(target)
            self.assertIn('Chưa có', target.read_text(encoding='utf-8'))
            d.LOG_PATH.write_text(' ' * 1_000_001, encoding='utf-8')
            with ThreadPoolExecutor(3) as executor:
                list(executor.map(lambda _: d.record(stage='download_mp4', status=429), range(9)))
            self.assertTrue(d.LOG_PATH.with_suffix('.previous.jsonl').exists())
            self.assertEqual(len(d.LOG_PATH.read_text().splitlines()), 9)
            with self.assertRaises(ValueError):
                d.export_log(d.LOG_PATH)

    def test_logging_failure_does_not_fail_request(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError):
            d.record(stage='search', status=429)
