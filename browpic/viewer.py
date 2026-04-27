"""Image/video viewer with zoom/pan and slideshow."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QSize, QUrl
from PySide6.QtGui import (
    QPixmap, QMovie, QKeySequence, QShortcut, QPainter, QImage, QWheelEvent,
)
from PySide6.QtWidgets import (
    QDialog, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QVBoxLayout, QLabel, QFileDialog,
)

from .media import FoundMedia, MediaKind


ZOOM_MIN = 0.1
ZOOM_MAX = 10.0
ZOOM_STEP = 1.2
DEFAULT_INTERVAL_S = 3
DEFAULT_VIDEO_MAX_S = 60


class MediaView(QGraphicsView):
    """A QGraphicsView that handles wheel-zoom (cursor-centered) and drag-pan."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setStyleSheet("background: #111; border: 0;")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._zoom = 1.0
        self._fit_scale = 1.0
        self._item = None
        self._movie: QMovie | None = None

    def show_pixmap(self, pix: QPixmap):
        self._stop_movie()
        self.scene().clear()
        self._item = QGraphicsPixmapItem(pix)
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self._item)
        self.scene().setSceneRect(QRectF(pix.rect()))
        self.fit_to_window()

    def show_movie(self, path: Path):
        self._stop_movie()
        self.scene().clear()
        movie = QMovie(str(path))
        if not movie.isValid():
            return
        movie.jumpToFrame(0)
        first = movie.currentPixmap()
        self._item = QGraphicsPixmapItem(first)
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self._item)
        self.scene().setSceneRect(QRectF(first.rect()))

        def on_frame():
            self._item.setPixmap(movie.currentPixmap())

        movie.frameChanged.connect(on_frame)
        movie.start()
        self._movie = movie
        self.fit_to_window()

    def _stop_movie(self):
        if self._movie is not None:
            self._movie.stop()
            self._movie = None

    def fit_to_window(self):
        if self._item is None:
            return
        self.resetTransform()
        rect = self._item.boundingRect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        vw = max(self.viewport().width(), 1)
        vh = max(self.viewport().height(), 1)
        self._fit_scale = min(vw / rect.width(), vh / rect.height())
        self.scale(self._fit_scale, self._fit_scale)
        self._zoom = self._fit_scale
        self.centerOn(self._item)

    def set_zoom_absolute(self, factor: float):
        if self._item is None:
            return
        factor = max(ZOOM_MIN, min(ZOOM_MAX, factor))
        self.resetTransform()
        self.scale(factor, factor)
        self._zoom = factor

    def zoom_step(self, mult: float):
        self.set_zoom_absolute(self._zoom * mult)

    def wheelEvent(self, e: QWheelEvent):
        if self._item is None:
            return
        mult = ZOOM_STEP if e.angleDelta().y() > 0 else 1 / ZOOM_STEP
        self.zoom_step(mult)
        e.accept()

    def mouseDoubleClickEvent(self, e):
        self.fit_to_window()


class Viewer(QDialog):
    def __init__(self, media: list[FoundMedia], start: int = 0,
                 parent=None, slideshow: bool = False,
                 interval_s: int = DEFAULT_INTERVAL_S,
                 video_max_s: int = DEFAULT_VIDEO_MAX_S):
        super().__init__(parent)
        self.setWindowTitle("BrowPic Viewer")
        self.media = media
        self.idx = start
        self.interval = interval_s
        self.video_max_s = video_max_s

        self.view = MediaView(self)
        self.view.setMinimumSize(800, 600)

        self._video_widget = None
        self._media_player = None

        self.zoom_label = QLabel(self)
        self.zoom_label.setStyleSheet(
            "background: rgba(0,0,0,160); color: #fff; padding: 3px 8px;"
            "border-radius: 4px;"
        )
        self.zoom_label.setVisible(False)
        self._zoom_hide_timer = QTimer(self)
        self._zoom_hide_timer.setSingleShot(True)
        self._zoom_hide_timer.timeout.connect(lambda: self.zoom_label.setVisible(False))

        self.info = QLabel(alignment=Qt.AlignCenter)
        self.info.setStyleSheet("color: #ddd; background: #111; padding: 4px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.info)

        self.advance_timer = QTimer(self)
        self.advance_timer.setSingleShot(True)
        self.advance_timer.timeout.connect(self.next)

        QShortcut(QKeySequence(Qt.Key_Right), self, self.next)
        QShortcut(QKeySequence(Qt.Key_Space), self, self.next)
        QShortcut(QKeySequence(Qt.Key_Left), self, self.prev)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.close)
        QShortcut(QKeySequence(Qt.Key_P), self, self.toggle_slideshow)
        QShortcut(QKeySequence(Qt.Key_F), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+S"), self, self.save_current)
        QShortcut(QKeySequence(Qt.Key_Plus), self, lambda: self._zoom_with_label(ZOOM_STEP))
        QShortcut(QKeySequence(Qt.Key_Minus), self, lambda: self._zoom_with_label(1 / ZOOM_STEP))
        QShortcut(QKeySequence(Qt.Key_0), self, self._reset_zoom)
        QShortcut(QKeySequence(Qt.Key_1), self, lambda: self._set_zoom_with_label(1.0))

        self.slideshow_active = slideshow
        self.show_current()
        if slideshow:
            self.showFullScreen()

    def show_current(self):
        if not self.media:
            return
        self.idx %= len(self.media)
        m = self.media[self.idx]
        self._stop_video()
        if m.kind == MediaKind.IMAGE:
            pix = QPixmap()
            if m.data:
                pix.loadFromData(m.data)
            else:
                pix.load(str(m.bytes_or_path()))
            self.view.show_pixmap(pix)
            self._maybe_schedule_advance(self.interval * 1000)
        elif m.kind == MediaKind.GIF:
            self.view.show_movie(m.bytes_or_path())
            self._maybe_schedule_advance(self.interval * 1000)
        else:
            self._play_video(m)

        self.info.setText(
            f"{self.idx + 1} / {len(self.media)}  ·  "
            f"{m.width}×{m.height}  ·  {m.kind.value}  ·  {m.url[:120]}"
        )
        self._reposition_zoom_label()

    def next(self):
        self.idx += 1
        self.show_current()

    def prev(self):
        self.idx -= 1
        self.show_current()

    def _zoom_with_label(self, mult: float):
        self.view.zoom_step(mult)
        self._show_zoom_label()

    def _set_zoom_with_label(self, factor: float):
        self.view.set_zoom_absolute(factor)
        self._show_zoom_label()

    def _reset_zoom(self):
        self.view.fit_to_window()

    def _show_zoom_label(self):
        pct = int(round(self.view._zoom * 100))
        self.zoom_label.setText(f"{pct}%")
        self.zoom_label.adjustSize()
        self._reposition_zoom_label()
        self.zoom_label.setVisible(True)
        self._zoom_hide_timer.start(1500)

    def _reposition_zoom_label(self):
        m = 12
        self.zoom_label.move(
            self.width() - self.zoom_label.width() - m,
            self.height() - self.zoom_label.height() - m - 28,
        )

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.view.fit_to_window()
        self._reposition_zoom_label()

    def toggle_slideshow(self):
        self.slideshow_active = not self.slideshow_active
        if self.slideshow_active:
            self.show_current()
        else:
            self.advance_timer.stop()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _maybe_schedule_advance(self, ms: int):
        self.advance_timer.stop()
        if self.slideshow_active:
            self.advance_timer.start(ms)

    def _play_video(self, m: FoundMedia):
        # Implemented in Task 9
        pass

    def _stop_video(self):
        # Implemented in Task 9
        pass

    def save_current(self):
        if not self.media:
            return
        m = self.media[self.idx]
        suggested = Path(urlparse(m.url).path).name or "media.bin"
        path, _ = QFileDialog.getSaveFileName(self, "Salvează", suggested)
        if path:
            if m.data:
                Path(path).write_bytes(m.data)
            elif m.cache_path:
                import shutil
                shutil.copyfile(m.cache_path, path)
