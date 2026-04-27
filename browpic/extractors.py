"""URL → media-list extractors."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx

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
                        dur=float(rv.get("duration", 0)))
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
