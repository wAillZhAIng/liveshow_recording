"""Bound read-only Playwright evaluations on the synchronous owner thread."""

import asyncio
import weakref

_MAX_PENDING_READS = 64
_UNSET = object()
_pending_reads = set()
_pending_by_page = weakref.WeakKeyDictionary()


def _prune_closed_loops():
    """Forget tasks whose owner loop was closed during driver teardown."""
    closed = {task for task in _pending_reads if task.get_loop().is_closed()}
    _pending_reads.difference_update(closed)
    for page, task in list(_pending_by_page.items()):
        if task in closed:
            _pending_by_page.pop(page, None)


class BrowserReadTimeout(TimeoutError):
    """The browser did not finish a read; page state is unknown."""


def bounded_evaluate(page, expression: str, arg=_UNSET, *, timeout_seconds: float = 5.0):
    """Use the sync API's own event loop; never touch a page from another thread.

    Playwright's public sync evaluate has no timeout. Its sync wrapper itself
    delegates to this asynchronous implementation, so bound that same coroutine.
    The fallback is for test doubles only, not a timeout guarantee.
    """
    impl = getattr(page, "_impl_obj", None)
    sync = getattr(page, "_sync", None)
    if impl is None or not callable(sync):
        return page.evaluate(expression) if arg is _UNSET else page.evaluate(expression, arg)

    async def run():
        _prune_closed_loops()
        existing = _pending_by_page.get(page)
        if existing is not None and not existing.done():
            raise BrowserReadTimeout("该页面上一次读取尚未返回；页面状态未知")
        if len(_pending_reads) >= _MAX_PENDING_READS:
            raise BrowserReadTimeout(f"未完成页面读取已达安全上限（{len(_pending_reads)}/{_MAX_PENDING_READS}）；页面状态未知")
        arguments = {} if arg is _UNSET else {"arg": arg}
        task = asyncio.create_task(impl.evaluate(expression=expression, **arguments))
        _pending_reads.add(task)
        _pending_by_page[page] = task
        page_ref = weakref.ref(page)

        def finished(done):
            _pending_reads.discard(done)
            owner = page_ref()
            if owner is not None and _pending_by_page.get(owner) is done:
                _pending_by_page.pop(owner, None)
            try:
                done.result()
            except (Exception, asyncio.CancelledError):  # noqa: BLE001 - drain late protocol failures
                # Consume late errors without altering the page or any recording.
                return

        task.add_done_callback(finished)
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
        if not done:
            # Do not cancel: Playwright's cancellation may wait indefinitely
            # for a disconnected protocol driver to acknowledge abort.
            raise BrowserReadTimeout(f"浏览器页面读取超过 {timeout_seconds:g} 秒；页面状态未知")
        return task.result()

    return sync(run())
