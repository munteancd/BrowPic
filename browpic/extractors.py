"""URL → media-list extractors."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

from .media import FoundMedia, MediaKind, kind_from_url

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BrowPic/2.0"


# ---------- cursor types ----------

@dataclass
class RedditAfterCursor:
    after: str


@dataclass
class PlaywrightScrollCursor:
    scroll_y: int
    seen_count: int


Paginator = RedditAfterCursor | PlaywrightScrollCursor


# ---------- url helpers ----------

def is_reddit(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("reddit.com") or host.endswith("redd.it")


def _to_reddit_json_url(url: str, after: Optional[str]) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if not path.endswith(".json"):
        path = path.rstrip("/") + ".json"
    base = f"{parsed.scheme}://{parsed.netloc}{path}"
    qs = []
    if parsed.query:
        qs.append(parsed.query)
    if after:
        qs.append(f"after={after}")
        qs.append("count=25")
    return base + ("?" + "&".join(qs) if qs else "")


# ---------- Reddit extractor ----------

async def extract_reddit(
    url: str, after: Optional[RedditAfterCursor] = None
) -> tuple[list[FoundMedia], Optional[RedditAfterCursor]]:
    after_str = after.after if after else None
    json_url = _to_reddit_json_url(url, after_str)
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
        r = await client.get(json_url)
        r.raise_for_status()
        data = r.json()

    media: list[FoundMedia] = []
    seen: set[str] = set()

    def add(u: str, kind: MediaKind, w: int = 0, h: int = 0, dur: float = 0.0):
        u = u.replace("&amp;", "&")
        if u in seen:
            return
        seen.add(u)
        media.append(FoundMedia(
            url=u, kind=kind, data=None, cache_path=None,
            width=w, height=h, duration_s=dur, source_url=url,
        ))

    next_after: Optional[str] = None

    def walk(node):
        nonlocal next_after
        if isinstance(node, dict):
            if node.get("kind") == "Listing" and isinstance(node.get("data"), dict):
                a = node["data"].get("after")
                if a:
                    next_after = a
            kind = node.get("kind")
            d = node.get("data") if isinstance(node.get("data"), dict) else None
            if kind == "t3" and d:
                rv = (d.get("media") or {}).get("reddit_video")
                if isinstance(rv, dict) and rv.get("fallback_url"):
                    add(rv["fallback_url"], MediaKind.VIDEO,
                        w=rv.get("width", 0), h=rv.get("height", 0),
                        dur=float(rv.get("duration") or 0))
                gallery = d.get("media_metadata")
                if isinstance(gallery, dict):
                    for m in gallery.values():
                        if not isinstance(m, dict):
                            continue
                        s = m.get("s") or {}
                        u = s.get("u") or s.get("gif")
                        if u:
                            add(u, kind_from_url(u),
                                w=int(s.get("x", 0)), h=int(s.get("y", 0)))
                u = d.get("url_overridden_by_dest") or d.get("url")
                if u:
                    if re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", u, re.I):
                        add(u, kind_from_url(u))
                    elif d.get("post_hint") == "image":
                        add(u, MediaKind.IMAGE)
                    elif _looks_like_external_gallery(u):
                        add(u, MediaKind.IMAGE)
                preview = d.get("preview")
                if isinstance(preview, dict):
                    for img in preview.get("images") or []:
                        src = (img.get("source") or {}).get("url")
                        if src:
                            add(src, MediaKind.IMAGE,
                                w=int((img.get("source") or {}).get("width", 0)),
                                h=int((img.get("source") or {}).get("height", 0)))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    cursor = RedditAfterCursor(after=next_after) if next_after else None
    return media, cursor


_EXTERNAL_HOST_RE = re.compile(
    r"^(?:https?://)?(?:[a-z0-9.-]+\.)?(imgur\.com|redgifs\.com|flickr\.com)",
    re.I,
)


def _looks_like_external_gallery(url: str) -> bool:
    return bool(_EXTERNAL_HOST_RE.match(url))


# ---------- external gallery resolvers ----------

EXTERNAL_GALLERY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^https?://(?:www\.|m\.)?imgur\.com/(?:a|gallery)/([A-Za-z0-9]+)"), "imgur_album"),
    (re.compile(r"^https?://(?:www\.|m\.)?imgur\.com/([A-Za-z0-9]+)$"), "imgur_single"),
    (re.compile(r"^https?://(?:www\.)?redgifs\.com/watch/([A-Za-z0-9]+)"), "redgifs"),
]


async def resolve_external_gallery(url: str) -> list[FoundMedia]:
    for pattern, kind in EXTERNAL_GALLERY_PATTERNS:
        m = pattern.match(url)
        if not m:
            continue
        try:
            if kind == "imgur_album":
                return await _resolve_imgur_album(url)
            if kind == "imgur_single":
                return await _resolve_imgur_single(url)
            if kind == "redgifs":
                return await _resolve_redgifs(m.group(1))
        except Exception:
            return []
    return []


async def _fetch_text(url: str) -> str:
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT},
                                  follow_redirects=True, timeout=20) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text


async def _resolve_imgur_album(url: str) -> list[FoundMedia]:
    html = await _fetch_text(url)
    media: list[FoundMedia] = []
    obj = None
    key_match = re.search(r"album_images\s*\"\s*:\s*\{", html, re.S)
    if key_match:
        start = key_match.end() - 1  # position of '{'
        depth = 0
        end = -1
        in_str = False
        esc = False
        for i in range(start, len(html)):
            ch = html[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
        if end != -1:
            try:
                obj = json.loads(html[start:end + 1])
            except Exception:
                obj = None
    if obj is not None:
        try:
            for img in obj.get("images", []):
                ext = img.get("ext", ".jpg")
                u = f"https://i.imgur.com/{img['hash']}{ext}"
                media.append(FoundMedia(
                    url=u, kind=kind_from_url(u), data=None, cache_path=None,
                    width=int(img.get("width", 0)), height=int(img.get("height", 0)),
                    source_url=url, is_external=True,
                ))
        except Exception:
            pass
    if media:
        return media
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        u = og["content"]
        media.append(FoundMedia(
            url=u, kind=kind_from_url(u), data=None, cache_path=None,
            width=0, height=0, source_url=url, is_external=True,
        ))
    return media


async def _resolve_imgur_single(url: str) -> list[FoundMedia]:
    html = await _fetch_text(url)
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        u = og["content"]
        return [FoundMedia(
            url=u, kind=kind_from_url(u), data=None, cache_path=None,
            width=0, height=0, source_url=url, is_external=True,
        )]
    return []


async def _resolve_redgifs(gif_id: str) -> list[FoundMedia]:
    api = f"https://api.redgifs.com/v2/gifs/{gif_id}"
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT},
                                  follow_redirects=True, timeout=20) as c:
        r = await c.get(api)
        r.raise_for_status()
        obj = r.json()
    g = obj.get("gif") or {}
    urls = g.get("urls") or {}
    u = urls.get("hd") or urls.get("sd")
    if not u:
        return []
    return [FoundMedia(
        url=u, kind=MediaKind.VIDEO, data=None, cache_path=None,
        width=int(g.get("width", 0)), height=int(g.get("height", 0)),
        duration_s=float(g.get("duration", 0)),
        source_url=f"https://www.redgifs.com/watch/{gif_id}",
        is_external=True,
    )]


# ---------- Generic (Playwright) extractor ----------

async def extract_generic(
    url: str,
    context,  # playwright.async_api.BrowserContext
    cursor: Optional[PlaywrightScrollCursor] = None,
    scroll_chunks: int = 8,
) -> tuple[list[FoundMedia], Optional[PlaywrightScrollCursor]]:
    """Render the page in `context` and extract image/video URLs.

    If `cursor` is provided, reuses the existing tab and continues scrolling
    from `scroll_y`; otherwise opens a new page and starts at the top.
    """
    pages = context.pages
    page = pages[0] if pages and cursor else await context.new_page()

    media_urls: dict[str, MediaKind] = {}

    def add(u: Optional[str], k: MediaKind = MediaKind.IMAGE):
        if not u or u.startswith("data:"):
            return
        media_urls.setdefault(u, k)

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if ct.startswith("image/"):
                add(resp.url, kind_from_url(resp.url))
            elif ct.startswith("video/"):
                add(resp.url, MediaKind.VIDEO)
        except Exception:
            pass

    if cursor is None:
        page.on("response", on_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        start_y = 0
    else:
        start_y = cursor.scroll_y

    new_y = start_y
    for _ in range(scroll_chunks):
        await page.evaluate(f"window.scrollTo(0, {new_y})")
        await page.wait_for_timeout(400)
        new_y = await page.evaluate("window.scrollY + window.innerHeight")

    base = page.url
    html = await page.content()

    for img in await page.query_selector_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src"):
            v = await img.get_attribute(attr)
            if v:
                add(urljoin(base, v), kind_from_url(v))
        srcset = await img.get_attribute("srcset")
        if srcset:
            parts = [p.strip() for p in srcset.split(",") if p.strip()]
            if parts:
                last = parts[-1].split()[0]
                add(urljoin(base, last), kind_from_url(last))

    for vid in await page.query_selector_all("video"):
        v = await vid.get_attribute("src")
        if v:
            add(urljoin(base, v), MediaKind.VIDEO)
        for src_el in await vid.query_selector_all("source"):
            v = await src_el.get_attribute("src")
            if v:
                add(urljoin(base, v), MediaKind.VIDEO)

    soup = BeautifulSoup(html, "html.parser")
    for el in soup.find_all(style=True):
        style = el.get("style") or ""
        for m in re.finditer(r"url\(['\"]?([^'\")]+)['\"]?\)", style):
            add(urljoin(base, m.group(1)), kind_from_url(m.group(1)))

    end_y = await page.evaluate("window.scrollY")
    page_height = await page.evaluate("document.body.scrollHeight")
    progressed = end_y > start_y + 200
    at_bottom = end_y + 100 >= page_height
    next_cursor = (
        PlaywrightScrollCursor(scroll_y=end_y, seen_count=len(media_urls))
        if progressed and not at_bottom
        else None
    )

    out = [
        FoundMedia(
            url=u, kind=k, data=None, cache_path=None,
            width=0, height=0, source_url=url,
        )
        for u, k in media_urls.items()
    ]
    return out, next_cursor


async def extract_external_links(html: str, base_url: str) -> list[str]:
    """Find href attributes in HTML that match external gallery patterns."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        u = urljoin(base_url, a["href"])
        if u in seen:
            continue
        for pattern, _ in EXTERNAL_GALLERY_PATTERNS:
            if pattern.match(u):
                seen.add(u)
                out.append(u)
                break
    return out
