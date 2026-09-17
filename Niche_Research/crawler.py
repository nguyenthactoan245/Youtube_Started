"""Public YouTube metadata collection and compatible CSV/JSON snapshots."""
from __future__ import annotations

import csv
import json
import queue
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
FIELDS = ['video_id', 'title', 'view_count', 'created_time', 'created_time_precision',
          'upload_date', 'video_url', 'thumbnail_url', 'thumbnail_file', 'channel', 'collected_at_utc']


def normalize_channel(value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith(('youtube.com/', 'www.youtube.com/')):
        value = 'https://' + value
    if '://' in value:
        parsed = urlparse(value)
        if parsed.scheme != 'https' or parsed.netloc.lower() not in ('youtube.com', 'www.youtube.com'):
            raise ValueError('Hãy nhập @tenkenh hoặc URL kênh youtube.com.')
        path = unquote(parsed.path).strip('/')
        path = re.sub(r'/(videos|shorts|streams)$', '', path)
    else:
        path = '@' + value.lstrip('@')
    if not re.fullmatch(r'@[\w.-]{1,100}|channel/UC[A-Za-z0-9_-]{22}', path):
        raise ValueError('Tên kênh không hợp lệ. Ví dụ: @InfinitePlanet4K')
    slug = re.sub(r'[^\w.-]', '_', path.split('/')[-1]).strip('._')
    if not slug:
        raise ValueError('Tên kênh không hợp lệ.')
    return 'https://www.youtube.com/' + path, slug


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def saved_runs(root: Path = ROOT) -> list[Path]:
    runs = list(root.glob('*/*/videos.json'))
    return [p.parent for p in sorted(runs, key=lambda p: p.parent.name, reverse=True)]


def load_rows(run: Path) -> list[dict]:
    rows = read_json(run / 'videos.json')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('File videos.json không đúng định dạng.')
    return rows


def export_metadata(run: Path) -> tuple[list[dict], list[str]]:
    rows, errors = {}, []
    thumbs = {p.stem: p for p in (run / 'thumbnails').glob('*') if p.is_file() and p.stat().st_size}
    for file in (run / 'metadata').glob('*.info.json'):
        try:
            v = read_json(file)
            if v.get('_type') == 'playlist' or not re.fullmatch(r'[\w-]{11}', v.get('id', '')):
                continue
            created, precision = None, 'unavailable'
            if v.get('timestamp') is not None:
                created = datetime.fromtimestamp(v['timestamp'], timezone.utc).isoformat()
                precision = 'timestamp_utc'
            elif v.get('upload_date'):
                created = datetime.strptime(v['upload_date'], '%Y%m%d').date().isoformat()
                precision = 'date_only'
            thumb = thumbs.get(v['id'])
            rows[v['id']] = dict(zip(FIELDS, [
                v['id'], v.get('title'), v.get('view_count'), created, precision,
                v.get('upload_date'), v.get('webpage_url') or f"https://www.youtube.com/watch?v={v['id']}",
                v.get('thumbnail'), thumb.relative_to(run).as_posix() if thumb else None,
                v.get('channel'), datetime.fromtimestamp(file.stat().st_mtime, timezone.utc).isoformat(),
            ]))
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            errors.append(f'{file.name}: {exc}')
    result = sorted(rows.values(), key=lambda r: r.get('created_time') or '', reverse=True)
    (run / 'videos.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    with (run / 'videos.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(result)
    return result, errors


class CrawlJob:
    def __init__(self, channel: str, limit: int = 0, root: Path = ROOT):
        self.url, self.slug = normalize_channel(channel)
        if not 0 <= limit <= 1_000_000:
            raise ValueError('Giới hạn phải từ 0 đến 1.000.000.')
        self.limit, self.root = limit, root
        self.events: queue.Queue = queue.Queue()
        self.cancelled = threading.Event()
        self.process = None
        self.run_dir = root / self.slug / datetime.now().strftime('%Y%m%d_%H%M%S_%f')

    def stop(self):
        self.cancelled.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass

    def run(self):
        started = datetime.now(timezone.utc).isoformat()
        exit_code, errors = -1, []
        try:
            exe = self.root / 'tools' / 'yt-dlp.exe'
            if not exe.is_file():
                raise FileNotFoundError('Không tìm thấy tools/yt-dlp.exe.')
            for name in ('metadata', 'thumbnails'):
                (self.run_dir / name).mkdir(parents=True, exist_ok=True)
            args = [str(exe), '--ignore-config', '--skip-download', '--write-info-json',
                    '--write-thumbnail', '--ignore-errors', '--ignore-no-formats-error',
                    '--no-progress', '--windows-filenames', '--socket-timeout', '30',
                    '--retries', '3', '--extractor-retries', '3', '--sleep-requests', '1',
                    '--encoding', 'utf-8', '-o', str(self.run_dir / 'metadata' / '%(id)s.%(ext)s'),
                    '-o', 'thumbnail:' + str(self.run_dir / 'thumbnails' / '%(id)s.%(ext)s')]
            if self.limit:
                args += ['--playlist-end', str(self.limit)]
            args.append(self.url)
            with (self.run_dir / 'crawl.log').open('w', encoding='utf-8') as log:
                self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                encoding='utf-8', errors='replace',
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if self.cancelled.is_set():
                    self.stop()
                for line in self.process.stdout:
                    line = line.rstrip()
                    log.write(line + '\n')
                    log.flush()
                    self.events.put(('log', line))
                    if line.startswith('ERROR:'):
                        errors.append(line)
                    match = re.search(r'Downloading item (\d+) of (\d+)', line)
                    if match:
                        self.events.put(('progress', (int(match[1]), int(match[2]))))
                exit_code = self.process.wait()
                self.process.stdout.close()
            rows, export_errors = export_metadata(self.run_dir)
            errors.extend(export_errors)
            missing = sum(not row['thumbnail_file'] or row['created_time'] is None or row['view_count'] is None for row in rows)
            status = ('cancelled' if self.cancelled.is_set() else 'failed' if not rows
                      else 'partial' if exit_code or errors or missing else 'complete')
            summary = dict(channel_url=self.url, started_at_utc=started,
                           finished_at_utc=datetime.now(timezone.utc).isoformat(),
                           video_count=len(rows), status=status, extractor_exit_code=exit_code,
                           limit_per_playlist=self.limit, missing_thumbnails=sum(not r['thumbnail_file'] for r in rows),
                           missing_created_time=sum(not r['created_time'] for r in rows), errors=errors)
            (self.run_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            self.events.put(('done', (self.run_dir, summary)))
        except Exception as exc:
            self.stop()
            if self.process:
                self.process.wait()
            self.events.put(('error', str(exc)))
