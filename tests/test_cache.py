import json
import time
from pathlib import Path
import pytest
from browpic.cache import (
    SessionState, load_session, save_session, lru_cleanup
)


def test_save_and_load_roundtrip(tmp_path):
    s = SessionState(
        tabs=[{"url": "https://reddit.com/r/x", "follow_external": False}],
        settings={"min_dim": 200, "interval_s": 3, "video_max_s": 60},
    )
    path = tmp_path / "session.json"
    save_session(s, path)
    loaded = load_session(path)
    assert loaded.tabs == s.tabs
    assert loaded.settings == s.settings


def test_load_session_missing_returns_default(tmp_path):
    s = load_session(tmp_path / "nope.json")
    assert s.tabs == []
    assert "min_dim" in s.settings


def test_load_session_corrupt_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not-json{")
    s = load_session(p)
    assert s.tabs == []


def test_lru_cleanup_under_limit_no_op(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "b.bin").write_bytes(b"x" * 100)
    deleted = lru_cleanup(tmp_path, max_bytes=10_000)
    assert deleted == 0
    assert (tmp_path / "a.bin").exists()
    assert (tmp_path / "b.bin").exists()


def test_lru_cleanup_over_limit_deletes_oldest(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    a.write_bytes(b"x" * 1000)
    time.sleep(0.02)
    b.write_bytes(b"x" * 1000)
    time.sleep(0.02)
    c.write_bytes(b"x" * 1000)
    import os
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    os.utime(c, (3000, 3000))
    deleted = lru_cleanup(tmp_path, max_bytes=2200)
    assert deleted >= 1
    assert not a.exists()
    assert c.exists()
