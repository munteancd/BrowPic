"""Download worker: fetches images/videos in parallel; extracts video thumbs."""
from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import httpx
from PIL import Image
from PySide6.QtCore import QThread, Signal

from .media import FoundMedia, MediaKind, CACHE_DIR

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BrowPic/2.0"
LARGE_FILE_THRESHOLD = 20 * 1024 * 1024  # 20 MB → stream to disk


def _video_thumbnail_jpeg(path: Path) -> bytes | None:
    """Return JPEG bytes of the first frame, or None on failure."""
    try:
        import imageio.v3 as iio3
        from PIL import Image as PImage
        frame = next(iio3.imiter(path, plugin="pyav"))
        im = PImage.fromarray(frame)
        buf = io.BytesIO()
        im.thumbnail((400, 400))
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        try:
            import subprocess, imageio_ffmpeg as iio
            ff = iio.get_ffmpeg_exe()
            proc = subprocess.run(
                [ff, "-y", "-i", str(path), "-vframes", "1", "-f", "image2pipe",
                 "-vcodec", "mjpeg", "-"],
                capture_output=True, timeout=15,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception:
            return None
    return None


class DownloadWorker(QThread):
    media_ready = Signal(int, object)
    thumbnail_ready = Signal(int, bytes)
    progress = Signal(int, int)
    done = Signal()

    def __init__(self, media: list[FoundMedia], min_dim: int):
        super().__init__()
        self._items = media
        self._min_dim = min_dim
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._fetch_all())
        finally:
            loop.close()
        self.done.emit()

    async def _fetch_all(self):
        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True, timeout=60,
        ) as client:
            await asyncio.gather(*(
                self._fetch_one(client, sem, i, m)
                for i, m in enumerate(self._items)
            ))

    async def _fetch_one(self, client, sem, idx: int, m: FoundMedia):
        if self._stopped:
            return
        async with sem:
            try:
                if m.kind == MediaKind.IMAGE or m.kind == MediaKind.GIF:
                    await self._fetch_image(client, idx, m)
                else:
                    await self._fetch_video(client, idx, m)
            except Exception:
                pass
            finally:
                self.progress.emit(idx + 1, len(self._items))

    async def _fetch_image(self, client, idx: int, m: FoundMedia):
        r = await client.get(m.url)
        if r.status_code != 200:
            return
        data = r.content
        try:
            im = Image.open(io.BytesIO(data))
            w, h = im.size
        except Exception:
            return
        if w < self._min_dim or h < self._min_dim:
            return
        m.data = data
        m.width = w
        m.height = h
        self.media_ready.emit(idx, m)

    async def _fetch_video(self, client, idx: int, m: FoundMedia):
        async with client.stream("GET", m.url) as r:
            if r.status_code != 200:
                return
            length = int(r.headers.get("content-length") or 0)
            stream_to_disk = length == 0 or length > LARGE_FILE_THRESHOLD
            if stream_to_disk:
                ext = ".mp4"
                h = hashlib.sha1(m.url.encode()).hexdigest()[:16]
                out = CACHE_DIR / f"{h}{ext}"
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with open(out, "wb") as f:
                    async for chunk in r.aiter_bytes(64 * 1024):
                        if self._stopped:
                            return
                        f.write(chunk)
                m.cache_path = out
                m.data = None
            else:
                m.data = await r.aread()
                m.cache_path = None
        if m.width and (m.width < self._min_dim or m.height < self._min_dim):
            return
        self.media_ready.emit(idx, m)
        path = m.cache_path or m.bytes_or_path()
        thumb = _video_thumbnail_jpeg(path)
        if thumb:
            self.thumbnail_ready.emit(idx, thumb)
