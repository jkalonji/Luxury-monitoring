"""Source collection: direct RSS, Google News fallback, Reddit RSS, Bluesky search."""

import asyncio
import json
import logging
import os
import re
import urllib.parse as up
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import mktime

import aiohttp
import feedparser
import requests

from entities import EntityIndex, normalize
from models import Article, now_utc

try:
    from atproto import AsyncClient
    _ATPROTO = True
except ImportError:  # pragma: no cover
    _ATPROTO = False

USER_AGENT = "Mozilla/5.0 (compatible; LuxuryMonitoring/1.0; +https://github.com/jkalonji/Luxury-monitoring)"
MAX_ENTRIES_PER_SOURCE = 100
_GNEWS_LOCALES = {"fr": ("fr", "FR", "FR:fr"), "en": ("en-US", "US", "US:en")}


@dataclass
class SourceReport:
    name: str
    fetched: int = 0
    kept: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def parse_feed_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
    return None


def gnews_url(query: str, lang: str = "en", lookback_hours: int | None = None) -> str:
    """Google News RSS search URL. `when:Nd` in the query is rewritten to honour the lookback window."""
    if lookback_hours:
        days = max(1, -(-lookback_hours // 24))
        query = re.sub(r"when:\d+[dh]", f"when:{days}d", query)
    hl, gl, ceid = _GNEWS_LOCALES.get(lang, _GNEWS_LOCALES["en"])
    return f"https://news.google.com/rss/search?q={up.quote(query)}&hl={hl}&gl={gl}&ceid={ceid}"


def _entries_to_articles(feed, source: dict, cutoff: datetime, strip_publisher: bool = False,
                         kind: str = "article") -> list[Article]:
    articles = []
    for entry in feed.entries[:MAX_ENTRIES_PER_SOURCE]:
        pub = parse_feed_date(entry)
        if pub and pub < cutoff:
            continue
        title = clean_html(entry.get("title", ""))
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        if strip_publisher:
            publisher = (entry.get("source") or {}).get("title", "")
            if publisher and title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3].rstrip()
        description = "" if strip_publisher else clean_html(entry.get("summary", ""))[:300]
        articles.append(Article(
            title=title, url=link, source=source["name"], country=source.get("country", "🌍"),
            published=(pub or now_utc()).isoformat(), published_is_estimated=pub is None,
            description=description, kind=kind, lang=source.get("lang", "en"), filter=source.get("filter", ""),
        ))
    return articles


def _requests_get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.text


async def _get_text(session: aiohttp.ClientSession, url: str) -> str:
    """GET a feed. Some CDNs (Cloudflare) challenge aiohttp's TLS fingerprint but not `requests`'s: retry with it on 403."""
    async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status == 200:
            return await resp.text()
        status = resp.status
    if status == 403:
        return await asyncio.to_thread(_requests_get, url)
    raise RuntimeError(f"HTTP {status}")


def fallback_query(source: dict) -> str:
    """Google News query used when a direct feed fails: explicit `fallback_query`, else `site:<host>`."""
    if source.get("fallback_query"):
        return source["fallback_query"]
    host = up.urlparse(source.get("url", "")).netloc.removeprefix("www.")
    return f"site:{host} when:2d" if host else ""


# ---------------------------------------------------------------------------
# Fetchers (each returns (articles, SourceReport))
# ---------------------------------------------------------------------------

async def _load_feed(session: aiohttp.ClientSession, url: str):
    feed = feedparser.parse(await _get_text(session, url))
    if not feed.entries:
        raise RuntimeError("empty feed")
    return feed


async def fetch_feed(session: aiohttp.ClientSession, source: dict, lookback_hours: int) -> tuple[list[Article], SourceReport]:
    """Direct RSS (type=rss), Google News (type=gnews) or Reddit RSS (type=reddit).

    A failing direct RSS falls back to a Google News `site:` query so a broken feed does not silence a magazine.
    """
    report = SourceReport(source["name"])
    stype = source["type"]
    lang = source.get("lang", "en")
    via_gnews = stype == "gnews"
    try:
        url = gnews_url(source["query"], lang, lookback_hours) if via_gnews else source["url"]
        feed = await _load_feed(session, url)
    except Exception as e:
        direct_error = str(e) or type(e).__name__
        query = fallback_query(source) if stype == "rss" else ""
        if not query:
            report.error = direct_error
            logging.error(f"[{source['name']}] {direct_error}")
            return [], report
        try:
            feed = await _load_feed(session, gnews_url(query, lang, lookback_hours))
            via_gnews = True
            logging.warning(f"[{source['name']}] direct feed failed ({direct_error}) - using Google News fallback")
        except Exception as e2:
            report.error = f"{direct_error}; fallback: {e2}"
            logging.error(f"[{source['name']}] {report.error}")
            return [], report
    cutoff = now_utc() - timedelta(hours=lookback_hours)
    articles = _entries_to_articles(
        feed, source, cutoff, strip_publisher=via_gnews,
        kind="social" if stype == "reddit" else "article",
    )
    report.fetched = len(articles)
    logging.info(f"[{source['name']}] {len(articles)} items")
    return articles, report


def _engagement(post) -> int:
    """Weighted virality: likes + 3 x reposts + 2 x replies (reposts spread content the most)."""
    return (post.like_count or 0) + 3 * (post.repost_count or 0) + 2 * (post.reply_count or 0)


def _bsky_query(entity) -> str:
    return f'"{entity.label}"' if " " in entity.label else entity.label


async def fetch_bluesky(index: EntityIndex, lookback_hours: int, min_score: int = 10,
                        per_entity: int = 5, client=None) -> tuple[list[Article], SourceReport]:
    """Search Bluesky for each tracked entity; keep the most engaging posts. Needs BSKY_HANDLE/BSKY_APP_PASSWORD."""
    report = SourceReport("Bluesky")
    if client is None:
        handle, password = os.environ.get("BSKY_HANDLE"), os.environ.get("BSKY_APP_PASSWORD")
        if not _ATPROTO or not handle or not password:
            report.error = "skipped (atproto or BSKY credentials missing)"
            logging.warning(f"[Bluesky] {report.error}")
            return [], report
        try:
            client = AsyncClient()
            await client.login(handle, password)
        except Exception as e:
            report.error = f"login failed: {e}"
            logging.error(f"[Bluesky] {report.error}")
            return [], report

    since = (now_utc() - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: set[str] = set()
    out: list[Article] = []
    for i, entity in enumerate(index.entities):
        try:
            resp = await client.app.bsky.feed.search_posts(params={
                "q": _bsky_query(entity), "limit": 50, "sort": "top", "since": since})
        except Exception as e:
            logging.warning(f"[Bluesky] search '{entity.label}' failed: {e}")
            continue
        kept = 0
        for post in resp.posts or []:
            score = _engagement(post)
            text = (post.record.text or "").strip()
            uri = post.uri
            if score < min_score or not text or uri in seen:
                continue
            if entity.id not in index.tag(text):   # search is fuzzy: require a real mention
                continue
            seen.add(uri)
            first_line = text.split("\n")[0].strip()
            title = first_line if len(first_line) <= 120 else first_line[:117] + "…"
            try:
                created = datetime.fromisoformat(post.record.created_at.replace("Z", "+00:00")).isoformat()
                estimated = False
            except Exception:
                created, estimated = now_utc().isoformat(), True
            out.append(Article(
                title=title, url=f"https://bsky.app/profile/{post.author.handle}/post/{uri.split('/')[-1]}",
                source=f"Bluesky / @{post.author.handle}", country="🌍", published=created,
                published_is_estimated=estimated, description=text[:300], kind="social", engagement=score,
            ))
            kept += 1
            if kept >= per_entity:
                break
        if i < len(index.entities) - 1:
            await asyncio.sleep(0.5)
    report.fetched = len(out)
    logging.info(f"[Bluesky] {len(out)} posts across {len(index.entities)} entities")
    return out, report


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def load_sources(path: str = "sources.json") -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    sources = [s for s in data["sources"] if "name" in s and s.get("enabled", True)]
    logging.info(f"Loaded {len(sources)} enabled sources")
    return sources


def finalize(articles: list[Article], index: EntityIndex) -> list[Article]:
    """Deduplicate by URL and by (source, title), apply the luxury filter to generalist sources, tag entities."""
    seen: set[str] = set()
    out = []
    for a in articles:
        key = f"{a.source}|{' '.join(normalize(a.title).split())}"
        if a.url in seen or key in seen:
            continue
        seen.update((a.url, key))
        text = f"{a.title} {a.description}"
        if a.filter == "luxury" and not index.is_luxury(text):
            continue
        a.entities = index.tag(text)
        out.append(a)
    return out


async def fetch_all(sources: list[dict], index: EntityIndex, lookback_hours: int = 36,
                    bluesky_client=None) -> tuple[list[Article], list[SourceReport]]:
    async with aiohttp.ClientSession() as session:
        feed_sources = [s for s in sources if s["type"] in ("rss", "gnews")]
        results = list(await asyncio.gather(*(fetch_feed(session, s, lookback_hours) for s in feed_sources),
                                            return_exceptions=True))
        # Reddit rate-limits bursts (HTTP 429): fetch its subreddits one by one, best effort.
        reddit_sources = [s for s in sources if s["type"] == "reddit"]
        for i, s in enumerate(reddit_sources):
            results.append((await asyncio.gather(fetch_feed(session, s, lookback_hours), return_exceptions=True))[0])
            if i < len(reddit_sources) - 1:
                await asyncio.sleep(3)
        feed_sources += reddit_sources
    articles: list[Article] = []
    reports: list[SourceReport] = []
    for src, res in zip(feed_sources, results):
        if isinstance(res, Exception):
            reports.append(SourceReport(src["name"], error=str(res)))
            logging.error(f"[{src['name']}] task failed: {res}")
        else:
            articles.extend(res[0])
            reports.append(res[1])

    bsky_articles, bsky_report = await fetch_bluesky(index, lookback_hours, client=bluesky_client)
    articles.extend(bsky_articles)
    reports.append(bsky_report)

    kept = finalize(articles, index)
    per_source: dict[str, int] = {}
    for a in kept:
        key = "Bluesky" if a.source.startswith("Bluesky") else a.source
        per_source[key] = per_source.get(key, 0) + 1
    for r in reports:
        r.kept = per_source.get(r.name, 0)
    logging.info(f"{len(kept)}/{len(articles)} items kept after dedup + luxury filter")
    return kept, reports
