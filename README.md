# BrowPic

A Windows desktop app: paste any URL, see all images, GIFs, and videos from
that page in a thumbnail grid, with a zoom-pan viewer and a slideshow that
auto-advances through images and videos alike.

Built with PySide6 + Playwright. Reddit gets a fast JSON-API path; everything
else is rendered headlessly with `playwright-stealth` to dodge most bot
detection. Imgur albums and Redgifs are resolved automatically when "Follow
external galleries" is on.

## Features

- **Multi-URL tabs** — load several sources side by side
- **Continuous load** — "Încarcă mai multe" pages through Reddit listings
  and scroll-based feeds
- **Reddit JSON path** — fast, no browser needed, handles galleries and
  v.redd.it videos
- **Generic Playwright path** — for blogs, image hosts, and JS-heavy pages,
  with stealth patches to mask headless tells
- **External-gallery fan-out** — optional checkbox follows imgur/redgifs
  links inside a Reddit post
- **Video / GIF playback** — `QtMultimedia` plays muted, advances on
  end-of-media in slideshow
- **Viewer with zoom & pan** — wheel = zoom on cursor, drag = pan, `0` =
  fit, `1` = pixel-perfect
- **Save all** — dump every loaded media item to a folder
- **Session restore** — closes with N tabs open, reopens with the same N
  (empty grids; press Enter to reload each)

## Install

Requires Python 3.10+ on Windows.

```bat
git clone https://github.com/munteancd/BrowPic.git
cd BrowPic
setup.bat
```

`setup.bat` creates a venv, installs Python deps, and downloads the
Playwright Chromium binary (~150 MB). Run once.

## Run

```bat
run.bat
```

Paste a URL, press Enter (or `Shift+Enter` for a new tab).

### Keyboard shortcuts (in the viewer)

| Key            | Action                                  |
| -------------- | --------------------------------------- |
| `→` / `Space`  | Next                                    |
| `←`            | Previous                                |
| `P`            | Toggle slideshow                        |
| `F`            | Fullscreen                              |
| `Esc`          | Close                                   |
| `+` / `-`      | Zoom step                               |
| `0`            | Fit to window                           |
| `1`            | 100% (pixel-perfect)                    |
| Scroll wheel   | Zoom centered on cursor                 |
| Drag           | Pan                                     |
| Double-click   | Fit to window                           |
| `Ctrl+S`       | Save current item                       |

## Known limitations

- **Google Images is blocked.** Google detects headless Chromium even with
  stealth and serves a reCAPTCHA. Use Bing Images or DuckDuckGo Images
  instead — both work fine.
- **Audio is muted on videos** — by design, to skip the ffmpeg audio-merge
  step on Reddit v.redd.it videos.
- **Windows-only setup scripts.** The Python code is portable; the
  `.bat` files aren't. Linux/macOS would need equivalent shell scripts.

## Development

```bat
.venv\Scripts\pytest -v
```

22 unit tests covering the data model, cache, Reddit and external-gallery
extractors, and the browser pool. UI components (viewer, tabs, app) are
smoke-tested manually.

The app is split into a small package:

```
browpic/
  app.py            MainWindow + entry point
  tabs.py           GalleryTab — one tab per source URL
  extractors.py     Reddit JSON, generic Playwright, imgur/redgifs resolvers
  downloader.py     Async downloads, video streaming, thumbnails
  viewer.py         QGraphicsView-based viewer with zoom/pan and video
  media.py          FoundMedia data model
  browser_pool.py   Per-tab Chromium contexts + idle eviction + async runner
  cache.py          Session JSON + LRU disk cleanup
```

Design and implementation plan are checked into `docs/superpowers/`.

## License

MIT — see [LICENSE](LICENSE).
