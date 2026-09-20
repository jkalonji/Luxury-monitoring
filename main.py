"""Luxury Radar - daily pipeline: collect, classify, detect topics, compute signals, e-mail the recap.

Usage:
  python main.py                       # daily run (last 36h)
  python main.py --backfill-days 14    # first run: load the last 14 days to seed the signal baselines (no e-mail)
  python main.py --dry-run             # everything except sending the e-mail (preview in output/)
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import asdict

from classify import classify_articles, groq_model, make_client
from entities import EntityIndex
from fetchers import fetch_all, load_sources
from mailer import build_daily, deliver
from models import Article, today_str
from signals import (collect_trends, collect_wikipedia, derive_article_rows, detect_weak_signals, rows_to_articles,
                     yesterday)
from store import Store, article_row, get_store
from topics import apply_story_matches, extract_topic_clusters, match_clusters_to_stories, name_topic_clusters

MAX_SOCIAL_POSTS = 150
HISTORY_DAYS = 40


def limit_social(articles: list[Article], cap: int = MAX_SOCIAL_POSTS) -> list[Article]:
    """Keep every magazine article; keep only the most engaging social posts (Reddit RSS carries no score: kept last)."""
    magazine = [a for a in articles if a.kind == "article"]
    social = sorted((a for a in articles if a.kind == "social"), key=lambda a: -a.engagement)[:cap]
    return magazine + social


async def detect_topics(new: list[Article], store: Store, client, model: str, today: str) -> list[dict]:
    """Cluster today's new articles, name clusters, track stories across days, flag hot articles in-place."""
    clusters = extract_topic_clusters(new, min_articles=3) or extract_topic_clusters(new, min_articles=2)
    if not clusters:
        return []
    clusters = await name_topic_clusters(clusters, client, model)
    matches = match_clusters_to_stories(clusters, store.open_stories())
    url_to_story = apply_story_matches(store, clusters, matches, today)
    store.close_stale_stories(today)

    best: dict[str, dict] = {}
    for c in clusters:
        for a in c["articles"]:
            if a.url not in best or c["score"] > best[a.url]["score"]:
                best[a.url] = c
    for a in new:
        c = best.get(a.url)
        if c:
            a.hot_topic, a.hot_reason, a.mention_count = True, c["label"], c["article_count"]
            a.supa_hot = c["article_count"] >= 5 and c["source_count"] >= 3
            a.story_id = url_to_story.get(a.url)
    return clusters


async def refresh_signals(store: Store, index: EntityIndex, external: bool) -> list[dict]:
    """Recompute article-derived series, refresh Wikipedia/Trends series, return D-1 weak signals."""
    rows = derive_article_rows(store.load_articles(HISTORY_DAYS), index)
    if external:
        wiki, trends = await asyncio.gather(collect_wikipedia(index, HISTORY_DAYS), collect_trends(index))
        rows += wiki + trends
    store.save_signals(rows)
    return detect_weak_signals(store.load_signals(HISTORY_DAYS), day=yesterday())


async def run(args: argparse.Namespace) -> int:
    index = EntityIndex.load(args.entities)
    sources = load_sources(args.sources)
    store = get_store()
    today = today_str()
    lookback = args.backfill_days * 24 if args.backfill_days else args.lookback_hours

    articles, reports = await fetch_all(sources, index, lookback_hours=lookback)
    failed = [r.name for r in reports if r.error and not r.error.startswith("skipped")]
    if len(failed) > len(reports) / 3:
        print(f"::warning::{len(failed)}/{len(reports)} sources failed: {', '.join(failed)}")

    known = store.recent_urls(lookback // 24 + 14)
    new = limit_social([a for a in articles if a.url not in known])
    logging.info(f"{len(new)} new items ({len(articles) - len(new)} already stored)")

    client = make_client()
    if args.backfill_days:
        # cluster day by day so a topic never spans unrelated days
        clusters = []
        for day in sorted({a.day for a in new}):
            await detect_topics([a for a in new if a.day == day], store, client, groq_model(), day)
    else:
        clusters = await detect_topics(new, store, client, groq_model(), today) if new else []
    classified = await classify_articles(new, client=client) if new else []
    store.save_articles(classified)
    logging.info(f"Saved {len(classified)} items")

    signals = await refresh_signals(store, index, external=not args.no_external)
    logging.info(f"{len(signals)} weak signal(s) on {yesterday()}")

    if args.backfill_days:
        logging.info("Backfill run: e-mail skipped")
        return 0
    if not classified:
        logging.info("Nothing new - no e-mail")
        return 0

    kept_urls = {a.url for a in classified}
    mail_clusters = [{**c, "articles": [article_row(a) for a in c["articles"] if a.url in kept_urls]} for c in clusters]
    mail_clusters = [c for c in mail_clusters if c["articles"]]
    subject, html_body, text_body = build_daily(
        day=today, new_rows=[article_row(a) for a in classified], clusters=mail_clusters, signals=signals,
        index=index, dashboard_url=os.environ.get("DASHBOARD_URL", ""), reports=[asdict(r) for r in reports])
    if not deliver(subject, html_body, text_body, "daily", dry_run=args.dry_run):
        print("::error::daily e-mail could not be sent")
        return 1
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sources", default="sources.json")
    p.add_argument("--entities", default="entities.json")
    p.add_argument("--lookback-hours", type=int, default=36)
    p.add_argument("--backfill-days", type=int, default=0, help="seed history: fetch N days, skip the e-mail")
    p.add_argument("--no-external", action="store_true", help="skip Wikipedia / Google Trends")
    p.add_argument("--dry-run", action="store_true", help="do not send the e-mail")
    return p.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    sys.exit(asyncio.run(run(parse_args())))
