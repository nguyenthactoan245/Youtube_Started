"""Cancellable local vocal isolation using Demucs."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import re

from merger import ROOT, FLAGS, executable, run_ffmpeg


def demucs_worker(args):
    """Run Demucs inside the packaged executable."""
    from demucs.separate import main
    main(args)


def _time_seconds(value):
    parts = value.strip().split(':')
    if not 1 <= len(parts) <= 3:
        raise ValueError
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise ValueError from None
    if any(number < 0 for number in numbers) or any(number >= 60 for number in numbers[1:]):
        raise ValueError
    return sum(number * (60 ** index) for index, number in enumerate(reversed(numbers)))


def parse_music_ranges(text, duration):
    """Parse one start-end range per line and return sorted, merged seconds."""
    ranges = []
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        match = re.fullmatch(r'(.+?)\s+(?:-|–|—|->)\s+(.+)', line)
        if not match:
            raise ValueError(f'Dòng {line_number}: dùng dạng 00:01:20 - 00:01:28.')
        try:
            start, end = (_time_seconds(value) for value in match.groups())
        except ValueError:
            raise ValueError(f'Dòng {line_number}: thời gian không hợp lệ.') from None
        if start >= end:
            raise ValueError(f'Dòng {line_number}: thời gian kết thúc phải lớn hơn bắt đầu.')
        if start >= duration or end > duration + .05:
            raise ValueError(f'Dòng {line_number}: khoảng thời gian vượt quá video.')
        ranges.append((start, min(end, duration)))
    if not ranges:
        raise ValueError('Hãy nhập ít nhất một đoạn cần tắt nhạc nền.')
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def find_python(device='cpu'):
    candidates = [ROOT / '.venv/Scripts/python.exe',
                  ROOT.parent / 'YTDownloader/.venv/Scripts/python.exe', Path(sys.executable)]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            check = ('import demucs.separate, torch; '
                     "assert torch.cuda.is_available(), 'CUDA GPU is unavailable'"
                     if device == 'gpu' else 'import demucs.separate, torch')
            result = subprocess.run([str(candidate), '-c', check],
                                    capture_output=True, timeout=30, creationflags=FLAGS)
            if result.returncode == 0:
                return str(candidate)
        except (OSError, subprocess.TimeoutExpired):
            continue
    detail = ' hoặc PyTorch không nhận CUDA' if device == 'gpu' else ''
    raise RuntimeError(f'Thiếu thư viện tách nhạc{detail}. Hãy chạy install_music.bat trong Reup.')


async def remove_background(source, destination, duration, progress, device='cpu', ranges=None):
    if device not in {'cpu', 'gpu'}:
        raise ValueError('Thiết bị xử lý phải là CPU hoặc GPU.')
    interpreter = await asyncio.to_thread(find_python, device)
    with tempfile.TemporaryDirectory(prefix='.audio-', dir=Path(destination).parent) as folder:
        folder = Path(folder)
        selected = ranges or [(0, duration)]
        env = os.environ.copy()
        env['PATH'] = str(Path(executable('ffmpeg')).parent) + os.pathsep + env.get('PATH', '')
        cache = ROOT.parent / 'YTDownloader/models/torch'
        env['TORCH_HOME'] = str(cache if cache.is_dir() else ROOT / 'models/torch')
        env['PYTHONIOENCODING'] = 'utf-8'
        vocals = []
        for index, (start, end) in enumerate(selected):
            audio = folder / f'input_{index:03}.wav'
            span = end - start
            await run_ffmpeg(['-ss', str(start), '-i', source, '-t', str(span), '-vn',
                '-ac', '2', '-ar', '44100', audio], span,
                lambda p, i=index: progress((i + .1 * p) / (len(selected) + 1),
                    f'Chuẩn bị đoạn {i + 1}/{len(selected)}...'))
            progress((index + .1) / (len(selected) + 1),
                     f'Tách nhạc đoạn {index + 1}/{len(selected)} bằng AI...')
            with tempfile.TemporaryFile() as log:
                demucs_args = ['--two-stems', 'vocals', '-n', 'htdemucs', '-d',
                    'cuda' if device == 'gpu' else 'cpu', '--shifts', '0',
                    '-o', str(folder / 'stems'), str(audio)]
                command = ([interpreter, '--demucs-worker', *demucs_args]
                           if getattr(sys, 'frozen', False)
                           else [interpreter, '-m', 'demucs', *demucs_args])
                process = await asyncio.create_subprocess_exec(*command, env=env,
                    stdout=log, stderr=log, creationflags=FLAGS)
                try:
                    await process.wait()
                    if process.returncode:
                        log.seek(0)
                        raise RuntimeError('Tách nhạc thất bại: ' + log.read().decode('utf-8', errors='replace')[-2000:])
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
            vocal = folder / f'stems/htdemucs/{audio.stem}/vocals.wav'
            if not vocal.is_file():
                raise RuntimeError('Không tìm thấy âm thanh lời nói sau khi tách nhạc.')
            vocals.append(vocal)
            progress((index + 1) / (len(selected) + 1),
                     f'Đã xử lý đoạn {index + 1}/{len(selected)}.')

        inputs = ['-i', source]
        for vocal in vocals:
            inputs += ['-i', vocal]
        filters, labels = [], []
        cursor = 0.0
        part = 0
        for index, (start, end) in enumerate(selected, 1):
            if start > cursor:
                label = f'a{part}'
                filters.append(f'[0:a:0]atrim=start={cursor}:end={start},asetpts=PTS-STARTPTS[{label}]')
                labels.append(f'[{label}]')
                part += 1
            label = f'a{part}'
            filters.append(f'[{index}:a:0]apad,atrim=duration={end - start},asetpts=PTS-STARTPTS[{label}]')
            labels.append(f'[{label}]')
            part += 1
            cursor = end
        if cursor < duration:
            label = f'a{part}'
            filters.append(f'[0:a:0]atrim=start={cursor}:end={duration},asetpts=PTS-STARTPTS[{label}]')
            labels.append(f'[{label}]')
        filters.append(''.join(labels) + f'concat=n={len(labels)}:v=0:a=1[outa]')
        await run_ffmpeg([*inputs, '-filter_complex', ';'.join(filters), '-map', '0:v:0',
            '-map', '[outa]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', str(duration),
            '-movflags', '+faststart', destination], duration,
            lambda p: progress(len(selected) / (len(selected) + 1) + p / (len(selected) + 1),
                               'Lưu video đã tắt nhạc nền theo đoạn...'))
