"""Media model used across the app."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

CACHE_DIR = Path.home() / ".browpic_cache" / "media"


class MediaKind(str, Enum):
    IMAGE = "image"
    GIF = "gif"
    VIDEO = "video"


_VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".gifv"}
_GIF_EXT = {".gif"}


def kind_from_url(url: str) -> MediaKind:
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|#|$)", url)
    if not m:
        return MediaKind.IMAGE
    ext = "." + m.group(1).lower()
    if ext in _VIDEO_EXT:
        return MediaKind.VIDEO
    if ext in _GIF_EXT:
        return MediaKind.GIF
    return MediaKind.IMAGE


def _safe_ext(url: str, default: str) -> str:
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|#|$)", url)
    return "." + m.group(1).lower() if m else default


@dataclass
class FoundMedia:
    url: str
    kind: MediaKind
    data: Optional[bytes]
    cache_path: Optional[Path]
    width: int
    height: int
    duration_s: float = 0.0
    source_url: str = ""
    is_external: bool = False

    def bytes_or_path(self) -> Path:
        """Return a Path that QPixmap/QMovie/QMediaPlayer can read."""
        if self.cache_path is not None:
            return self.cache_path
        if self.data is None:
            raise ValueError("FoundMedia has neither data nor cache_path")
        ext = _safe_ext(self.url, ".bin")
        h = hashlib.sha1(self.url.encode()).hexdigest()[:16]
        out = CACHE_DIR / f"tmp_{h}{ext}"
        if not out.exists():
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            out.write_bytes(self.data)
        self.cache_path = out
        return out
