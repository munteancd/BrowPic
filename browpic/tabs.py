"""GalleryTab: one tab = one source URL with its own paginator and grid."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QIcon, QPixmap, QImage
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QCheckBox,
    QListWidget, QListWidgetItem, QLabel,
)

from .media import FoundMedia, MediaKind
from .extractors import (
    is_reddit, extract_reddit, extract_generic, resolve_external_gallery,
    RedditAfterCursor, PlaywrightScrollCursor, EXTERNAL_GALLERY_PATTERNS,
)

THUMB_SIZE = 200


class SourceKind(Enum):
    REDDIT_LISTING = "reddit_listing"
    REDDIT_POST = "reddit_post"
    GENERIC = "generic"


class TabStatus(Enum):
    IDLE = "idle"
    LOADING = "loading"
    DONE = "done"
    ERROR = "error"


def classify_source(url: str) -> SourceKind:
    if is_reddit(url):
        path = urlparse(url).path
        if "/comments/" in path:
            return SourceKind.REDDIT_POST
        return SourceKind.REDDIT_LISTING
    return SourceKind.GENERIC


def short_title(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.replace("www.", "")
    if "reddit.com" in host:
        parts = [s for s in p.path.split("/") if s]
        if len(parts) >= 2 and parts[0] == "r":
            sub = "r/" + parts[1]
            return sub
    return host or url


class GalleryTab(QWidget):
    title_changed = Signal(str)
    request_open_viewer = Signal(int)
    status_message = Signal(str)

    def __init__(self, source_url: str, runner, browser_pool, settings: dict, parent=None):
        super().__init__(parent)
        self.source_url = source_url
        self.kind = classify_source(source_url)
        self.runner = runner
        self.pool = browser_pool
        self.settings = settings
        self.media: list[FoundMedia] = []
        self.seen_urls: set[str] = set()
        self.paginator = None
        self.status = TabStatus.IDLE
        self.tab_id = f"tab_{id(self)}"

        self.list = QListWidget(viewMode=QListWidget.IconMode)
        self.list.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setSpacing(8)
        self.list.setMovement(QListWidget.Static)
        self.list.itemDoubleClicked.connect(self._on_item_dbl)

        self.load_more_btn = QPushButton("Încarcă mai multe")
        self.load_more_btn.setEnabled(False)
        self.load_more_btn.clicked.connect(self.load_next_page)

        self.follow_chk = QCheckBox("Follow external galleries")

        bottom = QHBoxLayout()
        bottom.addWidget(self.load_more_btn)
        bottom.addWidget(self.follow_chk)
        bottom.addStretch()

        v = QVBoxLayout(self)
        v.addWidget(self.list, 1)
        v.addLayout(bottom)

    def start_load(self):
        self._set_status(TabStatus.LOADING)
        self._fetch_page(initial=True)

    def load_next_page(self):
        if self.paginator is None or self.status == TabStatus.LOADING:
            return
        self._set_status(TabStatus.LOADING)
        self._fetch_page(initial=False)

    def set_follow_external(self, on: bool):
        self.follow_chk.setChecked(on)

    def get_session_dict(self) -> dict:
        return {"url": self.source_url, "follow_external": self.follow_chk.isChecked()}

    def _set_status(self, s: TabStatus):
        self.status = s
        self._update_title()

    def _update_title(self):
        base = short_title(self.source_url)
        badge = ""
        if self.status == TabStatus.LOADING:
            badge = " ⏳"
        elif self.status == TabStatus.ERROR:
            badge = " ⚠"
        elif self.status == TabStatus.DONE and self.media:
            badge = f" ✓{len(self.media)}"
        self.title_changed.emit(base + badge)

    def _fetch_page(self, initial: bool):
        async def do_extract():
            if self.kind in (SourceKind.REDDIT_LISTING, SourceKind.REDDIT_POST):
                cur = self.paginator if isinstance(self.paginator, RedditAfterCursor) else None
                return await extract_reddit(self.source_url, after=cur)
            else:
                ctx = await self.pool.acquire(self.tab_id)
                cur = self.paginator if isinstance(self.paginator, PlaywrightScrollCursor) else None
                return await extract_generic(self.source_url, ctx, cursor=cur)

        fut = self.runner.submit(do_extract())

        def on_done(f):
            try:
                media, cursor = f.result()
            except Exception as e:
                self.status_message.emit(f"Eroare: {type(e).__name__}: {e}")
                self._set_status(TabStatus.ERROR)
                return
            new = []
            for m in media:
                if m.url in self.seen_urls:
                    continue
                self.seen_urls.add(m.url)
                new.append(m)
            self.paginator = cursor
            self._start_download(new)
            if self.follow_chk.isChecked():
                self._fan_out_external(new)

        fut.add_done_callback(lambda f: self._post(lambda: on_done(f)))

    def _post(self, fn):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, fn)

    def _start_download(self, media: list[FoundMedia]):
        from .downloader import DownloadWorker
        if not media:
            self._set_status(TabStatus.DONE)
            self.load_more_btn.setEnabled(self.paginator is not None)
            return
        first_index = self.list.count()
        for _ in media:
            item = QListWidgetItem()
            item.setSizeHint(QSize(THUMB_SIZE + 16, THUMB_SIZE + 16))
            item.setData(Qt.UserRole, -1)
            self.list.addItem(item)

        self._dl = DownloadWorker(media, self.settings.get("min_dim", 200))
        self._dl_offset = first_index

        def on_ready(idx, m: FoundMedia):
            self.media.append(m)
            new_index = len(self.media) - 1
            item = self.list.item(self._dl_offset + idx)
            if item is None:
                return
            if m.kind == MediaKind.IMAGE or m.kind == MediaKind.GIF:
                qimg = QImage.fromData(m.data) if m.data else QImage(str(m.bytes_or_path()))
                pix = QPixmap.fromImage(qimg).scaled(
                    THUMB_SIZE, THUMB_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                item.setIcon(QIcon(pix))
            else:
                item.setIcon(_video_placeholder_icon())
            item.setToolTip(f"{m.kind.value} {m.width}×{m.height}\n{m.url}")
            item.setData(Qt.UserRole, new_index)
            self._update_title()

        def on_thumb(idx, jpeg: bytes):
            item = self.list.item(self._dl_offset + idx)
            if item is None:
                return
            pix = QPixmap()
            pix.loadFromData(jpeg)
            pix = pix.scaled(THUMB_SIZE, THUMB_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            item.setIcon(QIcon(_overlay_play(pix)))

        def on_done():
            for i in range(self.list.count() - 1, -1, -1):
                it = self.list.item(i)
                if it.data(Qt.UserRole) == -1:
                    self.list.takeItem(i)
            self._set_status(TabStatus.DONE)
            self.load_more_btn.setEnabled(self.paginator is not None)
            self._update_title()

        self._dl.media_ready.connect(on_ready)
        self._dl.thumbnail_ready.connect(on_thumb)
        self._dl.done.connect(on_done)
        self._dl.start()

    def _fan_out_external(self, media: list[FoundMedia]):
        targets: list[str] = []
        for m in media:
            for pattern, _ in EXTERNAL_GALLERY_PATTERNS:
                if pattern.match(m.url):
                    targets.append(m.url)
                    break
            if len(targets) >= 20:
                break
        if not targets:
            return

        async def do_resolve():
            results: list[FoundMedia] = []
            for u in targets:
                try:
                    res = await resolve_external_gallery(u)
                    results.extend(res)
                except Exception:
                    continue
            return results

        fut = self.runner.submit(do_resolve())

        def on_done(f):
            try:
                external = f.result()
            except Exception:
                return
            new = [m for m in external if m.url not in self.seen_urls]
            for m in new:
                self.seen_urls.add(m.url)
            self._start_download(new)

        fut.add_done_callback(lambda f: self._post(lambda: on_done(f)))

    def _on_item_dbl(self, item: QListWidgetItem):
        idx = item.data(Qt.UserRole)
        if idx is None or idx < 0:
            return
        self.request_open_viewer.emit(idx)


def _video_placeholder_icon() -> QIcon:
    pix = QPixmap(THUMB_SIZE, THUMB_SIZE)
    pix.fill(Qt.darkGray)
    return QIcon(_overlay_play(pix))


def _overlay_play(pix: QPixmap) -> QPixmap:
    from PySide6.QtGui import QPainter, QColor, QPolygonF
    from PySide6.QtCore import QPointF
    out = QPixmap(pix)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(0, 0, 0, 160))
    p.setPen(Qt.NoPen)
    cx, cy = out.width() / 2, out.height() / 2
    r = min(out.width(), out.height()) / 6
    p.drawEllipse(QPointF(cx, cy), r * 1.4, r * 1.4)
    p.setBrush(QColor(255, 255, 255, 230))
    tri = QPolygonF([
        QPointF(cx - r * 0.5, cy - r * 0.7),
        QPointF(cx - r * 0.5, cy + r * 0.7),
        QPointF(cx + r * 0.8, cy),
    ])
    p.drawPolygon(tri)
    p.end()
    return out
