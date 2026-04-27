from pathlib import Path
import tempfile
from browpic.media import FoundMedia, MediaKind, kind_from_url


def test_kind_from_url_image():
    assert kind_from_url("https://x.com/a.jpg") == MediaKind.IMAGE
    assert kind_from_url("https://x.com/a.PNG?q=1") == MediaKind.IMAGE
    assert kind_from_url("https://x.com/a.webp") == MediaKind.IMAGE


def test_kind_from_url_gif():
    assert kind_from_url("https://x.com/a.gif") == MediaKind.GIF


def test_kind_from_url_video():
    assert kind_from_url("https://x.com/a.mp4") == MediaKind.VIDEO
    assert kind_from_url("https://x.com/a.webm") == MediaKind.VIDEO
    assert kind_from_url("https://imgur.com/a.gifv") == MediaKind.VIDEO


def test_kind_from_url_unknown_defaults_image():
    assert kind_from_url("https://x.com/photo") == MediaKind.IMAGE


def test_bytes_or_path_returns_cache_path_when_set(tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"video-bytes")
    m = FoundMedia(url="x", kind=MediaKind.VIDEO, data=None, cache_path=p,
                   width=640, height=480)
    assert m.bytes_or_path() == p


def test_bytes_or_path_writes_temp_when_data_only(tmp_path, monkeypatch):
    from browpic import media as media_mod
    monkeypatch.setattr(media_mod, "CACHE_DIR", tmp_path)
    m = FoundMedia(url="https://x.com/a.jpg", kind=MediaKind.IMAGE,
                   data=b"img-bytes", cache_path=None, width=200, height=200)
    out = m.bytes_or_path()
    assert out.exists()
    assert out.read_bytes() == b"img-bytes"
