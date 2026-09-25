"""Build the frozen app and Inno Setup installer using local build tools."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".build-tools" / "python"))
os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(ROOT / ".build-tools" / "pyinstaller-cache"))

from app_config import APP_VERSION


def build() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-dir", type=Path, default=Path.home() / ".flet" / "client" /
                        "flet-desktop-full-0.85.3" / "flet")
    parser.add_argument("--iscc", type=Path, default=ROOT / ".build-tools" / "inno" / "ISCC.exe")
    args = parser.parse_args()
    for package in ("flet", "flet-desktop", "flet-video"):
        if importlib.metadata.version(package) != "0.85.3":
            raise RuntimeError(f"Install the pinned dependencies in requirements.txt: {package}")
    if not (args.client_dir / "flet.exe").is_file():
        raise FileNotFoundError("Flet 0.85.3 desktop runtime is missing; pass --client-dir")
    if not args.iscc.is_file():
        raise FileNotFoundError("Inno Setup compiler is missing; pass --iscc")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("FFmpeg is required on the build machine")
    ffmpeg_root = Path(ffmpeg).resolve().parent.parent
    for notice in ("LICENSE", "README.txt"):
        if not (ffmpeg_root / notice).is_file():
            raise FileNotFoundError(f"FFmpeg distribution must include {notice}")

    stage = ROOT / "build" / "packaging"
    stage.mkdir(parents=True, exist_ok=True)
    client = stage / "flet-client"
    shutil.copytree(args.client_dir, client, dirs_exist_ok=True)
    icon = ROOT / "assets" / "stock-check.ico"
    version = tuple(int(part) for part in APP_VERSION.split(".")) + (0,)
    from PyInstaller.utils.win32 import icon as win_icon, versioninfo
    from PyInstaller import config
    config.CONF["workpath"] = str(stage)
    vi = versioninfo.VSVersionInfo(
        ffi=versioninfo.FixedFileInfo(filevers=version, prodvers=version, fileType=1),
        kids=[versioninfo.StringFileInfo([versioninfo.StringTable("040904B0", [
            versioninfo.StringStruct("FileDescription", "Stock Downloader"),
            versioninfo.StringStruct("FileVersion", ".".join(map(str, version))),
            versioninfo.StringStruct("ProductName", "Stock Downloader"),
            versioninfo.StringStruct("ProductVersion", APP_VERSION),
            versioninfo.StringStruct("OriginalFilename", "StockDownloader.exe"),
        ])]), versioninfo.VarFileInfo([versioninfo.VarStruct("Translation", [1033, 1200])])],
    )
    version_file = stage / "version_info.txt"
    version_file.write_text(str(vi), encoding="utf-8")
    win_icon.CopyIcons(str(client / "flet.exe"), [str(icon)])
    versioninfo.write_version_info_to_executable(str(client / "flet.exe"), vi)

    notices = stage / "licenses"
    notices.mkdir(exist_ok=True)
    for distribution in ("flet", "flet-desktop", "flet-video", "httpx", "httpcore", "anyio",
                         "certifi", "idna", "h11", "msgpack", "oauthlib", "repath", "typing_extensions"):
        dist = importlib.metadata.distribution(distribution)
        for file in dist.files or []:
            if any(word in file.name.lower() for word in ("license", "copying", "notice")):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    shutil.copy2(source, notices / f"{distribution}-{file.name}")
    shutil.copy2(ffmpeg_root / "LICENSE", notices / "FFmpeg-LICENSE.txt")
    shutil.copy2(ffmpeg_root / "README.txt", notices / "FFmpeg-README.txt")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, notices / "Python-LICENSE.txt")

    import PyInstaller.__main__
    PyInstaller.__main__.run([
        str(ROOT / "launcher.py"), "--name", "StockDownloader", "--onedir", "--windowed",
        "--noconfirm", "--icon", str(icon), "--version-file", str(version_file),
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(stage), "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'assets'};assets",
        "--add-data", f"{client};flet-client",
        "--add-data", f"{notices};licenses",
        "--add-data", f"{ffmpeg};ffmpeg",
        "--hidden-import", "flet_desktop", "--collect-all", "flet_video",
        "--collect-data", "flet", "--collect-data", "certifi", "--copy-metadata", "flet",
        "--copy-metadata", "flet-desktop", "--copy-metadata", "flet-video",
    ])
    subprocess.run([str(args.iscc), str(ROOT / "packaging" / "installer.iss")], check=True)
    print(f"Built Stock Downloader v{APP_VERSION}")


if __name__ == "__main__":
    build()
