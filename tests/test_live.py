"""Hit the real services. Excluded from the default run; use `python -m pytest -m live`."""

import aiohttp
import pytest

from entities import EntityIndex
from fetchers import fetch_all, load_sources
from signals import _wiki_views, collect_trends

pytestmark = pytest.mark.live


async def test_sources_return_items():
    index = EntityIndex.load("entities.json")
    sources = [s for s in load_sources("sources.json") if s["type"] != "reddit"]
    articles, reports = await fetch_all(sources, index, lookback_hours=72)
    ok = [r for r in reports if r.fetched and r.name != "Bluesky"]
    print({r.name: r.fetched for r in reports})
    assert len(ok) >= 0.7 * len(sources), [r for r in reports if r.error]
    assert len(articles) > 100
    assert all(a.url.startswith("http") and a.title for a in articles)


async def test_every_wikipedia_title_resolves():
    index = EntityIndex.load("entities.json")
    bad = []
    async with aiohttp.ClientSession() as session:
        for e in index.entities:
            for lang, title in e.wikipedia.items():
                if not await _wiki_views(session, lang, title, "20260801", "20260810"):
                    bad.append((e.id, lang, title))
    assert not bad, bad


async def test_google_trends_answers():
    index = EntityIndex.load("entities.json")
    index.entities = index.entities[:5]
    rows = await collect_trends(index)
    assert rows, "Google Trends returned nothing (blocked or rate-limited)"
