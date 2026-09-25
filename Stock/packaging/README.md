# Stock Downloader v1.9.1 — Windows x64

Installer: `dist/Stock-Downloader-Setup-v1.9.1-x64.exe`.

Run the installer, open Stock Downloader, and enter your Pixabay API key in **Setup**.
The installer includes Python, the Flet desktop/video runtime, FFmpeg, and the app icon.
It installs for the current user without administrator rights and provides an uninstaller.

Installed app data lives in `%LOCALAPPDATA%\StockDownloader` (database, library,
API configuration, cache, and logs). Upgrading or uninstalling does not remove this data.
The source app continues to use the existing `Stock` directory. Existing development
videos and credentials are not embedded in the installer or automatically migrated.

## Rebuild

Use Windows x64, Python 3.14, `requirements.txt`, PyInstaller 6.20.0, Inno Setup 6,
and a Windows FFmpeg distribution with its `LICENSE` and `README.txt`.

```powershell
python -m pip install -r requirements.txt
python -m pip install --target .build-tools/python pyinstaller==6.20.0
python packaging/build_windows.py --iscc "C:\Path\To\Inno Setup 6\ISCC.exe"
```

By default the builder uses the cached Flet 0.85.3 Windows client at
`~/.flet/client/flet-desktop-full-0.85.3/flet`. Use `--client-dir` to supply that
version's extracted runtime elsewhere. FFmpeg must be on `PATH` at build time.
`STOCK_DATA_DIR` can override the runtime data directory for isolated smoke tests.

The generated installer is unsigned.
