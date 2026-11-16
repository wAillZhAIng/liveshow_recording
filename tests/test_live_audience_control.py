"""Offline DOM regressions for the observed, unlabeled live header counter.

Synthetic HTML only. No production profile, Whatnot network or record writes.
"""
import pytest
from playwright.sync_api import sync_playwright
from whatnot_monitor.recording.browser_audio import _CAPTURE_IDENTITY_JS
from whatnot_monitor.whatnot import _AUDIENCE_PAGE_SCRIPT, _viewer_observation

# Literal captured control geometry/icon, independent from the extractor.
ICON = ('M7.8 10.6a1.4 1.4 0 1 0-2.8 0v7a1.4 1.4 0 1 0 2.8 0z'
        'M17.6 10.6A1.4 1.4 0 0 1 19 12v5.6a1.4 1.4 0 1 1-2.8 0V12a1.4 1.4 0 0 1 1.4-1.4'
        'M13.4 6.4a1.4 1.4 0 1 0-2.8 0v9.8a1.4 1.4 0 1 0 2.8 0z')


def counter(value, icon=ICON):
    return ('<section><button type="button"><svg viewBox="0 0 24 24">'
            f'<path d="{icon}"></path></svg><strong>{value}</strong></button></section>')


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as driver:
        launched = driver.chromium.launch(channel="chrome", headless=True)
        yield launched
        launched.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    context.route("**/*", lambda route: route.abort())
    current = context.new_page()
    yield current
    context.close()


def inspect(page, control, extra="", watching=True):
    page.set_content('''<title>Seller is live</title><style>
      .player {position:relative;width:500px;height:500px}
      video {width:500px;height:500px} section {position:absolute;right:20px;top:20px}
      svg {width:12px;height:12px}
    </style><div class="player"><video></video>''' + control + '</div>'
        + ('<button data-testid="tab-watching">Watching</button>' if watching else '')
        + extra)
    data = page.evaluate(_AUDIENCE_PAGE_SCRIPT)
    return data, _viewer_observation(data["audienceCandidates"], data["liveHeaderCounters"])


@pytest.mark.parametrize("raw,expected", [("0", 0), ("79", 79), ("80", 80),
                                          ("1,250", 1250), ("1.2K", 1200)])
def test_exact_live_header_control_can_supply_count(page, raw, expected):
    data, result = inspect(page, counter(raw), '<span>4.9</span><b>$80</b><i>21 entries</i>')
    assert data["liveHeaderCounters"] == [raw]
    assert result == (expected, f"live_header_counter: {raw}")


@pytest.mark.parametrize("control,watching", [
    (counter("80", "M0 0L10 10"), True),  # Other icons aren't audience evidence.
    (counter("80"), False),
    (counter("4.9", "star"), True),
    (counter("80 bids"), True),
    (counter("$80"), True),
    (counter("unknown"), True),
    (counter("79.9"), True),
    (counter("1,25"), True),
    (counter("80").replace('<strong>', '<strong style="display:none">'), True),
    (counter("80").replace('<section>', '<section style="top:300px">'), True),
    ('<section><button>80</button></section>', True),
])
def test_unproven_or_replaced_control_is_unknown_not_zero(page, control, watching):
    _, result = inspect(page, control, '<span>80</span><span>4.9</span>', watching)
    assert result == (None, None)


def test_ambiguous_counters_and_conflicting_labels_are_unknown(page):
    _, result = inspect(page, counter("80") + counter("79"))
    assert result == (None, None)
    _, result = inspect(page, counter("80"), '<span>79 viewers</span>')
    assert result == (None, None)


def test_explicit_labels_still_work_without_header_control(page):
    _, result = inspect(page, "", '<span>80 viewers</span>')
    assert result == (80, "80 viewers")
    assert _viewer_observation(["80", "21 entries", "4.9", "$80"]) == (None, None)


@pytest.mark.parametrize("path", ["/live/SHOW-A", "/show/show-a?x=1#room", "/shows/show-a/"])
def test_actual_identity_js_matches_show_aliases_and_guards_tokens(page, path):
    # Real Chromium JS parsing, but an intercepted synthetic document only.
    page.route("https://www.whatnot.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<title>Offline identity fixture</title>"
    ))
    page.goto("https://www.whatnot.com" + path)
    result = page.evaluate("""() => {
        const captureToken = 'owned-capture';
        let documentToken;
    """ + _CAPTURE_IDENTITY_JS + """
        const state = {};
        identifyCapture(state);
        documentToken = state.documentToken;
        requireOwnedCapture(state);
        const descriptor = Object.getOwnPropertyDescriptor(state, 'captureToken');
        let rejected = false;
        try { requireOwnedCapture({...state, captureToken:'newer-capture'}); }
        catch (_) { rejected = true; }
        history.replaceState(null, '', '/live/show-b');
        let driftRejected = false;
        try { requireOwnedCapture(state); } catch (_) { driftRejected = true; }
        return {showId:state.showId,documentToken,token:state.captureToken,
            writable:descriptor.writable,configurable:descriptor.configurable,
            rejected,driftRejected};
    }""")
    assert result == {
        "showId": "show-a", "documentToken": "owned-capture", "token": "owned-capture",
        "writable": False, "configurable": False, "rejected": True, "driftRejected": True,
    }


def test_actual_identity_js_document_token_survives_spa_but_not_reload(page):
    page.route("https://www.whatnot.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<title>Offline identity fixture</title>"
    ))
    page.goto("https://www.whatnot.com/live/show-a")
    expression = """({captureToken}) => {
        const documentToken = null;
    """ + _CAPTURE_IDENTITY_JS + """
        const state = {}; identifyCapture(state);
        return {showId:state.showId,documentToken:state.documentToken};
    }"""
    assert page.evaluate(expression, {"captureToken": "first"}) == {
        "showId": "show-a", "documentToken": "first",
    }
    page.evaluate("history.replaceState(null, '', '/shows/show-a?x=2')")
    assert page.evaluate(expression, {"captureToken": "second"})["documentToken"] == "first"
    page.reload()
    assert page.evaluate(expression, {"captureToken": "third"})["documentToken"] == "third"
