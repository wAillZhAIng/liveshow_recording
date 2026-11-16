"""Regress the real search layout: Live filter and slow skeleton pagination."""
import time

import pytest
from playwright.sync_api import sync_playwright
from whatnot_monitor.whatnot import WhatnotBrowser, WhatnotPageChanged


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as manager:
        instance = manager.chromium.launch(channel="chrome", headless=True)
        yield instance
        instance.close()


def test_live_filter_excludes_upcoming_and_waits_for_slow_final_batch(browser):
    context = browser.new_context(viewport={"width": 1000, "height": 725})
    html = """<!doctype html><meta charset='utf-8'>
    <input type='search' aria-label='Search'><button role='tab'>Shows</button><button>Show all</button>
    <aside style='height:600px;overflow:auto;position:fixed;right:0'><div style='height:2000px'>
      <a href='/live/decoy'>Live · 9</a><a href='/user/decoy'>Decoy</a></div></aside>
    <main><label><input type='checkbox' id='PLAYING' value='PLAYING'>Live</label>
    <label><input type='checkbox' id='CREATED' value='CREATED' checked>Upcoming</label>
    <div id='grid'></div></main><script>
    const card=(id,status)=>`<section data-testid='livestream-card' style='height:150px'>
      <a href='/live/${id}'>${status}</a><a href='/live/${id}'>Title ${id}</a>
      <a href='/user/${id}'>Seller ${id}</a></section>`;
    grid.innerHTML=Array.from({length:860},(_,i)=>card(i,i<44?'Live · 12':'Tomorrow')).join('');
    document.querySelector('input[type=search]').onkeydown=e=>{
      if(e.key==='Enter')history.replaceState({},'','/search?query='+e.target.value)};
    document.querySelector('[role=tab]').onclick=()=>history.replaceState({},'',location.href+'&searchVertical=LIVESTREAM');
    PLAYING.onchange=()=>{
      grid.innerHTML=Array.from({length:44},(_,i)=>card(i,'Live · 12')).join('')+
        `<div data-type='SkeletonItem' style='height:150px'><div aria-busy='true'>Loading</div></div>`;
    };
    let loading=false;
    window.addEventListener('scroll',()=>{
      const skeleton=grid.querySelector('[data-type=SkeletonItem]');
      if(skeleton&&!loading&&skeleton.getBoundingClientRect().top<innerHeight){
        loading=true;setTimeout(()=>skeleton.outerHTML=card('last','Live · 20'),7500);
      }
    });</script>"""
    context.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
    started = time.monotonic()
    try:
        shows = WhatnotBrowser(context).scan_keyword("needoh")
        assert len(shows) == 45
        assert shows[-1].session_id == "whatnot-show:last"
        assert all(show.session_id != "whatnot-show:decoy" for show in shows)
        assert context.pages[0].locator("#PLAYING").is_checked()
        assert not context.pages[0].locator("#CREATED").is_checked()
        assert time.monotonic() - started >= 7.5
    finally:
        context.close()


def test_pending_loading_is_not_success_even_without_a_scrollbar(browser):
    context = browser.new_context()
    page = context.new_page()
    page.set_content('<main><div data-type="SkeletonItem" aria-busy="true">Loading</div></main>')
    try:
        with pytest.raises(WhatnotPageChanged, match="无法确认已加载全部结果"):
            WhatnotBrowser._collect_search_results(
                page, "() => ({title:document.title,body:document.body.innerText,rows:[]})",
                timeout_seconds=1,
            )
    finally:
        context.close()


def test_live_filter_expands_clipped_input_and_waits_for_async_checked_prop(browser):
    context = browser.new_context()
    page = context.new_page()
    page.set_content("""<main>
    <button onclick="panel.style.height='auto';window.expanded++">Time of Show</button>
    <div id='panel' style='height:0;overflow:hidden'><label>Live
      <input id='PLAYING' type='checkbox'></label><input id='CREATED' type='checkbox'></div>
    <div id='grid'><section data-testid='livestream-card'>Tomorrow</section></div></main>
    <div style='display:none'><button>Time of Show</button></div>
    <script>window.expanded=0;
    PLAYING.onclick=e=>{e.preventDefault();setTimeout(()=>{
      PLAYING.checked=true;
      grid.innerHTML='<section data-testid="livestream-card"><a>Live · 8</a></section>';
    },350)};</script>""")
    try:
        WhatnotBrowser._select_live_filter(page)
        assert page.locator("#PLAYING").is_checked()
        assert page.evaluate("window.expanded") == 1
    finally:
        context.close()


def test_lost_live_filter_restarts_collection_and_discards_stale_cards(monkeypatch):
    class Page:
        def wait_for_timeout(self, _ms):
            pass

    events = iter([
        {"title": "", "body": "", "rows": [{"href": "https://example.test/live/stale"}]},
        {"filterLost": True},
        {"title": "", "body": "", "rows": [{"href": "https://example.test/live/current"}]},
        {"scrollable": False},
    ])
    restored = []
    monkeypatch.setattr("whatnot_monitor.whatnot._evaluate_page", lambda *_args, **_kwargs: next(events))
    monkeypatch.setattr(WhatnotBrowser, "_select_live_filter", lambda page: restored.append(page))
    page = Page()
    result = WhatnotBrowser._collect_search_results(page, "read cards")
    assert [item["href"] for item in result["rows"]] == ["https://example.test/live/current"]
    assert restored == [page]


def test_repeated_lost_live_filter_fails_closed(monkeypatch):
    class Page:
        def wait_for_timeout(self, _ms):
            pass

    def evaluate(_page, script, *, bounded=False):
        if script == "read cards":
            assert bounded  # Card observation is bounded; scrolling is an action.
            return {"title": "", "body": "", "rows": [{"href": "https://example.test/live/a"}]}
        assert not bounded
        return {"filterLost": True}

    restored = []
    monkeypatch.setattr("whatnot_monitor.whatnot._evaluate_page", evaluate)
    monkeypatch.setattr(WhatnotBrowser, "_select_live_filter", lambda page: restored.append(page))
    with pytest.raises(WhatnotPageChanged, match="反复被取消"):
        WhatnotBrowser._collect_search_results(Page(), "read cards")
    assert len(restored) == 2
