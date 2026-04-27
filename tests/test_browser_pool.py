import asyncio
import time
import pytest
from browpic.browser_pool import BrowserPool


class FakeContext:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts: list[FakeContext] = []

    async def new_context(self, **kwargs):
        c = FakeContext()
        self.contexts.append(c)
        return c

    async def close(self):
        for c in self.contexts:
            await c.close()


@pytest.mark.asyncio
async def test_pool_acquire_creates_and_reuses(monkeypatch):
    fake = FakeBrowser()
    pool = BrowserPool(idle_timeout_s=60)
    pool._browser = fake

    c1 = await pool.acquire("tab-1")
    c2 = await pool.acquire("tab-1")
    c3 = await pool.acquire("tab-2")
    assert c1 is c2
    assert c1 is not c3
    assert len(fake.contexts) == 2


@pytest.mark.asyncio
async def test_pool_release_closes_context():
    fake = FakeBrowser()
    pool = BrowserPool(idle_timeout_s=60)
    pool._browser = fake

    c = await pool.acquire("t")
    await pool.release("t")
    assert c.closed is True
    assert "t" not in pool._tabs


@pytest.mark.asyncio
async def test_pool_evicts_idle():
    fake = FakeBrowser()
    pool = BrowserPool(idle_timeout_s=0.05)
    pool._browser = fake

    c = await pool.acquire("t")
    await asyncio.sleep(0.1)
    await pool.evict_idle()
    assert c.closed is True
    assert "t" not in pool._tabs
