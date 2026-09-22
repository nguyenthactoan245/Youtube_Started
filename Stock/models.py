from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Video:
    id: int
    tags: str
    duration: int
    author: str
    page_url: str
    url: str
    thumbnail: str
    width: int
    height: int
    size: int


@dataclass(frozen=True)
class DownloadEvent:
    video_id: int
    state: str
    progress: float = 0.0
    message: str = ""

