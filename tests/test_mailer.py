import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

import mailer
from mailer import build_daily, build_weekly, deliver, fr_date, send_email, weekly_signals
from store import article_row

DAY = "2026-09-19"


def rows(make_article, n_dior=3, **kw):
    arts = [make_article(f"Dior story {i}", entities=["dior"], sentiment="Positif", category="Business & Finance", **kw)
            for i in range(n_dior)]
    arts.append(make_article("Gucci scandal", entities=["gucci"], sentiment="Négatif", category="Drama & Controverses", **kw))
    arts.append(make_article("Bluesky chatter about Dior", entities=["dior"], kind="social", source="Bluesky / @a", **kw))
    return [article_row(a) for a in arts]


def test_fr_date():
    assert fr_date("2026-02-01") == "1 février 2026" and fr_date("2026-12-25") == "25 décembre 2026"


def test_build_daily_content(make_article, index):
    new = rows(make_article)
    clusters = [{"label": "Demna chez Gucci", "article_count": 3, "source_count": 2, "articles": new[:2]}]
    signals = [{"entity_id": "dior", "entity_type": "house", "day": DAY, "score": 6,
                "hits": [{"metric": "wiki_views", "value": 500, "baseline": 100, "ratio": 5.0}]}]
    subject, html, text = build_daily(day=DAY, new_rows=new, clusters=clusters, signals=signals, index=index,
                                      dashboard_url="https://example.github.io/x/",
                                      reports=[{"name": "Vogue", "error": ""}, {"name": "WWD", "error": "HTTP 403"}])
    assert subject == "Luxury Radar — 19 septembre 2026 : 4 articles, 1 signal(s) faible(s)"
    assert "Demna chez Gucci" in html and "Dior" in html and "vues Wikipedia" in html and "×5" in html
    assert "https://example.github.io/x/" in html and "Ouvrir le dashboard" in html
    assert "Sources en échec" in html and "WWD" in html
    assert "Positif 3" in html and "Négatif 1" in html
    assert "Dashboard : https://example.github.io/x/" in text and "Demna chez Gucci" in text


def test_build_daily_handles_empty_day(index):
    subject, html, text = build_daily(day=DAY, new_rows=[], clusters=[], signals=[], index=index)
    assert "0 articles" in subject and "Aucun signal faible" in html and "Ouvrir le dashboard" not in html


def test_build_daily_escapes_html_from_titles(make_article, index):
    evil = rows(make_article)
    evil[0]["title"] = '<script>alert(1)</script> & "quotes"'
    clusters = [{"label": "<b>x</b>", "article_count": 3, "source_count": 2, "articles": evil[:1]}]
    _, html, _ = build_daily(day=DAY, new_rows=evil, clusters=clusters, signals=[], index=index)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html and "<b>x</b>" not in html


def _week(make_article, start_offset, n):
    return [article_row(make_article(f"Dior story {i}", entities=["dior"], days_ago=start_offset + i % 7,
                                     sentiment="Positif" if i % 2 else "Neutre", source=f"S{i % 4}"))
            for i in range(n)]


def test_build_weekly_content_and_comparison_guard(make_article, index):
    cur, prev = _week(make_article, 1, 40), _week(make_article, 8, 30)
    subject, html, text = build_weekly(end_day=DAY, rows=cur, prev_rows=prev, signal_rows=[], stories=[], index=index,
                                       dashboard_url="https://d/")
    assert subject.startswith("Luxury Radar — semaine du 13 septembre 2026 : 40 articles")
    assert "+33%" in html            # 40 vs 30
    assert "Maisons : volume et sentiment" in html and "Dior" in html and "Aucune histoire" in html
    # previous week barely collected -> no misleading percentages
    _, html2, _ = build_weekly(end_day=DAY, rows=cur, prev_rows=prev[:3], signal_rows=[], stories=[], index=index)
    assert "nouveau" not in html2 and "+1233%" not in html2


def test_build_weekly_lists_long_running_stories(make_article, index):
    stories = [{"label": "Affaire X", "first_seen": "2026-09-14", "last_seen": "2026-09-18", "article_count": 9},
               {"label": "One-day", "first_seen": "2026-09-18", "last_seen": "2026-09-18", "article_count": 5}]
    _, html, _ = build_weekly(end_day=DAY, rows=_week(make_article, 1, 10), prev_rows=[], signal_rows=[],
                              stories=stories, index=index)
    assert "Affaire X" in html and "One-day" not in html


def test_weekly_signals_takes_best_day_per_entity():
    def s(day, v):
        return {"entity_id": "dior", "entity_type": "house", "metric": "articles", "day": day, "value": float(v)}
    base = [s(f"2026-09-{d:02d}", 2) for d in range(10, 17)]
    sigs = weekly_signals(base + [s("2026-09-17", 4), s("2026-09-18", 12)], "2026-09-18")
    assert len(sigs) == 1 and sigs[0]["day"] == "2026-09-18"


# ---- sending ---------------------------------------------------------------------

class Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


def test_send_email_success_payload(monkeypatch):
    sent = {}

    def fake_post(url, **kw):
        sent.update(url=url, **kw)
        return Resp(200)

    monkeypatch.setattr(mailer.requests, "post", fake_post)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("MAIL_TO", "a@x.com, b@y.com ,")
    assert send_email("Sujet", "<p>x</p>", "x") is True
    assert sent["url"] == "https://api.resend.com/emails"
    assert sent["headers"]["Authorization"] == "Bearer re_test"
    assert sent["json"]["to"] == ["a@x.com", "b@y.com"] and sent["json"]["from"] == "Luxury Radar <onboarding@resend.dev>"
    assert sent["json"]["subject"] == "Sujet" and sent["json"]["html"] == "<p>x</p>" and sent["json"]["text"] == "x"
    monkeypatch.setenv("MAIL_FROM", "Radar <radar@mydomain.com>")
    send_email("s", "h", "t")
    assert sent["json"]["from"] == "Radar <radar@mydomain.com>"


@pytest.mark.parametrize("outcome", [Resp(403, "domain not verified"), requests.ConnectionError("down")])
def test_send_email_failures_return_false(monkeypatch, outcome):
    def fake_post(url, **kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mailer.requests, "post", fake_post)
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("MAIL_TO", "a@x.com")
    assert send_email("s", "h", "t") is False


def test_send_email_without_config_does_not_call_network(monkeypatch):
    monkeypatch.setattr(mailer.requests, "post", lambda *a, **k: pytest.fail("must not be called"))
    assert send_email("s", "h", "t") is False
    monkeypatch.setenv("RESEND_API_KEY", "k")
    assert send_email("s", "h", "t") is False        # no recipients


def test_deliver_writes_preview_and_respects_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mailer, "send_email", lambda *a: pytest.fail("dry run must not send"))
    assert deliver("s", "<p>hi</p>", "hi", "daily", dry_run=True) is True
    assert (tmp_path / "output" / "daily.html").read_text(encoding="utf-8") == "<p>hi</p>"
    monkeypatch.setattr(mailer, "send_email", lambda *a: False)
    assert deliver("s", "<p>hi</p>", "hi", "daily") is False
