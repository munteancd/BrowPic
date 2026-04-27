"""Per-tab Playwright BrowserContext pool with idle eviction.

Runs a single asyncio loop on a dedicated daemon thread so Qt code can
schedule async work without blocking the UI thread.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from typing import Optional

# Use a current Chrome UA so sites don't immediately serve the "browser
# upgrade" page or trigger soft bot heuristics. This is paired with
# playwright-stealth (applied per-context) to mask common headless tells.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BrowserPool:
    def __init__(self, idle_timeout_s: float = 300.0):
        self.idle_timeout_s = idle_timeout_s
        self._tabs: dict[str, tuple[object, float]] = {}
        self._browser = None
        self._playwright = None
        self._lock = asyncio.Lock()

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

    async def acquire(self, tab_id: str):
        await self._ensure_browser()
        async with self._lock:
            entry = self._tabs.get(tab_id)
            if entry is not None:
                ctx, _ = entry
                self._tabs[tab_id] = (ctx, time.monotonic())
                return ctx
            ctx = await self._browser.new_context(user_agent=USER_AGENT)
            # Apply stealth patches that mask navigator.webdriver, plugins,
            # WebGL vendor strings, etc. Many sites (Bing, Pinterest,
            # Twitter, most blogs) are happy with this. Google specifically
            # uses additional fingerprinting and will still serve a captcha
            # in headless mode — there is no fix for that short of running
            # the browser visibly, which we do not do here.
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(ctx)
            except Exception:
                # Stealth is a best-effort enhancement; never fail extraction
                # because it could not be applied.
                pass
            self._tabs[tab_id] = (ctx, time.monotonic())
            return ctx

    async def release(self, tab_id: str):
        async with self._lock:
            entry = self._tabs.pop(tab_id, None)
        if entry:
            try:
                await entry[0].close()
            except Exception:
                pass

    async def evict_idle(self):
        now = time.monotonic()
        to_close = []
        async with self._lock:
            for tid, (ctx, last) in list(self._tabs.items()):
                if now - last > self.idle_timeout_s:
                    to_close.append((tid, ctx))
                    self._tabs.pop(tid, None)
        for _, ctx in to_close:
            try:
                await ctx.close()
            except Exception:
                pass

    async def shutdown(self):
        async with self._lock:
            ctxs = [c for c, _ in self._tabs.values()]
            self._tabs.clear()
        for c in ctxs:
            try:
                await c.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass


class AsyncRunner:
    """Runs an asyncio loop on a background daemon thread.

    Use `submit(coro)` to schedule a coroutine and get a concurrent.futures.Future.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="BrowPicAsync")
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2)
