from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_config import configured_api_keys, parse_api_keys, save_api_keys
from api_key_settings import ApiKeySettings
from services.api_keys import ApiKeyPool, get_api_key_pool
from services.pixabay import PixabayError, PixabayRateLimitError, PixabayAccessPause, _fetch_payload, search_page
from script_view import ScriptView


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class ApiKeyTests(unittest.TestCase):
    def test_cloudflare_retries_after_five_seconds(self):
        pool = ApiKeyPool(['alpha'], interval=0)
        clock = FakeClock()
        calls = []

        def request(key, headers):
            calls.append(key)
            if len(calls) == 1:
                raise PixabayAccessPause('Cloudflare')
            return 'ok'

        self.assertEqual(pool.fetch(request, clock), 'ok')
        self.assertEqual(calls, ['alpha', 'alpha'])
        self.assertEqual(clock.waits, [5])
        self.assertFalse(pool.auto_retrying)
        self.assertFalse(pool.pause_reason)

    def test_cloudflare_pauses_all_keys_until_resume(self):
        from services.pixabay import PixabayAccessPause
        pool = ApiKeyPool(['alpha', 'beta'], interval=0)
        blocked = Event()
        retry_waiting, release_retry = Event(), Event()

        class RetryGate(Event):
            def wait(self, timeout=None):
                if timeout == 5:
                    retry_waiting.set()
                    release_retry.wait(1)
                    return self.is_set()
                return super().wait(timeout)

        cancel = RetryGate()
        calls = []
        def request(key, headers):
            calls.append(key)
            if len(calls) == 1:
                blocked.set()
                raise PixabayAccessPause('Cloudflare')
            return key
        with ThreadPoolExecutor(2) as executor:
            first = executor.submit(pool.fetch, request, cancel)
            try:
                self.assertTrue(blocked.wait(1))
                import time
                deadline = time.monotonic() + 1
                while not retry_waiting.is_set() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(retry_waiting.is_set())
                self.assertEqual(pool.pause_reason, 'Cloudflare')
                second = executor.submit(pool.fetch, request, cancel)
                time.sleep(.15)
                self.assertEqual(len(calls), 1)
                self.assertFalse(first.done())
                self.assertFalse(second.done())
                release_retry.set()
                first.result(timeout=2)
                second.result(timeout=2)
                self.assertEqual(len(calls), 3)
            finally:
                release_retry.set()
                cancel.set()

    def test_cloudflare_wait_can_be_cancelled_and_config_does_not_clear_it(self):
        pool = ApiKeyPool(['alpha'], interval=0)
        pool.pause_reason = 'Cloudflare'
        pool.configure(['beta'])
        self.assertEqual(pool.statuses(), ['Cloudflare'])
        cancel = Event()
        with ThreadPoolExecutor(1) as executor:
            request = Mock()
            future = executor.submit(pool.fetch, request, cancel)
            cancel.set()
            with self.assertRaises(InterruptedError):
                future.result(timeout=1)
            request.assert_not_called()

    def test_cloudflare_is_not_quota_even_with_retry_header(self):
        from services.pixabay import PixabayAccessPause
        for status in (403, 429):
            error = HTTPError('https://example.test', status, 'blocked',
                              {'CF-Mitigated': 'challenge', 'Retry-After': '60'}, io.BytesIO(b'html'))
            with patch('services.pixabay.urlopen', side_effect=error):
                with self.assertRaises(PixabayAccessPause):
                    _fetch_payload('alpha', 'query')

    def test_config_migration_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            (root / '.env').write_text('# keep\nOTHER=value\nPIXABAY_API_KEY=old\n', encoding='utf-8')
            os.environ['PIXABAY_API_KEY'] = 'old'
            self.assertEqual(configured_api_keys(), ['old'])
            self.assertEqual(save_api_keys(' alpha, beta\nalpha\n', root), ['alpha', 'beta'])
            self.assertEqual(configured_api_keys(), ['alpha', 'beta'])
            saved = (root / '.env').read_text(encoding='utf-8')
            self.assertIn('OTHER=value', saved)
            self.assertNotIn('PIXABAY_API_KEY=', saved)
            with patch.dict(os.environ, {}, clear=True):
                for line in saved.splitlines():
                    if '=' in line:
                        key, value = line.split('=', 1)
                        os.environ[key] = value
                self.assertEqual(configured_api_keys(), ['alpha', 'beta'])
            save_api_keys('', root)
            self.assertEqual(configured_api_keys(), [])

    def test_validation_and_new_setting_precedence(self):
        with self.assertRaises(ValueError):
            parse_api_keys('alpha\nOTHER=bad')
        with patch.dict(os.environ, {'PIXABAY_API_KEYS': 'alpha,beta', 'PIXABAY_API_KEY': 'old'}):
            self.assertEqual(configured_api_keys(), ['alpha', 'beta'])

    def test_rotation_and_configuration_changes_preserve_cooldown(self):
        pool = ApiKeyPool(['alpha', 'beta'], interval=0)
        calls = []
        def request(key, headers):
            calls.append(key)
            return key
        for _ in range(4):
            pool.fetch(request)
        self.assertEqual(calls, ['alpha', 'beta', 'alpha', 'beta'])
        def exhaust(key, headers):
            headers({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '60'})
        pool.fetch(exhaust)
        pool.configure(['alpha', 'gamma'])
        self.assertEqual(pool.fetch(request), 'gamma')
        self.assertIn('chờ', pool.statuses()[0])

    def test_429_skips_key_and_preserves_delay(self):
        pool = ApiKeyPool(['alpha', 'beta'], interval=0)
        calls = []
        def request(key, headers):
            calls.append(key)
            if key == 'alpha':
                raise PixabayRateLimitError(90)
            return {'ok': True}
        self.assertEqual(pool.fetch(request), {'ok': True})
        self.assertEqual(calls, ['alpha', 'beta'])
        self.assertIn('chờ 90', pool.statuses()[0])

    def test_wait_and_global_request_spacing(self):
        clock = FakeClock()
        with patch('services.api_keys.time.monotonic', side_effect=lambda: clock.now):
            pool = ApiKeyPool(['alpha', 'beta'], interval=0.75)
            times = []
            def request(key, headers):
                times.append(clock.now)
                headers({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '2'})
            for _ in range(3):
                pool.fetch(request, clock)
        self.assertGreaterEqual(times[1] - times[0], .75)
        self.assertGreaterEqual(times[2] - times[0], 2)

    def test_repeated_429_has_bounded_attempts(self):
        clock = FakeClock()
        request = Mock(side_effect=PixabayRateLimitError(1))
        with patch('services.api_keys.time.monotonic', side_effect=lambda: clock.now):
            pool = ApiKeyPool(['alpha', 'beta'], interval=0)
            with self.assertRaises(PixabayRateLimitError):
                pool.fetch(request, clock)
        self.assertEqual(request.call_count, 6)

    def test_cancel_during_all_keys_cooldown(self):
        pool = ApiKeyPool(['alpha'], interval=0)
        pool.fetch(lambda key, headers: headers({'X-RateLimit-Remaining': '0', 'Retry-After': '60'}))
        cancel = Event()
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(pool.fetch, Mock(), cancel)
            cancel.set()
            with self.assertRaises(InterruptedError):
                future.result(timeout=1)

    def test_busy_key_is_not_used_by_second_thread(self):
        pool = ApiKeyPool(['alpha', 'beta'], interval=0)
        entered, release = Event(), Event()
        def first(key, headers):
            entered.set()
            release.wait(2)
            return key
        with ThreadPoolExecutor(2) as executor:
            future = executor.submit(pool.fetch, first)
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(pool.fetch(lambda key, headers: key), 'beta')
            finally:
                release.set()
            self.assertEqual(future.result(timeout=1), 'alpha')

    def test_invalid_key_disabled_but_network_failure_not_disabled(self):
        pool = ApiKeyPool(['alpha', 'beta'], interval=0)
        error = PixabayError('invalid')
        error.invalid_key = True
        def request(key, headers):
            if key == 'alpha':
                raise error
            return key
        self.assertEqual(pool.fetch(request), 'beta')
        self.assertIn('cần kiểm tra', pool.statuses()[0])
        with self.assertRaises(PixabayError):
            pool.fetch(Mock(side_effect=PixabayError('network')))
        self.assertIn('sẵn sàng', pool.statuses()[1])
        pool.configure(['alpha'])
        with self.assertRaisesRegex(PixabayError, 'cần kiểm tra'):
            pool.fetch(request)

    def test_cache_does_not_consume_next_key(self):
        pool = ApiKeyPool(['alpha', 'beta'], interval=0)
        with tempfile.TemporaryDirectory() as folder, patch('services.pixabay._fetch_payload', return_value={'hits': []}) as fetch:
            for _ in range(2):
                search_page(pool, 'query', cache_dir=Path(folder))
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[0], 'alpha')
            search_page(pool, 'other', cache_dir=Path(folder))
            self.assertEqual(fetch.call_args.args[0], 'beta')

    def test_transport_classifies_invalid_key_and_hides_url(self):
        error = HTTPError('https://example.test/?key=secret', 400, 'bad', {},
                          io.BytesIO(b'[ERROR 400] Invalid API key'))
        with patch('services.pixabay.urlopen', side_effect=error):
            with self.assertRaises(PixabayError) as caught:
                _fetch_payload('secret', 'query')
        self.assertTrue(caught.exception.invalid_key)
        error.close()
        with patch('services.pixabay.urlopen', side_effect=OSError('https://example.test/?key=secret')):
            with self.assertRaises(PixabayError) as caught:
                _fetch_payload('secret', 'query')
        self.assertNotIn('secret', str(caught.exception))

    def test_script_does_not_stack_retries_for_pool_and_redacts_all_keys(self):
        from unittest.mock import AsyncMock
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path('unused'))
        with patch.object(view, 'download_with_progress', new_callable=AsyncMock,
                          side_effect=PixabayRateLimitError()) as download:
            with self.assertRaises(PixabayRateLimitError):
                asyncio.run(view.download_with_retry('query', ApiKeyPool(['alpha']), ''))
        self.assertEqual(download.await_count, 1)
        with patch.dict(os.environ, {'PIXABAY_API_KEYS': 'alpha,beta'}):
            self.assertNotIn('alpha', view.error_text('alpha beta'))
            self.assertNotIn('beta', view.error_text('alpha beta'))

    def test_settings_add_remove_save_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            page = SimpleNamespace(update=lambda: None)
            settings = ApiKeySettings(page, Path(folder))
            self.assertTrue(settings.fields[0].password)
            settings.fields[0].value = 'alpha'
            settings.add(None)
            settings.fields[1].value = 'beta'
            settings.add(None)
            settings.fields[2].data(None)
            asyncio.run(settings.save(None))
            self.assertEqual(configured_api_keys(), ['alpha', 'beta'])
            reopened = ApiKeySettings(page, Path(folder))
            self.assertEqual([f.value for f in reopened.fields], ['alpha', 'beta'])

    def test_settings_validation_and_write_failure_keep_previous_keys(self):
        with patch.dict(os.environ, {'PIXABAY_API_KEYS': 'alpha'}, clear=True):
            settings = ApiKeySettings(SimpleNamespace(update=lambda: None), Path('unused'))
            settings.fields[0].value = 'bad=value'
            asyncio.run(settings.save(None))
            self.assertEqual(configured_api_keys(), ['alpha'])
            self.assertFalse(settings.disabled)
            with patch('api_key_settings.save_api_keys', side_effect=OSError('private path')):
                asyncio.run(settings.save(None))
            self.assertNotIn('private path', settings.status.value)

    def test_shared_pool_and_save_during_inflight_request(self):
        with patch.dict(os.environ, {'PIXABAY_API_KEYS': 'alpha,beta'}, clear=True):
            pool = get_api_key_pool()
            self.assertIs(pool, get_api_key_pool())
            isolated = ApiKeyPool(['alpha', 'beta'], interval=0)
            entered, release = Event(), Event()
            def request(key, headers):
                entered.set()
                release.wait(2)
                return key
            with ThreadPoolExecutor(1) as executor:
                future = executor.submit(isolated.fetch, request)
                try:
                    self.assertTrue(entered.wait(1))
                    isolated.configure(['gamma'])
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=1), 'alpha')
            self.assertEqual(isolated.fetch(lambda key, headers: key), 'gamma')

    def test_real_transport_headers_update_pool_quota(self):
        response = io.BytesIO(b'{"hits": []}')
        response.headers = {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '30'}
        pool = ApiKeyPool(['alpha'], interval=0)
        with patch('services.pixabay.urlopen', return_value=response):
            self.assertEqual(pool.fetch(lambda key, headers: _fetch_payload(key, 'query', on_headers=headers)),
                             {'hits': []})
        self.assertIn('chờ 30', pool.statuses()[0])


if __name__ == '__main__':
    unittest.main()
