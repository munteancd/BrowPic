# BrowPic v2 — Design

**Date:** 2026-04-27
**Status:** Approved (awaiting user spec review)

## Goal

Add four major features to BrowPic v1: multi-URL tabs, continuous loading
with optional external-gallery following, video/GIF support with playback,
and zoom/pan in the viewer. Refactor the single-file v1 into a small
package so the new features fit cleanly.

## Non-Goals

- Audio playback for videos (Reddit/imgur audio omitted to skip ffmpeg merging)
- Cloud sync, sharing, multi-user
- Editing/cropping images
- Tags, favorites, search history (deferred to a later iteration)
- Mobile/non-Windows packaging

## File Structure

```
BrowPic/
  browpic/
    __init__.py
    app.py            # MainWindow, QApplication entry
    tabs.py           # QTabWidget container + GalleryTab widget
    extractors.py     # extract_reddit, extract_generic, paginators, external-gallery resolvers
    downloader.py     # DownloadWorker (images + video, streaming for large files)
    viewer.py         # QGraphicsView-based viewer with zoom/pan + video playback
    media.py          # FoundMedia dataclass, kind enums
    browser_pool.py   # Per-tab Playwright BrowserContext cache with idle timeout
    cache.py          # Disk cache helpers (LRU, session.json read/write)
  browpic.py          # entry point: from browpic.app import main; main()
  requirements.txt
  setup.bat / run.bat
  docs/superpowers/specs/2026-04-27-browpic-v2-design.md
```

Each module stays under ~300 lines. v1's logic is split across `extractors.py`,
`downloader.py`, `viewer.py` with light edits.

## Components

### MainWindow (app.py)

- Top toolbar: URL input, "Încarcă" (loads in current tab), "➕ Tab nou" (loads in new tab),
  min-dim spinner, slideshow-interval spinner, "▶ Slideshow" (current tab),
  "▶ Slideshow combinat" (all tabs), "💾 Salvează tot" (current tab).
- Center: `QTabWidget` containing one `GalleryTab` per source URL.
- Status bar: progress bar + message.
- Pressing Enter in URL bar with Shift → new tab; without Shift → replace current tab.

### GalleryTab (tabs.py)

State per tab:
- `source_url: str`
- `source_kind: SourceKind` — enum: `reddit_listing | reddit_post | imgur_album | generic | finite_gallery`
- `paginator: Paginator | None` — per-kind cursor (e.g., `RedditAfterCursor("t3_xxx")`,
  `PlaywrightScrollCursor(scroll_y=12000)`); `None` means no more pages
- `media: list[FoundMedia]`
- `seen_urls: set[str]` — dedup
- `follow_external: bool`
- `status: Literal["idle", "loading", "done", "error"]`

UI:
- Top: tab title with badge (⏳ / ✓N / ⚠), close button
- Center: thumbnail grid (`QListWidget` icon mode, 200×200, video items get a ▶ overlay)
- Bottom: "Încarcă mai multe" button (disabled when `paginator is None` or status == loading),
  "☐ Follow external galleries" checkbox

Tab title resolver:
- Reddit listing → `r/<sub>`
- Reddit post → `r/<sub>: <title-truncated-30>`
- imgur album → `imgur.com/a/<id>`
- generic → hostname

Right-click on tab header: Close, Close others, Duplicate, Copy URL.

### Extractors (extractors.py)

Stateless functions. Each returns `(media: list[FoundMedia], next_paginator | None)`.

- `extract_reddit(url, after=None)` — JSON API, paginated 25/batch, handles t3 posts,
  galleries (`media_metadata`), v.redd.it videos, gif previews.
- `extract_generic(url, browser_context, scroll_from=0)` — uses an existing Playwright
  context from the pool, scrolls in chunks (8 × innerHeight), extracts `<img>` (src,
  data-*, srcset last entry), `<video>`/`<source>`, CSS `background-image`,
  and any response with `content-type: image/* | video/*`.
- `resolve_external_gallery(url) → list[FoundMedia]` — pattern-matches host and
  delegates to `_imgur_album`, `_redgifs`, `_flickr_album`, etc. List of supported
  patterns lives at top of module as a constant for easy extension.
- External gallery cap: max 20 per fetch, surfaced via progress bar.

### Browser Pool (browser_pool.py)

- `dict[tab_id → (BrowserContext, last_used_at)]` guarded by `asyncio.Lock`.
- `acquire(tab_id) → context` (creates if missing).
- Background `QTimer` fires every 60s; closes contexts idle > 5 min.
- `release(tab_id)` called when tab closes.
- One shared `Browser` instance launched lazily; closed on app exit.

### Downloader (downloader.py)

- `DownloadWorker(QThread)` per tab; emits `media_ready(int idx, FoundMedia)`,
  `progress(done, total)`, `done()`.
- 8-connection `httpx.AsyncClient` per worker.
- Image flow: download bytes → check dimensions with PIL → emit if ≥ min_dim.
- Video flow: HEAD or stream-check first; if `Content-Length > 20MB`, stream to
  `~/.browpic_cache/media/<sha1>.<ext>` and emit `FoundMedia(data=None, cache_path=...)`.
  Otherwise keep in RAM.
- GIFs: always loaded as bytes (animated playback uses QMovie).
- Video thumbnail: extract first frame with `imageio-ffmpeg` (bundles a small ffmpeg
  binary). On extraction failure, fall back to a gray placeholder with ▶ icon.

### Media Model (media.py)

```python
class MediaKind(StrEnum):
    IMAGE = "image"
    GIF = "gif"
    VIDEO = "video"

@dataclass
class FoundMedia:
    url: str
    kind: MediaKind
    data: bytes | None
    cache_path: Path | None
    width: int
    height: int
    duration_s: float = 0.0
    source_url: str = ""
    is_external: bool = False
```

Helper: `media.bytes_or_path() -> Path` — writes bytes to a temp cache file if needed,
returns a Path that QMediaPlayer / QMovie / QPixmap can consume uniformly.

### Viewer (viewer.py)

`QGraphicsView` + `QGraphicsScene` with one of:
- `QGraphicsPixmapItem` for images
- `QGraphicsPixmapItem` driven by `QMovie.frameChanged` for animated GIFs
- `QGraphicsVideoItem` + `QMediaPlayer` for video

Controls:
- Wheel = zoom 1.2× per notch, centered on cursor, clamped [0.1×, 10×]
- Drag (left-button) = pan; cursor becomes hand
- Double-click = fit-to-window
- Keys: `+`/`-` zoom step, `0` reset+center, `1` 100%, `F` fullscreen,
  `Right`/`Space` next, `Left` prev, `Esc` close, `P` toggle slideshow timer,
  `Ctrl+S` save current
- Switching media resets to fit-to-window (slideshow always 1:1 fit, no pan)
- Bottom-right zoom indicator label, semi-transparent, fades after 1.5s when zoom != fit

Slideshow behavior (variant B):
- Image/static GIF frame: hold for `interval_s`, then advance
- Animated GIF: play one full loop (QMovie.loopCount or first cycle complete), then advance
- Video: play to end (`mediaStatusChanged → EndOfMedia`), then advance
- Hard cap `video_max_s` (default 60s) — if exceeded, force-advance even mid-playback

Combined slideshow: aggregates `media` lists from all tabs in tab order; otherwise identical.

### Cache & Session (cache.py)

- `~/.browpic_cache/media/<sha1-of-url>.<ext>` — large videos (and optionally all media,
  controlled by a future setting; v2 stores only large videos by default)
- `~/.browpic_cache/session.json`:
  ```json
  {
    "tabs": [{"url": "...", "follow_external": false}],
    "settings": {"min_dim": 200, "interval_s": 3, "video_max_s": 60}
  }
  ```
- Loaded on app start; tabs are recreated empty (no auto-fetch). User presses Enter to load.
- LRU cleanup at app start: scan `media/`, sort by atime, delete oldest until total ≤ 2GB.

## Data Flow

1. User pastes URL, presses Enter (or Enter+Shift for new tab).
2. `MainWindow` creates/reuses `GalleryTab`, calls `tab.start_load(url)`.
3. `GalleryTab` classifies source kind (`is_reddit`, host pattern matching).
4. `ExtractWorker` runs; for generic kinds it acquires a `BrowserContext` from the pool.
5. Extracted URLs → `DownloadWorker`; emits `FoundMedia` items as they finish.
6. `GalleryTab` appends to grid, updates dedup set, updates badge count.
7. If `follow_external` is checked, after the initial extraction the tab queues up to 20
   external gallery resolutions; their media is appended with `is_external=True`.
8. User clicks a thumbnail → `Viewer` opens with the tab's media list at that index.
9. "Încarcă mai multe" → `ExtractWorker` runs again with the saved paginator cursor;
   subsequent media merged into the same tab (deduped against `seen_urls`).

## Threading Model

- Main thread: Qt UI only.
- `ExtractWorker(QThread)` per active extract — owns its own asyncio loop.
- `DownloadWorker(QThread)` per active download — owns its own asyncio loop.
- Browser pool exposes a small sync API that schedules work on a single shared
  asyncio loop running on its own thread (one Playwright loop for the whole app).
- Cross-thread communication via Qt signals only; no shared mutable state besides
  the pool dict (guarded by lock).

## Error Handling

- Extract failure (bad URL, 404, JS error) → tab status `error`, message bar shows
  the exception name+message; user can retry by pressing Enter.
- Per-image download failure → silently skipped (current v1 behavior).
- Playwright launch failure → on first occurrence, message box prompts user to run
  `python -m playwright install chromium`; subsequent failures silently retried up to 2×.
- Cache write failure (disk full) → fall back to in-memory; warn in status bar.
- Reddit JSON 429 (rate limit) → retry once with 5s backoff; if still failing,
  surface the error in the tab.

## Testing

Manual smoke tests (no automated test suite for v2 — UI-heavy):
1. Reddit listing (`r/EarthPorn`) — load, "load more" twice, verify dedup.
2. Reddit gallery post — verify all gallery items appear, "load more" disabled.
3. imgur album — verify external resolver triggers when checkbox enabled on a Reddit post linking to it.
4. Generic blog with lazy-loaded images — verify Playwright path catches lazy `<img>`.
5. Video playback — open a Reddit post with v.redd.it; verify it plays in viewer and slideshow advances on EndOfMedia.
6. Slideshow with mixed images + video — verify timing rules per type.
7. Zoom: scroll-to-zoom centered on cursor; pan with drag; `0` resets; `1` is pixel-perfect.
8. Two tabs in parallel — verify independent paginators and that closing one tab releases its browser context.
9. App restart — verify session.json restores tab list (empty grids).
10. Cache LRU: pre-fill `~/.browpic_cache/media/` with > 2GB; verify cleanup on next start.

## Dependencies (additions to requirements.txt)

- `imageio-ffmpeg` — bundled ffmpeg for video thumbnail frames
- (Existing: PySide6, playwright, httpx, Pillow, beautifulsoup4)

PySide6 already includes `QtMultimedia` and `QtMultimediaWidgets` on Windows
via Media Foundation; no extra install needed for video playback itself.

## Open Issues / Future Work

- Favorites and per-tab "save" of selections (deferred from this iteration)
- Tag/search across all loaded media
- Optional disk cache for all media (not just large videos) with an opt-in setting
- Configurable external-gallery host patterns via JSON file
