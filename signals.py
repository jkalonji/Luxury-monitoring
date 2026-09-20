"""Weak-signal metrics: daily series per entity/term + simple-threshold detection.

Series (table `signals`, one row per entity_id x metric x day):
  articles            magazine articles mentioning the entity (or containing the term)
  social_posts        Bluesky/Reddit posts mentioning the entity
  social_engagement   summed engagement of those posts
  wiki_views          Wikipedia pageviews (all configured languages)
  trends              Google Trends interest (0-100, relative to the batch)

Detection: the last complete day (D-1) fires on a metric when
  value >= min_value  AND  value >= RATIO x max(mean of the previous 7 days, floor).
"""

import asyncio
import logging
import urllib.parse as up
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean

import aiohttp

from entities import EntityIndex
from models import Article
from topics import source_words, term_counts

WIKI_USER_AGENT = "LuxuryMonitoring/1.0 (https://github.com/jkalonji/Luxury-monitoring; jeremie.kalonji@gmail.com)"
RATIO = 2.0
BASELINE_DAYS = 7
MIN_HISTORY_DAYS = 3

# min_value: absolute floor for the day's value; floor: lower bound of the baseline (avoids x-infinity on empty history)
THRESHOLDS = {
    "articles": {"min_value": 3, "floor": 1.0},
    "term": {"min_value": 3, "floor": 0.5},
    "social_posts": {"min_value": 3, "floor": 1.0},
    "social_engagement": {"min_value": 100, "floor": 25.0},
    "wiki_views": {"min_value": 100, "floor": 20.0},
    "trends": {"min_value": 15, "floor": 5.0},
}
METRIC_LABELS = {
    "articles": "articles", "term": "articles", "social_posts": "posts sociaux",
    "social_engagement": "engagement social", "wiki_views": "vues Wikipedia", "trends": "Google Trends",
}
COUNT_METRICS = {"articles", "social_posts", "social_engagement"}  # missing day == 0


def _day(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def yesterday() -> str:
    return _day(datetime.now(timezone.utc) - timedelta(days=1))


# ---------------------------------------------------------------------------
# Series derived from collected articles
# ---------------------------------------------------------------------------

def rows_to_articles(rows: list[dict]) -> list[Article]:
    return [Article(title=r["title"], url=r["url"], source=r.get("source") or "", country=r.get("country") or "",
                    published=r["published"], kind=r.get("kind") or "article", entities=r.get("entities") or [],
                    engagement=r.get("engagement") or 0, sentiment=r.get("sentiment") or "") for r in rows]


def coverage_start(article_rows: list[dict]) -> str | None:
    """First day with complete coverage: the day after the pipeline first ran.

    RSS feeds only expose their latest items, so a backfill under-counts older days. Volume series are
    therefore only trusted from the first full day of real daily collection.
    """
    days = [r["collected_at"][:10] for r in article_rows if r.get("collected_at")]
    return (datetime.fromisoformat(min(days)) + timedelta(days=1)).strftime("%Y-%m-%d") if days else None


def derive_article_rows(article_rows: list[dict], index: EntityIndex) -> list[dict]:
    """Daily article/social/term series from stored articles (idempotent: recomputed over the loaded window)."""
    start = coverage_start(article_rows)
    if start:
        article_rows = [r for r in article_rows if r["published"][:10] >= start]
    types = {e.id: e.type for e in index.entities}
    counts: dict[tuple, float] = defaultdict(float)
    by_day: dict[str, list[Article]] = defaultdict(list)
    for a in rows_to_articles(article_rows):
        day = a.day
        if a.kind == "article":
            by_day[day].append(a)
        for eid in a.entities:
            if eid not in types:
                continue
            if a.kind == "article":
                counts[(eid, "articles", day)] += 1
            else:
                counts[(eid, "social_posts", day)] += 1
                counts[(eid, "social_engagement", day)] += a.engagement
    rows = [{"entity_id": eid, "entity_type": types[eid], "metric": m, "day": d, "value": v}
            for (eid, m, d), v in counts.items()]
    exclude = index.alias_words() | source_words(a for arts in by_day.values() for a in arts)
    for day, arts in by_day.items():
        for term, stats in term_counts(arts, exclude).items():
            rows.append({"entity_id": term, "entity_type": "term", "metric": "articles", "day": day,
                         "value": float(stats["count"])})
    return rows


# ---------------------------------------------------------------------------
# External series: Wikipedia, Google Trends
# ---------------------------------------------------------------------------

async def _wiki_views(session: aiohttp.ClientSession, lang: str, title: str, start: str, end: str) -> dict[str, int]:
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{lang}.wikipedia/all-access/user/"
           f"{up.quote(title, safe='')}/daily/{start}/{end}")
    try:
        async with session.get(url, headers={"User-Agent": WIKI_USER_AGENT}, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                if r.status != 404:
                    logging.warning(f"[wiki] {lang}:{title} HTTP {r.status}")
                return {}
            data = await r.json()
    except Exception as e:
        logging.warning(f"[wiki] {lang}:{title} failed: {e}")
        return {}
    return {i["timestamp"][:8]: i["views"] for i in data.get("items", [])}


async def collect_wikipedia(index: EntityIndex, days: int = 35) -> list[dict]:
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start, end_s = (end - timedelta(days=days)).strftime("%Y%m%d"), end.strftime("%Y%m%d")
    sem = asyncio.Semaphore(8)

    async def one(session, entity):
        totals: dict[str, int] = defaultdict(int)
        for lang, title in entity.wikipedia.items():
            async with sem:
                for ymd, v in (await _wiki_views(session, lang, title, start, end_s)).items():
                    totals[ymd] += v
        return entity, totals

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(one(session, e) for e in index.entities if e.wikipedia))
    rows = [{"entity_id": e.id, "entity_type": e.type, "metric": "wiki_views",
             "day": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", "value": float(v)}
            for e, totals in results for ymd, v in totals.items()]
    logging.info(f"[wiki] {len(rows)} data points for {sum(1 for e in index.entities if e.wikipedia)} entities")
    return rows


def _trends_sync(entities: list, batch_size: int = 5, pause: float = 3.0) -> list[dict]:
    """Google Trends daily interest, last month, batches of 5 terms. Best effort: stops at the first block."""
    import time
    from pytrends.request import TrendReq
    rows: list[dict] = []
    pytrends = TrendReq(hl="en-US", tz=0, timeout=(10, 30))
    for i in range(0, len(entities), batch_size):
        batch = entities[i:i + batch_size]
        terms = [e.trends for e in batch]
        try:
            pytrends.build_payload(terms, timeframe="today 1-m")
            df = pytrends.interest_over_time()
        except Exception as e:
            logging.warning(f"[trends] stopped at batch {i // batch_size + 1}: {type(e).__name__}: {str(e)[:120]}")
            break
        if df is not None and not df.empty:
            if "isPartial" in df.columns:
                df = df[~df["isPartial"].astype(bool)]
            for entity in batch:
                if entity.trends not in df.columns:
                    continue
                for ts, v in df[entity.trends].items():
                    rows.append({"entity_id": entity.id, "entity_type": entity.type, "metric": "trends",
                                 "day": ts.strftime("%Y-%m-%d"), "value": float(v)})
        time.sleep(pause)
    logging.info(f"[trends] {len(rows)} data points")
    return rows


async def collect_trends(index: EntityIndex) -> list[dict]:
    try:
        return await asyncio.to_thread(_trends_sync, index.entities)
    except Exception as e:
        logging.warning(f"[trends] unavailable: {e}")
        return []


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_weak_signals(rows: list[dict], day: str | None = None, ratio: float = RATIO) -> list[dict]:
    """Return one entry per entity/term firing on >= 1 metric, ranked by score (convergence counts).

    Each: {entity_id, entity_type, day, hits: [{metric, value, baseline, ratio}], score}.
    """
    day = day or yesterday()
    series: dict[tuple, dict[str, float]] = defaultdict(dict)
    types: dict[str, str] = {}
    first_day: dict[str, str] = {}
    for r in rows:
        series[(r["entity_id"], r["metric"])][r["day"]] = r["value"]
        types[r["entity_id"]] = r["entity_type"]
        m = r["metric"]
        if m not in first_day or r["day"] < first_day[m]:
            first_day[m] = r["day"]

    eval_date = datetime.fromisoformat(day)
    per_entity: dict[str, list[dict]] = defaultdict(list)
    for (eid, metric), by_day in series.items():
        if metric == "social_posts":
            continue  # kept as context only: engagement is the reach signal
        key = "term" if types[eid] == "term" else metric
        if key not in THRESHOLDS or day not in by_day:
            continue
        # not enough history for this metric yet -> no alert (avoids day-one false positives)
        if (eval_date - datetime.fromisoformat(first_day[metric])).days < MIN_HISTORY_DAYS:
            continue
        value = by_day[day]
        prev_days = [_day(eval_date - timedelta(days=i)) for i in range(1, BASELINE_DAYS + 1)]
        if metric in COUNT_METRICS:
            # missing day == 0, but only for days the series actually covers
            prev = [by_day.get(d, 0.0) for d in prev_days if d >= first_day[metric]]
            if len(prev) < MIN_HISTORY_DAYS:
                continue
        else:
            prev = [by_day[d] for d in prev_days if d in by_day]
            if len(prev) < MIN_HISTORY_DAYS:
                continue
        baseline = mean(prev) if prev else 0.0
        th = THRESHOLDS[key]
        r = value / max(baseline, th["floor"])
        if value >= th["min_value"] and r >= ratio:
            per_entity[eid].append({"metric": metric, "value": value, "baseline": round(baseline, 2), "ratio": round(r, 1)})

    per_entity = _merge_term_variants(per_entity, types)
    out = []
    for eid, hits in per_entity.items():
        score = sum(min(h["ratio"], 10) for h in hits) * (1 + 0.5 * (len(hits) - 1))
        out.append({"entity_id": eid, "entity_type": types[eid], "day": day,
                    "hits": sorted(hits, key=lambda h: -h["ratio"]), "score": round(score, 1)})
    out.sort(key=lambda s: -s["score"])
    return out


def _merge_term_variants(per_entity: dict[str, list[dict]], types: dict[str, str]) -> dict[str, list[dict]]:
    """Drop a firing term contained in a longer firing term ('petra' vs 'petra fagerstrom') with a similar count."""
    terms = [t for t in per_entity if types[t] == "term"]
    drop = set()
    for t in terms:
        for u in terms:
            if t != u and t in u and per_entity[u][0]["value"] >= 0.8 * per_entity[t][0]["value"]:
                drop.add(t)
                break
    return {k: v for k, v in per_entity.items() if k not in drop}


def describe_signal(signal: dict, index: EntityIndex) -> str:
    label = index.label(signal["entity_id"]) if signal["entity_type"] != "term" else signal["entity_id"]
    parts = [f"{METRIC_LABELS['term' if signal['entity_type'] == 'term' else h['metric']]} ×{h['ratio']:g}"
             for h in signal["hits"]]
    return f"{label} — " + ", ".join(parts)
