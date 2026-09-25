"""Bounded diagnostic records with no raw keys, URLs, queries or response bodies."""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app_config import APP_VERSION, DATA_ROOT

LOG_PATH = DATA_ROOT / 'logs' / 'application-diagnostics.jsonl'
_enabled = False


def enable_logging():
    global _enabled
    _enabled = True
_lock = Lock()
_labels = {}
_session = uuid.uuid4().hex[:8]
_fields = {'stage', 'status', 'kind', 'limit', 'remaining', 'reset', 'retry_after',
           'elapsed_ms', 'key_label', 'video_id', 'error_type'}


def key_label(key):
    with _lock:
        if key not in _labels:
            _labels[key] = f'Key {len(_labels) + 1}'
        return _labels[key]


def record(**fields):
    if not _enabled:
        return
    row = {'time': datetime.now(timezone.utc).isoformat(), 'session': _session,
           'pid': os.getpid(), 'version': APP_VERSION}
    row.update({k: v for k, v in fields.items() if k in _fields})
    try:
        with _lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
                LOG_PATH.replace(LOG_PATH.with_suffix('.previous.jsonl'))
            with LOG_PATH.open('a', encoding='utf-8') as output:
                output.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        pass  # Diagnostics must never interrupt a download.


def http_fields(status, headers, body=b''):
    result = {'status': status, 'kind': 'success' if status < 400 else 'http_error'}
    for header, field in [('X-RateLimit-Limit', 'limit'), ('X-RateLimit-Remaining', 'remaining'),
                          ('X-RateLimit-Reset', 'reset'), ('Retry-After', 'retry_after')]:
        try:
            result[field] = int(headers.get(header, ''))
        except (ValueError, TypeError):
            pass
    if headers.get('CF-Mitigated', '').lower() == 'challenge':
        result['kind'] = 'cloudflare_challenge'
    elif status == 429:
        message = body[:8192].lower()
        result['kind'] = ('api_quota' if b'api rate limit exceeded' in message else
                          'html_429' if 'text/html' in headers.get('Content-Type', '') else 'unknown_429')
    return result


def export_log(destination):
    destination = Path(destination)
    with _lock:
        sources = [LOG_PATH.with_suffix('.previous.jsonl'), LOG_PATH]
        if destination.resolve() in [p.resolve() for p in sources]:
            raise ValueError('Hãy chọn tên file khác với log nội bộ.')
        content = '\n'.join(p.read_text(encoding='utf-8') for p in sources if p.exists())
    header = ('Stock diagnostic log — ' + APP_VERSION + '\n'
              'UTC timestamps. No API keys, search text, file paths or response bodies.\n'
              'Key labels are local to each session.\n\n')
    destination.write_text(header + (content or 'Chưa có sự kiện. Hãy chạy lại thao tác bị lỗi rồi xuất log.\n'),
                           encoding='utf-8')
