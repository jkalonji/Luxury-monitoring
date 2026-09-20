"""End-to-end runs of the daily pipeline, weekly report and dashboard export against a temporary SQLite DB,
with the network (feeds, Groq, Wikipedia, Trends, Resend) replaced by fakes."""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import dashboard
import main
import weekly_report
from fetchers import SourceReport
from models import Article, now_utc
from store import SqliteStore, get_store


ENTITIES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "entities.json")


def today_iso(hour=8):
    return now_utc().replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def fake_feed(n_dior=4):
    """A realistic mini-day: a topic covered by 3 outlets, Dior/Gucci news, one off-topic generalist article."""
    arts = [
        Article("Demna Gucci debut confirmed for March", "https://v/1", "Vogue", "US", today_iso(), entities=["gucci"]),
        Article("Demna Gucci: what we know so far", "https://w/1", "WWD", "US", today_iso(), entities=["gucci"]),
        Article("Inside the Demna Gucci era", "https://b/1", "Business of Fashion", "UK", today_iso(), entities=["gucci"]),
        Article("Dior ouvre une boutique éphémère à Tokyo", "https://v/2", "Vogue France", "FR", today_iso(), entities=["dior"]),
        Article("Bluesky: Dior is trending", "https://bsky.app/1", "Bluesky / @a", "🌍", today_iso(), kind="social",
                engagement=50, entities=["dior"]),
    ]
    for i in range(n_dior - 1):
        arts.append(Article(f"Dior record growth story {i}", f"https://x/{i}", f"Src{i}", "FR", today_iso(), entities=["dior"]))
    return arts


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DB", str(tmp_path / "t.db"))
    monkeypatch.chdir(tmp_path)          # output/ previews go to the tmp dir
    monkeypatch.setenv("DASHBOARD_URL", "https://example.github.io/Luxury-monitoring/")
    sent = []
    monkeypatch.setattr(main, "deliver", lambda subject, html, text, name, dry_run=False:
                        (sent.append((name, subject, html, text, dry_run)) or True))

    async def fake_fetch_all(sources, index, lookback_hours=36, bluesky_client=None):
        return fake_feed(), [SourceReport("Vogue", fetched=4, kept=4), SourceReport("WWD", error="HTTP 403")]

    monkeypatch.setattr(main, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(main, "load_sources", lambda path: [])

    async def no_external(index, days=35):
        return []

    async def no_trends(index):
        return []

    monkeypatch.setattr(main, "collect_wikipedia", no_external)
    monkeypatch.setattr(main, "collect_trends", no_trends)
    return sent


def args(**kw):
    ns = main.parse_args([])
    ns.entities = ENTITIES
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


async def test_daily_run_saves_classifies_and_emails(env):
    assert await main.run(args()) == 0
    store = get_store()
    rows = store.load_articles(2)
    assert len(rows) == 8
    assert all(r["category"] and r["sentiment"] for r in rows)                   # rule-based fallback (no GROQ key)
    boutique = next(r for r in rows if "boutique" in r["title"])
    assert boutique["category"] == "Ouverture de boutique / pop-up"
    hot = [r for r in rows if r["hot_topic"]]
    # two topics, each covered by 3 outlets: the Demna/Gucci one and the Dior "record growth" one
    assert len(hot) == 6 and len({r["hot_reason"] for r in hot}) == 2 and all(r["story_id"] for r in hot)
    assert "Demna Gucci" in {r["hot_reason"] for r in hot}
    assert [s["label"] for s in store.open_stories()]
    assert store.load_signals(5) == []       # day one: volume series start the day after the first collection

    assert len(env) == 1
    name, subject, html, text, dry = env[0]
    assert name == "daily" and dry is False and "7 articles" in subject          # the social post is not an article
    assert "Sources en échec" in html and "WWD" in html
    assert "https://example.github.io/Luxury-monitoring/" in html


async def test_second_run_finds_nothing_new_and_sends_nothing(env):
    await main.run(args())
    env.clear()
    assert await main.run(args()) == 0
    assert env == [] and len(get_store().load_articles(2)) == 8


async def test_backfill_run_skips_email_and_clusters_per_day(env, monkeypatch):
    old = fake_feed()
    for i, a in enumerate(old):
        a.published = (now_utc() - timedelta(days=3 + i % 2)).replace(hour=8).isoformat()

    async def fetch(sources, index, lookback_hours=36, bluesky_client=None):
        return old, []

    monkeypatch.setattr(main, "fetch_all", fetch)
    assert await main.run(args(backfill_days=14)) == 0
    assert env == [] and len(get_store().load_articles(10)) == 8


async def test_email_failure_returns_nonzero_but_data_is_saved(env, monkeypatch, capsys):
    monkeypatch.setattr(main, "deliver", lambda *a, **k: False)
    assert await main.run(args()) == 1
    assert len(get_store().load_articles(2)) == 8
    assert "::error::" in capsys.readouterr().out


async def test_dry_run_flag_is_forwarded(env):
    await main.run(args(dry_run=True))
    assert env[0][4] is True


async def test_run_survives_total_source_failure(env, monkeypatch, capsys):
    async def fetch(sources, index, lookback_hours=36, bluesky_client=None):
        return [], [SourceReport(f"S{i}", error="HTTP 500") for i in range(6)]

    monkeypatch.setattr(main, "fetch_all", fetch)
    assert await main.run(args()) == 0
    assert env == [] and "::warning::6/6 sources failed" in capsys.readouterr().out


def test_limit_social_keeps_all_articles_and_top_posts(make_article):
    arts = [make_article(f"a{i}") for i in range(3)] + \
        [make_article(f"p{i}", kind="social", engagement=i) for i in range(10)]
    out = main.limit_social(arts, cap=4)
    assert len([a for a in out if a.kind == "article"]) == 3
    assert sorted(a.engagement for a in out if a.kind == "social") == [6, 7, 8, 9]


# ---- dashboard export -----------------------------------------------------------

def seed(store, index, days=10):
    arts = []
    for d in range(days):
        for i in range(3):
            arts.append(Article(f"Dior story {d}-{i}", f"https://s/{d}/{i}", f"Src{i}", "FR",
                                (now_utc() - timedelta(days=d)).replace(hour=9).isoformat(), entities=["dior"],
                                category="Business & Finance", sentiment=["Positif", "Neutre", "Négatif"][i]))
    store.save_articles(arts)
    yday = (now_utc() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = [{"entity_id": "dior", "entity_type": "house", "metric": "wiki_views",
             "day": (now_utc() - timedelta(days=k)).strftime("%Y-%m-%d"), "value": 100.0 if k else 900.0}
            for k in range(1, 10)]
    rows[0]["value"] = 900.0
    store.save_signals(rows)
    return yday


def extract_payload(html: str) -> dict:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_dashboard_export_embeds_valid_json(tmp_path, index):
    store = SqliteStore(":memory:")
    seed(store, index)
    out = dashboard.export(str(tmp_path / "site" / "index.html"), 30, store=store, index=index)
    html = open(out, encoding="utf-8").read()
    assert "/*__DATA__*/" not in html and "null</script>" not in html.split('id="data"')[1][:80]
    data = extract_payload(html)
    assert len(data["articles"]) == 30 and data["days"] == 30
    assert [e["id"] for e in data["entities"]] == ["dior", "alaia", "gucci", "lvmh", "demna"]
    assert len(data["categories"]) == 10
    sig = data["signals"][0]
    assert sig["id"] == "dior" and sig["hits"][0]["metric"] == "wiki_views" and len(sig["hits"][0]["series"]) >= 8
    assert data["signals_day"]


def test_dashboard_render_escapes_script_terminators(index):
    store = SqliteStore(":memory:")
    store.save_articles([Article("</script><script>alert(1)</script> \u2028", "https://s/1", "S", "FR",
                                 now_utc().isoformat(), entities=["dior"])])
    html = dashboard.render_html(dashboard.build_payload(store, index, 7))
    assert "</script><script>alert(1)" not in html and "\u2028" not in html
    assert extract_payload(html)["articles"][0]["t"].startswith("</script>")


def test_dashboard_template_has_all_requested_sections():
    html = open("dashboard_template.html", encoding="utf-8").read()
    for needle in ["Signaux faibles", "Sentiment", "Nuage de mots", "Qui en parle", "Sujets chauds"]:
        assert needle in html, needle


def test_dashboard_with_empty_database(tmp_path, index):
    out = dashboard.export(str(tmp_path / "i.html"), 30, store=SqliteStore(":memory:"), index=index)
    data = extract_payload(open(out, encoding="utf-8").read())
    assert data["articles"] == [] and data["signals"] == []


# ---- weekly report ---------------------------------------------------------------

def test_weekly_report_dry_run(tmp_path, monkeypatch, index):
    monkeypatch.setenv("LOCAL_DB", str(tmp_path / "w.db"))
    monkeypatch.chdir(tmp_path)
    store = get_store()
    seed(store, index, days=14)
    ns = weekly_report.parse_args(["--dry-run", "--entities", ENTITIES])
    assert weekly_report.run(ns) == 0
    html = open(tmp_path / "output" / "weekly.html", encoding="utf-8").read()
    assert "Synthèse de la semaine" in html


def test_weekly_report_without_data_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DB", str(tmp_path / "w.db"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(weekly_report, "deliver", lambda *a, **k: pytest.fail("nothing to send"))
    ns = weekly_report.parse_args(["--entities", ENTITIES])
    assert weekly_report.run(ns) == 0
