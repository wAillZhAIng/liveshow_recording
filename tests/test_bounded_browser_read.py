import asyncio
import time

import pytest
from playwright.sync_api import sync_playwright
from whatnot_monitor.bounded_browser_read import BrowserReadTimeout, bounded_evaluate


def test_read_arguments_are_forwarded_on_the_owner_loop():
    loop = asyncio.new_event_loop()
    class Impl:
        async def evaluate(self, expression, arg):
            assert expression == "requirements => requirements.requireAudio"
            return arg["requireAudio"]
    class PageDouble:
        _impl_obj = Impl()
        _sync = staticmethod(loop.run_until_complete)
    try:
        assert bounded_evaluate(
            PageDouble(), "requirements => requirements.requireAudio", {"requireAudio": True},
        ) is True
    finally:
        loop.close()


def test_awaitable_never_resolves_returns_with_timeout():
    loop = asyncio.new_event_loop()
    release = loop.create_future()
    class Impl:
        cancelled = False
        async def evaluate(self, expression):
            try:
                return await release
            except asyncio.CancelledError:
                self.cancelled = True
                # Model a Playwright Channel._abort cancellation that never ends.
                await asyncio.Future()
    class PageDouble:
        _impl_obj = Impl()
        _sync = staticmethod(loop.run_until_complete)
    page = PageDouble()
    try:
        started = time.monotonic()
        with pytest.raises(BrowserReadTimeout):
            bounded_evaluate(page, "() => 1", timeout_seconds=0.05)
        assert time.monotonic() - started < 2
        assert not page._impl_obj.cancelled
        with pytest.raises(BrowserReadTimeout, match="上一次"):
            bounded_evaluate(page, "() => 1", timeout_seconds=0.05)
        release.set_result(42)
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))
        assert not page._impl_obj.cancelled
    finally:
        loop.close()


def test_pending_reads_are_globally_bounded_and_late_errors_are_reaped(monkeypatch):
    import whatnot_monitor.bounded_browser_read as bounded
    loop = asyncio.new_event_loop()
    release = loop.create_future()
    class Impl:
        async def evaluate(self, expression):
            return await release
    class PageDouble:
        _impl_obj = Impl()
        _sync = staticmethod(loop.run_until_complete)
    first, second = PageDouble(), PageDouble()
    monkeypatch.setattr(bounded, "_MAX_PENDING_READS", 1)
    try:
        with pytest.raises(BrowserReadTimeout):
            bounded_evaluate(first, "() => 1", timeout_seconds=0.05)
        with pytest.raises(BrowserReadTimeout, match="安全上限"):
            bounded_evaluate(second, "() => 1", timeout_seconds=0.05)
        release.set_exception(RuntimeError("late synthetic failure"))
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))
        assert not bounded._pending_reads
    finally:
        loop.close()


def test_closed_owner_loop_cannot_permanently_exhaust_capacity(monkeypatch):
    import whatnot_monitor.bounded_browser_read as bounded
    class ClosedLoop:
        def is_closed(self): return True
    class OldTask:
        def get_loop(self): return ClosedLoop()
    class OldPage: pass
    old_page, old_task = OldPage(), OldTask()
    bounded._pending_reads.add(old_task)
    bounded._pending_by_page[old_page] = old_task
    loop = asyncio.new_event_loop()
    class Impl:
        async def evaluate(self, expression): return 17
    class PageDouble:
        _impl_obj = Impl()
        _sync = staticmethod(loop.run_until_complete)
    monkeypatch.setattr(bounded, "_MAX_PENDING_READS", 1)
    try:
        assert bounded_evaluate(PageDouble(), "() => 17", timeout_seconds=0.05) == 17
        loop.run_until_complete(asyncio.sleep(0))
        assert old_task not in bounded._pending_reads
        assert old_page not in bounded._pending_by_page
    finally:
        loop.close()


@pytest.mark.parametrize("stall", ["promise", "busy_loop"])
def test_timeout_of_one_page_preserves_other_page_media_recorder(stall):
    """Independent local Chromium; no production profile, Whatnot or audio devices."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        first = browser.new_context().new_page()
        other = browser.new_context().new_page()
        try:
            other.evaluate("""() => {
                const ctx = new AudioContext();
                const osc = ctx.createOscillator();
                const dest = ctx.createMediaStreamDestination();
                osc.connect(dest); osc.start();
                const r = new MediaRecorder(dest.stream);
                window.syntheticAudio = { ctx, osc, r, chunks: 0, bytes: 0 };
                r.ondataavailable = e => { if (e.data.size) { window.syntheticAudio.chunks++; window.syntheticAudio.bytes += e.data.size; } };
                r.start(100);
            }""")
            other.wait_for_function("window.syntheticAudio.chunks >= 2", timeout=8000)
            before = other.evaluate("window.syntheticAudio.bytes")
            start = time.monotonic()
            expression = "() => new Promise(() => {})" if stall == "promise" else "() => { while (true) {} }"
            with pytest.raises(BrowserReadTimeout):
                bounded_evaluate(first, expression, timeout_seconds=0.5)
            assert time.monotonic() - start < 5
            other.wait_for_function("n => window.syntheticAudio.bytes > n", arg=before, timeout=8000)
            assert other.evaluate("window.syntheticAudio.r.state") == "recording"
            with pytest.raises(BrowserReadTimeout, match="上一次"):
                bounded_evaluate(first, "() => 42", timeout_seconds=2)
            assert bounded_evaluate(other, "() => 42", timeout_seconds=2) == 42
        finally:
            other.evaluate("""() => { window.syntheticAudio.r.stop();
                window.syntheticAudio.osc.stop(); window.syntheticAudio.ctx.close(); }""")
            browser.close()
