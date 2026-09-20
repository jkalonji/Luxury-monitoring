import json
from types import SimpleNamespace

import feedparser

import fetchers
from fetchers import _entries_to_articles, fallback_query, fetch_bluesky, fetch_feed, finalize, gnews_url, load_sources
from models import now_utc

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Dior names a new creative director</title><link>https://a.com/1</link>
<pubDate>{recent}</pubDate><description>&lt;p&gt;Big &lt;b&gt;news&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Old story</title><link>https://a.com/2</link><pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
<item><title></title><link>https://a.com/3</link></item>
<item><title>No date</title><link>https://a.com/4</link></item>
</channel></rss>"""

GNEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Chanel opens in Tokyo - Jing Daily</title><link>https://news.google.com/rss/articles/abc</link>
<pubDate>{recent}</pubDate><source url="https://jingdaily.com">Jing Daily</source></item></channel></rss>"""


def rfc822(dt=None):
    return (dt or now_utc()).strftime("%a, %d %b %Y %H:%M:%S GMT")


def test_gnews_url_locale_and_window():
    url = gnews_url("site:vogue.com when:7d", "fr", lookback_hours=36)
    assert url.startswith("https://news.google.com/rss/search?q=")
    assert "when%3A2d" in url and "hl=fr" in url and "gl=FR" in url and "ceid=FR:fr" in url
    assert "when%3A1d" in gnews_url("x when:7d", "en", lookback_hours=24)


def test_entries_to_articles_filters_cleans_and_flags_estimated_dates():
    feed = feedparser.parse(RSS.format(recent=rfc822()))
    src = {"name": "Vogue", "country": "US", "lang": "en", "filter": "luxury"}
    arts = _entries_to_articles(feed, src, cutoff=now_utc().replace(year=now_utc().year - 1))
    titles = [a.title for a in arts]
    assert titles == ["Dior names a new creative director", "No date"]   # old + empty-title items dropped
    assert arts[0].description == "Big news" and arts[0].filter == "luxury"
    assert not arts[0].published_is_estimated and arts[1].published_is_estimated


def test_entries_strip_gnews_publisher_suffix():
    feed = feedparser.parse(GNEWS.format(recent=rfc822()))
    arts = _entries_to_articles(feed, {"name": "Jing Daily"}, cutoff=now_utc().replace(year=2000), strip_publisher=True)
    assert arts[0].title == "Chanel opens in Tokyo" and arts[0].description == ""


def test_fallback_query():
    assert fallback_query({"url": "https://www.businessoffashion.com/arc/outboundfeeds/rss/"}) == \
        "site:businessoffashion.com when:2d"
    assert fallback_query({"url": "https://x.com/rss", "fallback_query": "site:lemonde.fr/m-mode when:7d"}) == \
        "site:lemonde.fr/m-mode when:7d"
    assert fallback_query({}) == ""


def _patch_feeds(monkeypatch, responses):
    """responses: {substring of url: xml text | Exception}"""
    calls = []

    async def fake_get_text(session, url):
        calls.append(url)
        for key, val in responses.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise RuntimeError("HTTP 404")

    monkeypatch.setattr(fetchers, "_get_text", fake_get_text)
    return calls


async def test_fetch_feed_direct_rss(monkeypatch):
    calls = _patch_feeds(monkeypatch, {"a.com/rss": RSS.format(recent=rfc822())})
    arts, report = await fetch_feed(None, {"name": "A", "type": "rss", "url": "https://a.com/rss"}, 36)
    assert arts[0].title.startswith("Dior") and report.error == "" and report.fetched == len(arts)
    assert len(calls) == 1


async def test_fetch_feed_falls_back_to_google_news(monkeypatch):
    calls = _patch_feeds(monkeypatch, {"a.com/rss": RuntimeError("HTTP 403"),
                                       "news.google.com": GNEWS.format(recent=rfc822())})
    arts, report = await fetch_feed(None, {"name": "A", "type": "rss", "url": "https://www.a.com/rss"}, 36)
    assert len(arts) == 1 and arts[0].title == "Chanel opens in Tokyo" and report.error == ""
    assert "site%3Aa.com" in calls[1]


async def test_fetch_feed_reports_error_when_everything_fails(monkeypatch):
    _patch_feeds(monkeypatch, {"a.com/rss": RuntimeError("HTTP 403"), "news.google.com": RuntimeError("HTTP 503")})
    arts, report = await fetch_feed(None, {"name": "A", "type": "rss", "url": "https://a.com/rss"}, 36)
    assert arts == [] and "HTTP 403" in report.error and "HTTP 503" in report.error


async def test_fetch_feed_gnews_source_has_no_second_fallback(monkeypatch):
    calls = _patch_feeds(monkeypatch, {"news.google.com": RuntimeError("HTTP 503")})
    arts, report = await fetch_feed(None, {"name": "G", "type": "gnews", "query": "site:x.com"}, 36)
    assert arts == [] and report.error == "HTTP 503" and len(calls) == 1


async def test_fetch_feed_reddit_is_social(monkeypatch):
    _patch_feeds(monkeypatch, {"reddit.com": RSS.format(recent=rfc822())})
    src = {"name": "Reddit r/fashion", "type": "reddit", "url": "https://reddit.com/r/fashion/top/.rss"}
    arts, _ = await fetch_feed(None, src, 36)
    assert arts and all(a.kind == "social" for a in arts)


def test_finalize_dedups_filters_and_tags(index, make_article):
    a = make_article("Dior wins", url="https://x/1")
    dup_url = make_article("Dior wins again", url="https://x/1")
    dup_title = make_article("Dior  WINS", url="https://x/2", source=a.source)
    other_source = make_article("Dior wins", url="https://x/3", source="WWD")
    off_topic = make_article("Budget vote in Luxembourg", url="https://x/4", filter="luxury")
    on_topic = make_article("Gucci and Alaia at the show", url="https://x/5", filter="luxury")
    unfiltered = make_article("Random news", url="https://x/6")
    out = finalize([a, dup_url, dup_title, other_source, off_topic, on_topic, unfiltered], index)
    assert [x.url for x in out] == ["https://x/1", "https://x/3", "https://x/5", "https://x/6"]
    assert out[0].entities == ["dior"] and set(out[2].entities) == {"gucci", "alaia"}


def test_load_sources_skips_disabled_and_comments(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"sources": [{"name": "A", "type": "rss", "url": "u"},
                                         {"name": "B", "type": "rss", "url": "u", "enabled": False},
                                         {"_comment": "x"}]}))
    assert [s["name"] for s in load_sources(str(p))] == ["A"]


def test_real_sources_file_is_valid():
    sources = load_sources("sources.json")
    names = [s["name"] for s in sources]
    assert len(names) == len(set(names))
    for s in sources:
        assert s["type"] in {"rss", "gnews", "reddit"}, s
        assert s.get("url") or s.get("query"), s
        assert s.get("filter", "luxury") == "luxury"
    magazines = ["Le Journal du Luxe", "Vogue", "Vogue France", "British Vogue", "FashionUnited", "AnOther Magazine",
                 "Jing Daily", "WWD", "Business of Fashion", "FashionNetwork", "Elle France", "Luxury Daily",
                 "New York Times", "10 Magazine", "Views", "GQ France", "British GQ", "Harper's Bazaar", "V Magazine",
                 "W Magazine", "Highsnobiety", "Tatler", "Le Monde", "Madame Figaro"]
    missing = {m for m in magazines if not any(m.lower() in n.lower() for n in names)}
    assert not missing, missing


# ---- Bluesky ---------------------------------------------------------------

def _post(text, likes=20, reposts=0, replies=0, handle="a.bsky.social", uri="at://did:x/app.bsky.feed.post/abc1"):
    return SimpleNamespace(uri=uri, author=SimpleNamespace(handle=handle), like_count=likes, repost_count=reposts,
                           reply_count=replies, record=SimpleNamespace(text=text, created_at="2026-09-19T10:00:00.000Z"))


class FakeBsky:
    def __init__(self, posts_by_query, fail_on=()):
        self.queries, self.posts_by_query, self.fail_on = [], posts_by_query, fail_on
        self.app = SimpleNamespace(bsky=SimpleNamespace(feed=SimpleNamespace(search_posts=self.search_posts)))

    async def search_posts(self, params):
        self.queries.append(params["q"])
        if params["q"] in self.fail_on:
            raise RuntimeError("boom")
        return SimpleNamespace(posts=self.posts_by_query.get(params["q"], []))


async def test_bluesky_requires_real_mention_and_min_score(index, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(fetchers.asyncio, "sleep", no_sleep)
    client = FakeBsky({
        "Dior": [_post("Dior is everything this season", likes=30, uri="at://x/p/1"),
                 _post("nothing relevant here", likes=99, uri="at://x/p/2"),      # fuzzy match: no mention
                 _post("Dior meh", likes=2, uri="at://x/p/3")],                     # below min_score
        "Demna": [_post("Demna at Gucci", likes=5, reposts=3, uri="at://x/p/4")],   # score 5 + 9 = 14
    }, fail_on={"Alaïa"})
    arts, report = await fetch_bluesky(index, 36, client=client)
    assert {a.url.split("/")[-1] for a in arts} == {"1", "4"}
    assert all(a.kind == "social" and a.source.startswith("Bluesky / @") for a in arts)
    assert {a.engagement for a in arts} == {30, 14}
    assert report.fetched == 2


async def test_bluesky_skipped_without_credentials(index):
    arts, report = await fetch_bluesky(index, 36)
    assert arts == [] and report.error.startswith("skipped")
