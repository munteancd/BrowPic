from pathlib import Path
import pytest
import respx
import httpx

from browpic.extractors import resolve_external_gallery
from browpic.media import MediaKind

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
@respx.mock
async def test_resolve_imgur_album_extracts_all_images():
    body = (FIXTURES / "imgur_album.html").read_text()
    respx.get("https://imgur.com/a/abcDEF").mock(
        return_value=httpx.Response(200, text=body)
    )
    media = await resolve_external_gallery("https://imgur.com/a/abcDEF")
    urls = [m.url for m in media]
    assert "https://i.imgur.com/aaa111.jpg" in urls
    assert "https://i.imgur.com/bbb222.png" in urls
    assert "https://i.imgur.com/ccc333.gif" in urls
    kinds = {m.url: m.kind for m in media}
    assert kinds["https://i.imgur.com/ccc333.gif"] == MediaKind.GIF


@pytest.mark.asyncio
@respx.mock
async def test_resolve_imgur_single_image_url():
    respx.get("https://imgur.com/abcDEF").mock(
        return_value=httpx.Response(
            200,
            text='<meta property="og:image" content="https://i.imgur.com/abcDEF.jpg">',
        )
    )
    media = await resolve_external_gallery("https://imgur.com/abcDEF")
    assert any(m.url == "https://i.imgur.com/abcDEF.jpg" for m in media)


@pytest.mark.asyncio
@respx.mock
async def test_resolve_redgifs_via_api():
    respx.get("https://api.redgifs.com/v2/gifs/abc").mock(
        return_value=httpx.Response(200, json={
            "gif": {"id": "abc", "urls": {"hd": "https://media.redgifs.com/abc.mp4"},
                     "width": 1280, "height": 720, "duration": 12.5}
        })
    )
    media = await resolve_external_gallery("https://www.redgifs.com/watch/abc")
    assert media
    assert media[0].url == "https://media.redgifs.com/abc.mp4"
    assert media[0].kind == MediaKind.VIDEO
    assert media[0].duration_s == 12.5


@pytest.mark.asyncio
async def test_resolve_unknown_host_returns_empty():
    media = await resolve_external_gallery("https://example.com/gallery")
    assert media == []
