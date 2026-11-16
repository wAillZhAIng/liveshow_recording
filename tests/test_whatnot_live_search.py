"""Real Chromium DOM regression tests; network is fulfilled locally, not Whatnot."""
from urllib.parse import quote

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from whatnot_monitor.whatnot import (
    WhatnotBrowser,
    WhatnotVerificationRequired,
    _evaluate_page,
)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as manager:
        try:
            instance = manager.chromium.launch(headless=True)
        except PlaywrightError:
            # The desktop app uses installed Chrome, which may be the only
            # Chromium engine present on the Windows deployment machine.
            instance = manager.chromium.launch(channel="chrome", headless=True)
        yield instance
        instance.close()


def test_search_collects_lazy_live_cards_and_excludes_scheduled_and_ended(browser):
    context = browser.new_context(viewport={"width": 1000, "height": 650})
    html = """<!doctype html><meta charset="utf-8"><title>Local search fixture</title>
      <input type="search" aria-label="Search">
      <button role="tab">Shows</button><button>Show all</button>
      <main style="height:420px;overflow-y:auto" id="results">
        <article style="height:260px"><a href="/live/one">Live · 12</a>
          <a href="/user/seller-one">Seller One</a></article>
        <article style="height:260px"><a href="/live/scheduled">Tomorrow 8pm</a>
          <a href="/user/scheduled-seller">Scheduled seller</a></article>
        <article style="height:260px"><a href="/live/ended">Show has ended</a>
          <a href="/user/ended-seller">Ended seller</a></article>
      </main>
      <script>
        document.querySelector('input').onkeydown = event => {
          if (event.key === 'Enter') history.pushState({}, '',
            '/search?query=' + encodeURIComponent(event.target.value));
        };
        document.querySelector('[role=tab]').onclick = () => {
          history.replaceState({}, '', location.href + '&searchVertical=LIVESTREAM');
        };
        let loaded = false;
        results.onscroll = () => {
          if (!loaded && results.scrollTop + results.clientHeight >= results.scrollHeight - 5) {
            loaded = true;
            setTimeout(() => results.insertAdjacentHTML('beforeend',
              '<article style="height:260px"><a href="/live/two">Live · 28</a>' +
              '<a href="/user/seller-two">Seller Two</a></article>'), 350);
          }
        };
      </script>"""
    context.route("https://www.whatnot.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=html))
    try:
        shows = WhatnotBrowser(context).scan_keyword("needoh & toys")
        assert [show.session_id for show in shows] == [
            "whatnot-show:one", "whatnot-show:two"]
        assert [show.name for show in shows] == ["Seller One", "Seller Two"]
        assert context.pages[0].url.startswith(
            "https://www.whatnot.com/search?query=" + quote("needoh & toys"))
    finally:
        context.close()


def test_repeated_keyword_waits_for_delayed_shows_route_before_show_all(browser):
    """Same-query rescans must not act on an old route or CATEGORY control."""
    context = browser.new_context()
    html = """<!doctype html><meta charset='utf-8'>
    <input type='search' aria-label='Search'><main></main><script>
    const visit = Number(sessionStorage.homeVisits || 0) + 1;
    sessionStorage.homeVisits = visit;
    window.staleClicks = 0;
    const main = document.querySelector('main');
    document.querySelector('input').onkeydown = event => {
      if (event.key !== 'Enter') return;
      const query = encodeURIComponent(event.target.value);
      setTimeout(() => {
        history.replaceState({}, '', '/search?query='+query+'&searchVertical=CATEGORY');
        main.innerHTML = '<button role="tab" id="shows">Shows</button>' +
          '<button id="stale">Show all</button>';
        document.querySelector('#stale').onclick = () => {
          window.staleClicks++;
          history.replaceState({}, '', '/search?query=wrong&searchVertical=CATEGORY');
        };
        document.querySelector('#shows').onclick = () => {
          setTimeout(() => {
            history.replaceState({}, '', '/search?query='+query+'&searchVertical=LIVESTREAM');
            main.innerHTML = '<button id="all">Show all</button>';
            document.querySelector('#all').onclick = () => {
              main.innerHTML = `<article><a href="/live/room-${visit}">Live · 8</a>` +
                `<a href="/user/seller-${visit}">Seller ${visit}</a></article>`;
            };
          }, 500);
        };
      }, 300);
    };</script>"""
    context.route("https://www.whatnot.com/**", lambda route: route.fulfill(
        body=html, content_type="text/html"))
    try:
        scanner = WhatnotBrowser(context)
        first = scanner.scan_keyword("needoh")
        second = scanner.scan_keyword("needoh")
        assert [show.session_id for show in first] == ["whatnot-show:room-1"]
        assert [show.session_id for show in second] == ["whatnot-show:room-2"]
        assert len(context.pages) == 1
        page = context.pages[0]
        assert page.evaluate("window.staleClicks") == 0
        assert page.evaluate("Number(sessionStorage.homeVisits)") == 2
        assert "searchVertical=LIVESTREAM" in page.url
    finally:
        context.close()


def test_navigation_race_is_retried_without_reload_or_generic_failure():
    class NavigatingPage:
        def __init__(self):
            self.calls = 0
            self.waits = []

        def evaluate(self, _script):
            self.calls += 1
            if self.calls == 1:
                raise PlaywrightError("Execution context was destroyed, most likely because of a navigation")
            return {"title": "请稍候...", "body": "进行安全验证 恶意自动程序"}

        def is_closed(self):
            return False

        def wait_for_timeout(self, timeout):
            self.waits.append(timeout)

    page = NavigatingPage()
    result = _evaluate_page(page, "() => document.title")
    assert result["title"] == "请稍候..."
    assert page.calls == 2
    assert page.waits == [250]


def test_evaluate_does_not_retry_arbitrary_script_errors():
    class BrokenPage:
        def evaluate(self, _script):
            raise PlaywrightError("SyntaxError: invalid token")

    with pytest.raises(PlaywrightError, match="SyntaxError"):
        _evaluate_page(BrokenPage(), "broken")


def test_collector_does_not_treat_challenge_as_empty_results():
    class ChallengePage:
        def evaluate(self, _script):
            return {"title": "Verify you are human", "body": "security verification", "rows": []}

    with pytest.raises(WhatnotVerificationRequired):
        WhatnotBrowser._collect_search_results(ChallengePage(), "read cards")
