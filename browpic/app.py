"""MainWindow + entry point."""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QSpinBox, QLabel, QStatusBar, QProgressBar,
    QTabWidget, QFileDialog, QMessageBox, QMenu,
)

from .browser_pool import AsyncRunner, BrowserPool
from .cache import (
    SessionState, load_session, save_session, lru_cleanup,
    SESSION_PATH, MEDIA_DIR,
)
from .tabs import GalleryTab, TabStatus
from .viewer import Viewer


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BrowPic")
        self.resize(1200, 800)

        self.session = load_session()
        lru_cleanup(MEDIA_DIR)

        self.runner = AsyncRunner()
        self.pool = BrowserPool(idle_timeout_s=300)

        self.url_edit = QLineEdit(placeholderText="URL (Enter = current tab, Shift+Enter = new tab)")
        self.url_edit.returnPressed.connect(self._on_url_enter)
        self.load_btn = QPushButton("Încarcă")
        self.load_btn.clicked.connect(lambda: self._open_url(new_tab=False))
        self.new_tab_btn = QPushButton("➕ Tab nou")
        self.new_tab_btn.clicked.connect(lambda: self._open_url(new_tab=True))

        self.min_dim = QSpinBox()
        self.min_dim.setRange(0, 4000)
        self.min_dim.setValue(self.session.settings.get("min_dim", 200))
        self.min_dim.setSuffix(" px")

        self.interval = QSpinBox()
        self.interval.setRange(1, 60)
        self.interval.setValue(self.session.settings.get("interval_s", 3))
        self.interval.setSuffix(" s")

        self.video_max = QSpinBox()
        self.video_max.setRange(5, 600)
        self.video_max.setValue(self.session.settings.get("video_max_s", 60))
        self.video_max.setSuffix(" s")
        self.video_max.setToolTip("Cap maxim video în slideshow")

        self.slideshow_btn = QPushButton("▶ Slideshow")
        self.slideshow_btn.clicked.connect(self._slideshow_current)
        self.slideshow_all_btn = QPushButton("▶ Toate tab-urile")
        self.slideshow_all_btn.clicked.connect(self._slideshow_all)
        self.save_btn = QPushButton("💾 Salvează tot")
        self.save_btn.clicked.connect(self._save_current_tab)

        top = QHBoxLayout()
        top.addWidget(self.url_edit, 1)
        top.addWidget(self.load_btn)
        top.addWidget(self.new_tab_btn)
        top.addWidget(QLabel("Min:"))
        top.addWidget(self.min_dim)
        top.addWidget(QLabel("Interval:"))
        top.addWidget(self.interval)
        top.addWidget(QLabel("Cap video:"))
        top.addWidget(self.video_max)
        top.addWidget(self.slideshow_btn)
        top.addWidget(self.slideshow_all_btn)
        top.addWidget(self.save_btn)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_context_menu)

        central = QWidget()
        v = QVBoxLayout(central)
        v.addLayout(top)
        v.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        sb = QStatusBar()
        sb.addPermanentWidget(self.progress)
        self.setStatusBar(sb)

        from PySide6.QtCore import QTimer
        self._evict_timer = QTimer(self)
        self._evict_timer.timeout.connect(self._evict_idle)
        self._evict_timer.start(60_000)

        for t in self.session.tabs:
            self._add_tab(t["url"], follow_external=bool(t.get("follow_external")))

        QShortcut(QKeySequence("Shift+Return"), self,
                  lambda: self._open_url(new_tab=True))

    def _on_url_enter(self):
        mods = QApplication.keyboardModifiers()
        new_tab = bool(mods & Qt.ShiftModifier)
        self._open_url(new_tab=new_tab)

    def _open_url(self, new_tab: bool):
        url = self.url_edit.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if new_tab or self.tabs.count() == 0:
            tab = self._add_tab(url)
        else:
            tab = self.tabs.currentWidget()
            if not isinstance(tab, GalleryTab):
                tab = self._add_tab(url)
            else:
                tab.source_url = url
                tab.media.clear()
                tab.seen_urls.clear()
                tab.list.clear()
                tab.paginator = None
        tab.start_load()

    def _add_tab(self, url: str, follow_external: bool = False) -> GalleryTab:
        settings = {"min_dim": self.min_dim.value()}
        tab = GalleryTab(url, self.runner, self.pool, settings)
        tab.set_follow_external(follow_external)
        tab.title_changed.connect(lambda t, w=tab: self._set_tab_title(w, t))
        tab.request_open_viewer.connect(lambda i, w=tab: self._open_viewer(w, i))
        tab.status_message.connect(lambda msg: self.statusBar().showMessage(msg, 6000))
        idx = self.tabs.addTab(tab, url)
        self.tabs.setCurrentIndex(idx)
        return tab

    def _set_tab_title(self, tab: GalleryTab, title: str):
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, title)

    def _close_tab(self, index: int):
        w = self.tabs.widget(index)
        if isinstance(w, GalleryTab):
            self.runner.submit(self.pool.release(w.tab_id))
        self.tabs.removeTab(index)

    def _tab_context_menu(self, pos):
        bar = self.tabs.tabBar()
        idx = bar.tabAt(pos)
        if idx < 0:
            return
        m = QMenu(self)
        a_close = m.addAction("Închide")
        a_others = m.addAction("Închide celelalte")
        a_dup = m.addAction("Duplică")
        a_copy = m.addAction("Copiază URL")
        chosen = m.exec(bar.mapToGlobal(pos))
        if chosen == a_close:
            self._close_tab(idx)
        elif chosen == a_others:
            for i in range(self.tabs.count() - 1, -1, -1):
                if i != idx:
                    self._close_tab(i)
        elif chosen == a_dup:
            w = self.tabs.widget(idx)
            if isinstance(w, GalleryTab):
                self._add_tab(w.source_url, follow_external=w.follow_chk.isChecked()).start_load()
        elif chosen == a_copy:
            w = self.tabs.widget(idx)
            if isinstance(w, GalleryTab):
                QApplication.clipboard().setText(w.source_url)

    def _open_viewer(self, tab: GalleryTab, index: int):
        v = Viewer(
            tab.media, start=index, parent=self,
            interval_s=self.interval.value(),
            video_max_s=self.video_max.value(),
        )
        v.exec()

    def _slideshow_current(self):
        w = self.tabs.currentWidget()
        if not isinstance(w, GalleryTab) or not w.media:
            return
        v = Viewer(
            w.media, start=0, parent=self, slideshow=True,
            interval_s=self.interval.value(),
            video_max_s=self.video_max.value(),
        )
        v.exec()

    def _slideshow_all(self):
        combined = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, GalleryTab):
                combined.extend(w.media)
        if not combined:
            return
        v = Viewer(
            combined, start=0, parent=self, slideshow=True,
            interval_s=self.interval.value(),
            video_max_s=self.video_max.value(),
        )
        v.exec()

    def _save_current_tab(self):
        import hashlib
        w = self.tabs.currentWidget()
        if not isinstance(w, GalleryTab) or not w.media:
            return
        folder = QFileDialog.getExistingDirectory(self, "Alege folder")
        if not folder:
            return
        out = Path(folder)
        n = 0
        for m in w.media:
            name = Path(urlparse(m.url).path).name
            if not name or "." not in name:
                name = hashlib.md5(m.url.encode()).hexdigest()[:10] + ".bin"
            target = out / name
            i = 1
            while target.exists():
                target = out / f"{target.stem}_{i}{target.suffix}"
                i += 1
            try:
                if m.data:
                    target.write_bytes(m.data)
                elif m.cache_path:
                    import shutil
                    shutil.copyfile(m.cache_path, target)
                n += 1
            except Exception:
                pass
        self.statusBar().showMessage(f"Salvate {n} fișiere în {folder}", 6000)

    def _evict_idle(self):
        self.runner.submit(self.pool.evict_idle())

    def closeEvent(self, e):
        tabs = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, GalleryTab):
                tabs.append(w.get_session_dict())
        s = SessionState(
            tabs=tabs,
            settings={
                "min_dim": self.min_dim.value(),
                "interval_s": self.interval.value(),
                "video_max_s": self.video_max.value(),
            },
        )
        save_session(s)
        try:
            self.runner.submit(self.pool.shutdown()).result(timeout=5)
        except Exception:
            pass
        self.runner.stop()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("BrowPic")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
