"""BrowPic - URL image grabber with thumbnail grid and slideshow."""
from __future__ import annotations

import io
import re
import sys
import json
import asyncio
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer, QObject
from PySide6.QtGui import QPixmap, QIcon, QKeySequence, QShortcut, QAction, QImage
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QListWidget, QListWidgetItem, QLabel,
    QProgressBar, QStatusBar, QFileDialog, QSpinBox, QDialog,
    QMessageBox, QCheckBox,
)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 BrowPic/1.0"
MIN_DIM_DEFAULT = 200
THUMB_SIZE = 200
CACHE_DIR = Path.home() / ".browpic_cache"
CACHE_DIR.mkdir(exist_ok=True)


@dataclass
class FoundImage:
    url: str
    data: bytes | None = None
    width: int = 0
    height: int = 0


# ---------- URL extraction ----------

def is_reddit(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("reddit.com") or host.endswith("redd.it")


async def extract_reddit(url: str) -> list[str]:
    """Use Reddit's .json endpoint to pull image URLs from a post or listing."""
    parsed = urlparse(url)
    path = parsed.path
    if not path.endswith(".json"):
        path = path.rstrip("/") + ".json"
    json_url = f"{parsed.scheme}://{parsed.netloc}{path}"
    if parsed.query:
        json_url += "?" + parsed.query

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
        r = await client.get(json_url)
        r.raise_for_status()
        data = r.json()

    urls: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            kind = node.get("kind")
            d = node.get("data") if isinstance(node.get("data"), dict) else None
            if kind == "t3" and d:
                # post
                if d.get("post_hint") == "image" and d.get("url"):
                    urls.append(d["url"])
                # gallery
                gallery = d.get("media_metadata")
                if isinstance(gallery, dict):
                    for m in gallery.values():
                        if not isinstance(m, dict):
                            continue
                        s = m.get("s") or {}
                        u = s.get("u") or s.get("gif")
                        if u:
                            urls.append(u.replace("&amp;", "&"))
                # preview images
                preview = d.get("preview")
                if isinstance(preview, dict):
                    for img in preview.get("images", []) or []:
                        src = (img.get("source") or {}).get("url")
                        if src and not any(src.replace("&amp;", "&") == u for u in urls):
                            urls.append(src.replace("&amp;", "&"))
                # direct image url fallback
                u = d.get("url_overridden_by_dest") or d.get("url")
                if u and re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", u, re.I):
                    urls.append(u)
            # recurse
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    # dedupe preserving order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def extract_generic(url: str) -> list[str]:
    """Render page with Playwright, scroll to trigger lazy-load, extract image URLs."""
    from playwright.async_api import async_playwright

    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str | None):
        if not u:
            return
        u = u.strip()
        if u.startswith("data:"):
            return
        if u in seen:
            return
        seen.add(u)
        urls.append(u)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=USER_AGENT)
            page = await ctx.new_page()

            # capture image responses too (catches background-image, css, etc.)
            def on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    if ct.startswith("image/"):
                        add(resp.url)
                except Exception:
                    pass
            page.on("response", on_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # scroll to bottom in chunks to trigger lazy-load
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(400)

            html = await page.content()
            base = page.url

            # explicit <img> tags including srcset
            imgs = await page.query_selector_all("img")
            for img in imgs:
                for attr in ("src", "data-src", "data-original", "data-lazy-src"):
                    v = await img.get_attribute(attr)
                    if v:
                        add(urljoin(base, v))
                srcset = await img.get_attribute("srcset")
                if srcset:
                    # take last (largest) candidate
                    parts = [p.strip() for p in srcset.split(",") if p.strip()]
                    if parts:
                        last = parts[-1].split()[0]
                        add(urljoin(base, last))

            # background-image via inline style
            soup = BeautifulSoup(html, "html.parser")
            for el in soup.find_all(style=True):
                style = el["style"] or ""
                for m in re.finditer(r"url\(['\"]?([^'\")]+)['\"]?\)", style):
                    add(urljoin(base, m.group(1)))

            await browser.close()
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    return urls


# ---------- background workers ----------

class ExtractWorker(QThread):
    finished_with = Signal(list, str)  # urls, error

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            if is_reddit(self.url):
                urls = loop.run_until_complete(extract_reddit(self.url))
            else:
                urls = loop.run_until_complete(extract_generic(self.url))
            loop.close()
            self.finished_with.emit(urls, "")
        except Exception as e:
            self.finished_with.emit([], f"{type(e).__name__}: {e}")


class DownloadWorker(QThread):
    image_ready = Signal(int, object)  # index, FoundImage
    progress = Signal(int, int)
    done = Signal()

    def __init__(self, urls: list[str], min_dim: int):
        super().__init__()
        self.urls = urls
        self.min_dim = min_dim
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        async def fetch_all():
            sem = asyncio.Semaphore(8)
            headers = {"User-Agent": USER_AGENT}
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
                async def fetch(i: int, u: str):
                    if self._stop:
                        return
                    async with sem:
                        try:
                            r = await client.get(u)
                            if r.status_code != 200:
                                return
                            data = r.content
                            try:
                                im = Image.open(io.BytesIO(data))
                                w, h = im.size
                            except Exception:
                                return
                            if w < self.min_dim or h < self.min_dim:
                                return
                            self.image_ready.emit(i, FoundImage(url=u, data=data, width=w, height=h))
                        except Exception:
                            return
                        finally:
                            self.progress.emit(i + 1, len(self.urls))

                await asyncio.gather(*(fetch(i, u) for i, u in enumerate(self.urls)))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(fetch_all())
        finally:
            loop.close()
        self.done.emit()


# ---------- viewer / slideshow ----------

class Viewer(QDialog):
    def __init__(self, images: list[FoundImage], start: int = 0, parent=None, slideshow: bool = False, interval_s: int = 3):
        super().__init__(parent)
        self.setWindowTitle("BrowPic Viewer")
        self.images = images
        self.idx = start
        self.slideshow_on = slideshow
        self.interval = interval_s

        self.label = QLabel(alignment=Qt.AlignCenter)
        self.label.setStyleSheet("background: #111;")
        self.label.setMinimumSize(800, 600)

        self.info = QLabel(alignment=Qt.AlignCenter)
        self.info.setStyleSheet("color: #ddd; background: #111; padding: 4px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.info)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next)

        QShortcut(QKeySequence(Qt.Key_Right), self, self.next)
        QShortcut(QKeySequence(Qt.Key_Space), self, self.next)
        QShortcut(QKeySequence(Qt.Key_Left), self, self.prev)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.close)
        QShortcut(QKeySequence(Qt.Key_P), self, self.toggle_slideshow)
        QShortcut(QKeySequence(Qt.Key_F), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+S"), self, self.save_current)

        self.show_image()
        if slideshow:
            self.showFullScreen()
            self.timer.start(self.interval * 1000)

    def show_image(self):
        if not self.images:
            return
        self.idx %= len(self.images)
        img = self.images[self.idx]
        pix = QPixmap()
        pix.loadFromData(img.data)
        scaled = pix.scaled(self.label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.label.setPixmap(scaled)
        self.info.setText(f"{self.idx + 1} / {len(self.images)}  ·  {img.width}×{img.height}  ·  {img.url[:120]}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.show_image()

    def next(self):
        self.idx += 1
        self.show_image()

    def prev(self):
        self.idx -= 1
        self.show_image()

    def toggle_slideshow(self):
        if self.timer.isActive():
            self.timer.stop()
        else:
            self.timer.start(self.interval * 1000)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def save_current(self):
        if not self.images:
            return
        img = self.images[self.idx]
        suggested = Path(urlparse(img.url).path).name or "image.jpg"
        path, _ = QFileDialog.getSaveFileName(self, "Salvează imaginea", suggested)
        if path:
            Path(path).write_bytes(img.data)


# ---------- main window ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BrowPic")
        self.resize(1100, 750)

        self.images: list[FoundImage] = []
        self.extract_worker: ExtractWorker | None = None
        self.dl_worker: DownloadWorker | None = None

        # top bar
        self.url_edit = QLineEdit(placeholderText="Lipește un URL (reddit, blog, galerie etc.) și apasă Enter")
        self.url_edit.returnPressed.connect(self.on_load)
        self.load_btn = QPushButton("Încarcă")
        self.load_btn.clicked.connect(self.on_load)

        self.min_dim_spin = QSpinBox()
        self.min_dim_spin.setRange(0, 4000)
        self.min_dim_spin.setValue(MIN_DIM_DEFAULT)
        self.min_dim_spin.setSuffix(" px")
        self.min_dim_spin.setToolTip("Dimensiune minimă (lățime sau înălțime)")

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 60)
        self.interval_spin.setValue(3)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setToolTip("Interval slideshow")

        self.slideshow_btn = QPushButton("▶ Slideshow")
        self.slideshow_btn.clicked.connect(self.start_slideshow)
        self.slideshow_btn.setEnabled(False)

        self.save_all_btn = QPushButton("💾 Salvează tot")
        self.save_all_btn.clicked.connect(self.save_all)
        self.save_all_btn.setEnabled(False)

        top = QHBoxLayout()
        top.addWidget(self.url_edit, 1)
        top.addWidget(self.load_btn)
        top.addWidget(QLabel("Min:"))
        top.addWidget(self.min_dim_spin)
        top.addWidget(QLabel("Interval:"))
        top.addWidget(self.interval_spin)
        top.addWidget(self.slideshow_btn)
        top.addWidget(self.save_all_btn)

        # grid
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setSpacing(8)
        self.list.setMovement(QListWidget.Static)
        self.list.itemDoubleClicked.connect(self.open_viewer)

        central = QWidget()
        v = QVBoxLayout(central)
        v.addLayout(top)
        v.addWidget(self.list, 1)
        self.setCentralWidget(central)

        # status
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        sb = QStatusBar()
        sb.addPermanentWidget(self.progress)
        self.setStatusBar(sb)
        self.status("Gata. Lipește un URL.")

    def status(self, msg: str):
        self.statusBar().showMessage(msg)

    def on_load(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_edit.setText(url)

        self.list.clear()
        self.images.clear()
        self.slideshow_btn.setEnabled(False)
        self.save_all_btn.setEnabled(False)
        self.load_btn.setEnabled(False)
        self.status(f"Extrag URL-uri din {url} ...")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # busy

        self.extract_worker = ExtractWorker(url)
        self.extract_worker.finished_with.connect(self.on_extracted)
        self.extract_worker.start()

    def on_extracted(self, urls: list[str], err: str):
        self.load_btn.setEnabled(True)
        if err:
            self.progress.setVisible(False)
            self.status("Eroare la extragere.")
            QMessageBox.warning(self, "Eroare", err)
            return
        if not urls:
            self.progress.setVisible(False)
            self.status("Nicio imagine găsită.")
            return

        self.status(f"Găsite {len(urls)} URL-uri. Descarc...")
        self.progress.setRange(0, len(urls))
        self.progress.setValue(0)

        # placeholders
        self._slot_for_index = {}
        for i, u in enumerate(urls):
            item = QListWidgetItem(QIcon(), "")
            item.setSizeHint(QSize(THUMB_SIZE + 16, THUMB_SIZE + 16))
            item.setData(Qt.UserRole, -1)  # not loaded yet
            self.list.addItem(item)
            self._slot_for_index[i] = item

        self.dl_worker = DownloadWorker(urls, self.min_dim_spin.value())
        self.dl_worker.image_ready.connect(self.on_image)
        self.dl_worker.progress.connect(self.on_progress)
        self.dl_worker.done.connect(self.on_dl_done)
        self.dl_worker.start()

    def on_progress(self, done: int, total: int):
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def on_image(self, idx: int, img: FoundImage):
        # add to images list, build thumbnail
        self.images.append(img)
        new_index = len(self.images) - 1

        qimg = QImage.fromData(img.data)
        pix = QPixmap.fromImage(qimg).scaled(
            THUMB_SIZE, THUMB_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )

        # reuse the placeholder slot for this URL index
        item = self._slot_for_index.get(idx)
        if item is None:
            item = QListWidgetItem()
            item.setSizeHint(QSize(THUMB_SIZE + 16, THUMB_SIZE + 16))
            self.list.addItem(item)
        item.setIcon(QIcon(pix))
        item.setToolTip(f"{img.width}×{img.height}\n{img.url}")
        item.setData(Qt.UserRole, new_index)

    def on_dl_done(self):
        # remove unfilled placeholders (filtered out or failed)
        for i in range(self.list.count() - 1, -1, -1):
            item = self.list.item(i)
            if item.data(Qt.UserRole) == -1:
                self.list.takeItem(i)

        self.progress.setVisible(False)
        self.status(f"{len(self.images)} imagini afișate.")
        if self.images:
            self.slideshow_btn.setEnabled(True)
            self.save_all_btn.setEnabled(True)

    def open_viewer(self, item: QListWidgetItem):
        idx = item.data(Qt.UserRole)
        if idx is None or idx < 0:
            return
        v = Viewer(self.images, start=idx, parent=self, interval_s=self.interval_spin.value())
        v.exec()

    def start_slideshow(self):
        if not self.images:
            return
        v = Viewer(self.images, start=0, parent=self, slideshow=True, interval_s=self.interval_spin.value())
        v.exec()

    def save_all(self):
        if not self.images:
            return
        folder = QFileDialog.getExistingDirectory(self, "Alege folder pentru salvare")
        if not folder:
            return
        out = Path(folder)
        n = 0
        for img in self.images:
            name = Path(urlparse(img.url).path).name
            if not name or "." not in name:
                name = hashlib.md5(img.url.encode()).hexdigest()[:10] + ".jpg"
            target = out / name
            i = 1
            while target.exists():
                target = out / f"{target.stem}_{i}{target.suffix}"
                i += 1
            try:
                target.write_bytes(img.data)
                n += 1
            except Exception:
                pass
        self.status(f"Salvate {n} imagini în {folder}.")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("BrowPic")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
