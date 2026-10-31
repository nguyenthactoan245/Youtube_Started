"""Local speech recognition in a cancellable child process."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile

from merger import FLAGS, ROOT, run_ffmpeg


def check_available():
    if importlib.util.find_spec('faster_whisper') is not None:
        return sys.executable
    # IDEs may launch the system Python instead of the environment used by run.bat.
    local_python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if local_python.is_file() and local_python.resolve() != Path(sys.executable).resolve():
        try:
            result = subprocess.run([str(local_python), '-c', 'import faster_whisper'],
                capture_output=True, timeout=30, creationflags=FLAGS)
            if result.returncode == 0:
                return str(local_python)
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise RuntimeError('Không tìm thấy môi trường tạo SRT. Hãy chạy Reup/install.bat rồi mở Reup/run.bat. '
                       f'Python hiện tại: {sys.executable}')


def timestamp(seconds):
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def cues(segments, offset, duration):
    for segment in segments:
        start = max(0, min(duration, segment.start))
        end = max(start, min(duration, segment.end))
        text = ' '.join(segment.text.split())
        if text and round(end * 1000) > round(start * 1000):
            yield (offset + start, offset + end, text)


def publish(source, destination):
    """Publish without replacing an existing subtitle file."""
    destination = Path(destination)
    candidate = destination
    number = 1
    while True:
        try:
            if os.name == 'nt':
                Path(source).rename(candidate)
            else:
                os.link(source, candidate)
            return candidate
        except FileExistsError:
            candidate = destination.with_name(f'{destination.stem}_{number}.srt')
            number += 1


async def export_srt(videos, destination, language='auto', model='base', progress=lambda p, m: None):
    interpreter = await asyncio.to_thread(check_available)
    if not any(v.audio for v in videos):
        raise ValueError('Không có âm thanh nguồn để tạo SRT.')
    total = sum(v.duration for v in videos)
    with tempfile.TemporaryDirectory(prefix='.srt-', dir=Path(destination).parent) as folder:
        folder = Path(folder)
        entries = []
        offset = 0
        for i, video in enumerate(videos):
            if video.audio:
                wav = folder / f'{i}.wav'
                await run_ffmpeg(['-i', video.path, '-map', f'0:{video.audio["index"]}',
                    '-vn', '-af', 'asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,apad',
                    '-t', str(video.duration), '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', wav],
                    video.duration, lambda p: progress(.15 * (offset + p * video.duration) / total,
                                                       f'Chuẩn bị âm thanh {i + 1}/{len(videos)}'))
                entries.append({'path': str(wav), 'offset': offset, 'duration': video.duration})
            offset += video.duration
        manifest = folder / 'job.json'
        result = folder / 'result.srt'
        manifest.write_text(json.dumps({'entries': entries, 'language': language, 'model': model,
                                        'result': str(result), 'total': total}), encoding='utf-8')
        progress(.15, 'Đang nạp mô hình nhận dạng; lần đầu cần Internet để tải mô hình...')
        with tempfile.TemporaryFile() as errors:
            process = await asyncio.create_subprocess_exec(interpreter, str(Path(__file__).resolve()),
                str(manifest), stdout=asyncio.subprocess.PIPE, stderr=errors, creationflags=FLAGS)
            try:
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    progress(.15 + .85 * event['progress'], event['message'])
                await process.wait()
                if process.returncode:
                    errors.seek(0)
                    raise RuntimeError('Tạo SRT thất bại: ' + errors.read().decode('utf-8', errors='replace')[-1500:])
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        if not result.stat().st_size:
            raise ValueError('Không nhận dạng được lời nói; không tạo file SRT rỗng.')
        saved = publish(result, destination)
        progress(1, f'Đã lưu SRT: {saved.name}')
        return saved


def worker(manifest):
    from faster_whisper import WhisperModel
    job = json.loads(Path(manifest).read_text(encoding='utf-8'))
    model = WhisperModel(job['model'], device='cpu', compute_type='int8',
                         download_root=str(ROOT / 'models'))
    index = 0
    with Path(job['result']).open('w', encoding='utf-8', newline='\n') as output:
        for entry in job['entries']:
            segments, _ = model.transcribe(entry['path'], language=None if job['language'] == 'auto' else job['language'],
                beam_size=5, vad_filter=True, condition_on_previous_text=False)
            for start, end, text in cues(segments, entry['offset'], entry['duration']):
                index += 1
                output.write(f'{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n\n')
                print(json.dumps({'progress': min(.99, end / job['total']),
                                  'message': f'Đang tạo SRT: {timestamp(end)}'}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    worker(sys.argv[1])
