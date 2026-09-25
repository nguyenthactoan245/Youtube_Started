"""Bounded read-only API probe. Reports quota metadata, never credentials or URLs."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app_config import parse_api_keys


def configs():
    paths = [Path(__file__).parent / '.env',
             Path(os.environ.get('LOCALAPPDATA', '')) / 'StockDownloader' / '.env']
    result = []
    for path in paths:
        if not path.is_file():
            continue
        values = {}
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                values[k.strip()] = v.strip().strip('\"\'')
        keys = parse_api_keys(values.get('PIXABAY_API_KEYS', '') or values.get('PIXABAY_API_KEY', ''))
        result.append((path, keys))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--rounds', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--key', type=int, choices=range(1, 6))
    args = parser.parse_args()
    sources = configs()
    print(json.dumps({'configs': [{'location': 'source' if p.parent == Path(__file__).parent else 'installed',
                                   'keys': len(keys)} for p, keys in sources]}))
    keys = list(dict.fromkeys(key for _, group in sources for key in group))
    print(json.dumps({'distinct_keys': len(keys),
                      'distinct_key_prefixes': len({k.split('-')[0] for k in keys})}))
    if not args.probe:
        return
    records = []
    for round_id in range(args.rounds):
        for i, key in enumerate(keys[:5], 1):
            if args.key is not None and args.key != i:
                continue
            if records:
                time.sleep(2)
            params = {'key': key, 'q': 'nature', 'per_page': 200, 'safesearch': 'true',
                      'order': 'popular', 'page': 1}
            request = Request('https://pixabay.com/api/videos/?' + urlencode(params),
                              headers={'User-Agent': 'StockDownloader/1.0'})
            row = {'round': round_id + 1, 'key': i, 'time': datetime.now(timezone.utc).isoformat()}
            start = time.monotonic()
            try:
                response = urlopen(request, timeout=30)
            except HTTPError as error:
                response = error
            except Exception as error:
                row['network_error'] = type(error).__name__
                records.append(row)
                print(json.dumps(row), flush=True)
                continue
            with response:
                row['status'] = response.code
                names = ['X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Reset',
                         'Retry-After', 'Content-Type', 'Age', 'CF-Cache-Status', 'Date',
                         'Server', 'CF-Mitigated']
                row['headers'] = {name: response.headers[name] for name in names if response.headers.get(name)}
                body = response.read()
                row['bytes'] = len(body)
                if response.code == 200:
                    try:
                        data = json.loads(body)
                        row['hits'] = len(data.get('hits', []))
                    except ValueError:
                        row['body_kind'] = 'non_json'
                else:
                    message = body.decode('utf-8', errors='replace').lower()
                    row['body_kind'] = ('api_rate_limit' if 'api rate limit exceeded' in message else
                                        'invalid_key' if 'invalid' in message and 'key' in message else
                                        'html' if '<html' in message or '<!doctype html' in message else
                                        'other')
                    row['markers'] = [marker for marker in (
                        'cloudflare', 'you are being rate limited', 'error 1015', 'captcha',
                        'just a moment', 'too many requests', 'access denied', 'challenge')
                        if marker in message]
            row['elapsed_seconds'] = round(time.monotonic() - start, 3)
            records.append(row)
            print(json.dumps(row), flush=True)
    target = Path(__file__).parent / 'tests' / '.test-work' / ('pixabay-diagnostic-' + str(time.time_ns()) + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(records, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
