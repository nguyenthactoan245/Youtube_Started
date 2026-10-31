"""Download the Windows essentials build and verify the published SHA-256."""
import hashlib
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

def main():
    target = ROOT / 'bin'
    target.mkdir(exist_ok=True)
    if all((target / name).is_file() for name in ('ffmpeg.exe', 'ffprobe.exe')):
        print('FFmpeg and FFprobe are ready.')
        return
    print('Downloading FFmpeg essentials from gyan.dev...', flush=True)
    with tempfile.TemporaryDirectory(dir=ROOT) as temp:
        archive = Path(temp) / 'ffmpeg.zip'
        urllib.request.urlretrieve(URL, archive)
        expected = urllib.request.urlopen(URL + '.sha256', timeout=60).read().decode().split()[0]
        with archive.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual.lower() != expected.lower():
            raise RuntimeError('FFmpeg checksum mismatch. Please retry.')
        with zipfile.ZipFile(archive) as bundle:
            for name in ('ffmpeg.exe', 'ffprobe.exe', 'LICENSE', 'README.txt'):
                matches = [p for p in bundle.namelist() if p.endswith('/' + name)]
                if not matches:
                    raise RuntimeError(f'Missing {name} in download')
                with bundle.open(matches[0]) as source, (target / name).open('wb') as dest:
                    shutil.copyfileobj(source, dest)
    print('FFmpeg and FFprobe installed in bin/.')

if __name__ == '__main__':
    main()
