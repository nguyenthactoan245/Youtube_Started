"""Desktop entry point for the installed Windows application."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def run() -> None:
    from app_config import DATA_ROOT, RESOURCE_ROOT

    if getattr(sys, "frozen", False):
        os.environ["FLET_VIEW_PATH"] = str(RESOURCE_ROOT / "flet-client")
        os.environ["PATH"] = str(RESOURCE_ROOT / "ffmpeg") + os.pathsep + os.environ.get("PATH", "")
        # Never let an unrelated working directory select a different Flet client.
        os.chdir(RESOURCE_ROOT)

    log_root = DATA_ROOT / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(
        handlers=[RotatingFileHandler(log_root / "stock.log", maxBytes=2_000_000,
                                      backupCount=2, encoding="utf-8")],
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # PyInstaller's windowed launcher has no standard streams.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(log_root / "stderr.log", "a", encoding="utf-8")

    import main
    from services.diagnostics import enable_logging
    enable_logging()
    main.ft.run(main.main, assets_dir=str(main.ASSETS_ROOT))


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logging.exception("Stock Downloader failed to start")
        raise
