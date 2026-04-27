"""Disk cache and session persistence."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS = {"min_dim": 200, "interval_s": 3, "video_max_s": 60}
APP_CACHE_ROOT = Path.home() / ".browpic_cache"
SESSION_PATH = APP_CACHE_ROOT / "session.json"
MEDIA_DIR = APP_CACHE_ROOT / "media"
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


@dataclass
class SessionState:
    tabs: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))


def load_session(path: Path = SESSION_PATH) -> SessionState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        s = SessionState(
            tabs=list(raw.get("tabs", [])),
            settings={**DEFAULT_SETTINGS, **dict(raw.get("settings", {}))},
        )
        return s
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return SessionState()


def save_session(state: SessionState, path: Path = SESSION_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    tmp.replace(path)


def lru_cleanup(media_dir: Path = MEDIA_DIR, max_bytes: int = MAX_CACHE_BYTES) -> int:
    """Delete oldest files (by atime) until total size <= max_bytes.
    Returns number of files deleted.
    """
    if not media_dir.exists():
        return 0
    files = []
    total = 0
    for p in media_dir.iterdir():
        if p.is_file():
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_atime, st.st_size, p))
            total += st.st_size
    if total <= max_bytes:
        return 0
    files.sort(key=lambda t: t[0])  # oldest first
    deleted = 0
    for _, size, p in files:
        if total <= max_bytes:
            break
        try:
            p.unlink()
            deleted += 1
            total -= size
        except OSError:
            continue
    return deleted
