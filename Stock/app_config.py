"""Version, bundled resources and persistent per-user application data."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

APP_VERSION = "1.9.1"
RESOURCE_ROOT = Path(__file__).resolve().parent


def data_root() -> Path:
    override = os.environ.get("STOCK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        local_setting = os.environ.get("LOCALAPPDATA")
        local = Path(local_setting) if local_setting else Path.home() / "AppData" / "Local"
        return local / "StockDownloader"
    return RESOURCE_ROOT


DATA_ROOT = data_root()


def parse_api_keys(value: str) -> list[str]:
    keys = list(dict.fromkeys(k.strip() for k in re.split(r"[,\r\n]+", value) if k.strip()))
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in keys):
        raise ValueError("API key chỉ được chứa chữ, số, dấu gạch ngang và gạch dưới.")
    return keys


def configured_api_keys() -> list[str]:
    return parse_api_keys(os.getenv("PIXABAY_API_KEYS", "").strip()
                          or os.getenv("PIXABAY_API_KEY", ""))


def save_api_keys(value: str, root: Path = DATA_ROOT) -> list[str]:
    keys = parse_api_keys(value)
    root.mkdir(parents=True, exist_ok=True)
    target = root / ".env"
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip()
             not in {"PIXABAY_API_KEY", "PIXABAY_API_KEYS"}]
    lines.append("PIXABAY_API_KEYS=" + ",".join(keys))
    temporary = target.with_suffix(".env.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(target)
    os.environ["PIXABAY_API_KEYS"] = ",".join(keys)
    os.environ.pop("PIXABAY_API_KEY", None)
    return keys


def save_api_key(value: str, root: Path = DATA_ROOT) -> None:
    value = value.strip()
    if not value or any(char in value for char in "\r\n\"'"):
        raise ValueError("Vui lòng nhập API key hợp lệ.")
    root.mkdir(parents=True, exist_ok=True)
    target = root / ".env"
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip() != "PIXABAY_API_KEY"]
    lines.append(f"PIXABAY_API_KEY={value}")
    temporary = target.with_suffix(".env.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(target)
    os.environ["PIXABAY_API_KEY"] = value
