from datetime import datetime, timedelta, timezone

import pytest

from signals import (THRESHOLDS, collect_trends, collect_wikipedia, coverage_start, derive_article_rows,
                     detect_weak_signals, describe_signal, yesterday)

DAY = "2026-09-19"


def d(offset: int, base: str = DAY) -> str:
    return (datetime.fromisoformat(base) + timedelta(days=offset)).strftime("%Y-%m-%d")


def series(eid, metric, values, etype="house", end=DAY):
    """values[-1] is `end`; earlier values go back one day each."""
    n = len(values)
    return [{"entity_id": eid, "entity_type": etype, "metric": metric, "day": d(i - n + 1, end), "value": float(v)}
            for i, v in enumerate(values)]


def test_fires_at_twice_the_baseline():
    rows = series("dior", "articles", [2, 2, 2, 2, 2, 2, 2, 6])
    (sig,) = detect_weak_signals(rows, day=DAY)
    assert sig["entity_id"] == "dior"
    hit = sig["hits"][0]
    assert hit["metric"] == "articles" and hit["value"] == 6 and hit["baseline"] == 2 and hit["ratio"] == 3


def test_does_not_fire_below_ratio_or_min_value():
    assert detect_weak_signals(series("dior", "articles", [5, 5, 5, 5, 5, 5, 5, 9]), day=DAY) == []   # x1.8
    assert detect_weak_signals(series("dior", "articles", [0, 0, 0, 0, 0, 0, 0, 2]), day=DAY) == []   # below min_value 3


def test_needs_history():
    assert detect_weak_signals(series("dior", "articles", [1, 9]), day=DAY) == []
    assert detect_weak_signals(series("dior", "wiki_views", [100, 100, 900]), day=DAY) == []


def test_zero_history_uses_floor_not_infinity():
    rows = series("dior", "articles", [0, 0, 0, 0, 0, 0, 0, 4])
    (sig,) = detect_weak_signals(rows, day=DAY)
    assert sig["hits"][0]["ratio"] == 4 / THRESHOLDS["articles"]["floor"]


def test_missing_days_count_as_zero_for_count_metrics():
    # articles on D-7 and D-6 only, then nothing until a spike on D: the empty days are zeros, baseline < 2
    rows = [r for r in series("dior", "articles", [3, 3, 0, 0, 0, 0, 0, 6]) if r["value"] > 0]
    (sig,) = detect_weak_signals(rows, day=DAY)
    assert sig["hits"][0]["baseline"] == round((3 + 3) / 7, 2)


def test_gaps_do_not_dilute_external_baseline():
    wiki = series("dior", "wiki_views", [200, 200, 200, 200, 200, 200, 200, 350])
    wiki = [r for r in wiki if r["day"] not in (d(-2), d(-3))]
    assert detect_weak_signals(wiki, day=DAY) == []       # 350/200 = 1.75, gaps ignored (not 0)


def test_social_posts_is_context_only():
    assert detect_weak_signals(series("dior", "social_posts", [1, 1, 1, 1, 1, 1, 1, 30]), day=DAY) == []


def test_convergence_ranks_higher_and_min_values_apply_per_metric():
    rows = (series("dior", "articles", [2, 2, 2, 2, 2, 2, 2, 8]) + series("dior", "wiki_views", [200] * 7 + [900]) +
            series("gucci", "articles", [2, 2, 2, 2, 2, 2, 2, 8]) +
            series("alaia", "wiki_views", [10] * 7 + [50]))       # x5 but below min_value 100
    sigs = detect_weak_signals(rows, day=DAY)
    assert [s["entity_id"] for s in sigs] == ["dior", "gucci"]
    assert {h["metric"] for h in sigs[0]["hits"]} == {"articles", "wiki_views"}
    assert sigs[0]["score"] > sigs[1]["score"]


def test_ratio_is_capped_in_score():
    rows = series("dior", "articles", [1] * 7 + [1000])
    assert detect_weak_signals(rows, day=DAY)[0]["score"] == 10


def test_term_signals_and_variant_merge():
    rows = (series("petra fagerstrom", "articles", [0, 0, 0, 0, 0, 0, 0, 5], etype="term") +
            series("petra", "articles", [0, 0, 0, 0, 0, 0, 0, 5], etype="term") +
            series("tariffs", "articles", [0, 0, 0, 0, 0, 0, 0, 4], etype="term"))
    ids = [s["entity_id"] for s in detect_weak_signals(rows, day=DAY)]
    assert "petra" not in ids and "petra fagerstrom" in ids and "tariffs" in ids


def test_evaluation_only_looks_at_requested_day():
    rows = series("dior", "articles", [2, 2, 2, 2, 2, 2, 2, 6, 2])   # spike on D-1, D is calm
    assert detect_weak_signals(rows, day=DAY) == []
    assert len(detect_weak_signals(rows, day=d(-1))) == 1


def test_describe_signal(index):
    sig = detect_weak_signals(series("dior", "articles", [2] * 7 + [6]), day=DAY)[0]
    assert describe_signal(sig, index) == "Dior — articles ×3"


# ---- derived series ----------------------------------------------------------

def rows_for(make_article, index, spec):
    """spec: list of (days_ago, title, entities, kind, collected_days_ago)"""
    rows = []
    for days_ago, title, ents, kind, coll in spec:
        a = make_article(title, source=f"S{len(rows) % 3}", days_ago=days_ago, entities=ents, kind=kind)
        r = {k: getattr(a, k) for k in ("title", "url", "source", "country", "published", "kind", "entities", "engagement", "sentiment")}
        r["collected_at"] = (datetime.now(timezone.utc) - timedelta(days=coll)).isoformat()
        rows.append(r)
    return rows


def test_coverage_start_is_day_after_first_collection():
    assert coverage_start([]) is None
    rows = [{"collected_at": "2026-09-10T08:00:00"}, {"collected_at": "2026-09-14T08:00:00"}]
    assert coverage_start(rows) == "2026-09-11"


def test_derive_article_rows_counts_and_respects_coverage(make_article, index):
    spec = [(5, "Old backfilled Dior story", ["dior"], "article", 5),      # published before coverage start
            (1, "Dior story one", ["dior"], "article", 3),
            (1, "Dior story two", ["dior", "gucci"], "article", 1),
            (1, "Bluesky Dior post", ["dior"], "social", 1)]
    rows = rows_for(make_article, index, spec)
    for r in rows:
        r["engagement"] = 40 if r["kind"] == "social" else 0
    out = derive_article_rows(rows, index)
    y = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    get = lambda e, m, day=y: next((r["value"] for r in out if r["entity_id"] == e and r["metric"] == m and r["day"] == day), None)  # noqa: E731
    assert get("dior", "articles") == 2 and get("gucci", "articles") == 1
    assert get("dior", "social_posts") == 1 and get("dior", "social_engagement") == 40
    old = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    assert get("dior", "articles", old) is None       # before coverage start: not counted


def test_derive_emits_term_series_needing_two_sources(make_article, index):
    spec = [(1, "Tariffs hit luxury demand", [], "article", 3), (1, "Tariffs worry buyers", [], "article", 3),
            (1, "Tariffs again", [], "article", 3)]
    out = derive_article_rows(rows_for(make_article, index, spec), index)
    terms = {r["entity_id"]: r for r in out if r["entity_type"] == "term"}
    assert terms["tariffs"]["value"] == 3.0


# ---- external collectors ---------------------------------------------------------

class FakeResp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, handler):
        self.handler, self.urls = handler, []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.handler(url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_collect_wikipedia_sums_languages_and_skips_failures(index, monkeypatch):
    import signals

    def handler(url):
        if "Missing" in url:
            return FakeResp(404, {})
        return FakeResp(200, {"items": [{"timestamp": "2026091700", "views": 120}, {"timestamp": "2026091800", "views": 80}]})

    monkeypatch.setattr(signals.aiohttp, "ClientSession", lambda **kw: FakeSession(handler))
    idx = index
    idx.by_id["dior"].wikipedia = {"en": "Dior", "fr": "Christian_Dior"}
    idx.by_id["gucci"].wikipedia = {"en": "Missing"}
    rows = await collect_wikipedia(idx, 5)
    dior = {r["day"]: r["value"] for r in rows if r["entity_id"] == "dior"}
    assert dior == {"2026-09-17": 240.0, "2026-09-18": 160.0}       # en + fr summed
    assert not [r for r in rows if r["entity_id"] == "gucci"]
    assert all(r["metric"] == "wiki_views" for r in rows)


async def test_collect_trends_survives_blocked_client(index, monkeypatch):
    import signals

    def boom(*a, **kw):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(signals, "_trends_sync", boom)
    assert await collect_trends(index) == []
