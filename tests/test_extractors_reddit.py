import json
from pathlib import Path
import pytest
import respx
import httpx

from browpic.extractors import extract_reddit, RedditAfterCursor
from browpic.media import MediaKind

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
@respx.mock
async def test_reddit_listing_extracts_image_video_and_external():
    body = (FIXTURES / "reddit_listing.json").read_text()
    respx.get("https://www.reddit.com/r/test.json").mock(
        return_value=httpx.Response(200, text=body)
    )
    media, cursor = await extract_reddit("https://www.reddit.com/r/test", after=None)
    urls = [m.url for m in media]
    assert "https://i.redd.it/abc.jpg" in urls
    assert any("v.redd.it" in u and u.endswith(".mp4") for u in urls) or \
           any("DASH_720.mp4" in u for u in urls)
    assert any("imgur.com/a/abcDEF" in u for u in urls)
    kinds = {m.url: m.kind for m in media}
    assert kinds["https://i.redd.it/abc.jpg"] == MediaKind.IMAGE
    assert isinstance(cursor, RedditAfterCursor)
    assert cursor.after == "t3_aaaaaa"


@pytest.mark.asyncio
@respx.mock
async def test_reddit_gallery_post_extracts_all_items_no_cursor():
    body = (FIXTURES / "reddit_gallery.json").read_text()
    respx.get("https://www.reddit.com/r/x/comments/abc.json").mock(
        return_value=httpx.Response(200, text=body)
    )
    media, cursor = await extract_reddit(
        "https://www.reddit.com/r/x/comments/abc", after=None
    )
    urls = [m.url for m in media]
    assert "https://preview.redd.it/img1.jpg?w=2400&auto=webp" in urls
    assert "https://preview.redd.it/img2.jpg?w=2400&auto=webp" in urls
    assert cursor is None


@pytest.mark.asyncio
@respx.mock
async def test_reddit_pagination_uses_after_param():
    body = (FIXTURES / "reddit_listing.json").read_text()
    route = respx.get("https://www.reddit.com/r/test.json").mock(
        return_value=httpx.Response(200, text=body)
    )
    cursor = RedditAfterCursor(after="t3_zzz")
    await extract_reddit("https://www.reddit.com/r/test", after=cursor)
    assert route.called
    call = route.calls[-1]
    assert "after=t3_zzz" in str(call.request.url)
