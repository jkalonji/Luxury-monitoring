"""HTML e-mails (daily recap + weekly digest) and sending through Resend."""

import html
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests

from entities import EntityIndex
from models import CATEGORY_EMOJI
from signals import METRIC_LABELS, detect_weak_signals, rows_to_articles
from topics import source_words, term_counts

GOLD, INK, PAPER, MUTED = "#a8843f", "#141414", "#f6f2ea", "#77716a"
POS, NEG, NEU = "#3f7d58", "#b5473a", "#b9b3a8"
RESEND_URL = "https://api.resend.com/emails"
_FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
              "novembre", "décembre"]


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def fr_date(d: datetime | str) -> str:
    d = datetime.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.day} {_FR_MONTHS[d.month - 1]} {d.year}"


# ---------------------------------------------------------------------------
# HTML building blocks (inline CSS only: e-mail clients strip <style>/JS)
# ---------------------------------------------------------------------------

def _shell(title: str, subtitle: str, body: str, dashboard_url: str) -> str:
    cta = (f'<p style="text-align:center;margin:28px 0 8px"><a href="{_e(dashboard_url)}" style="background:{INK};color:#fff;'
           f'padding:12px 26px;text-decoration:none;font-size:13px;letter-spacing:.08em;text-transform:uppercase">'
           f'Ouvrir le dashboard</a></p>') if dashboard_url else ""
    return f"""<!doctype html><html lang="fr"><body style="margin:0;background:{PAPER};font-family:Georgia,'Times New Roman',serif;color:{INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;background:#fff">
<tr><td style="background:{INK};color:#fff;padding:26px 30px">
<div style="font:11px Helvetica,Arial,sans-serif;letter-spacing:.22em;text-transform:uppercase;color:{GOLD}">Luxury Radar</div>
<div style="font-size:26px;margin-top:6px">{_e(title)}</div>
<div style="font:13px Helvetica,Arial,sans-serif;color:#bdb6aa;margin-top:4px">{_e(subtitle)}</div></td></tr>
<tr><td style="padding:22px 30px 30px;font:14px/1.5 Helvetica,Arial,sans-serif">{body}{cta}</td></tr>
</table>
<p style="font:11px Helvetica,Arial,sans-serif;color:{MUTED};margin-top:14px">Généré automatiquement — sources : presse mode &amp; luxe, Wikipedia, Google Trends, Bluesky.</p>
</td></tr></table></body></html>"""


def _h(text: str) -> str:
    return (f'<h2 style="font:600 12px Helvetica,Arial,sans-serif;letter-spacing:.16em;text-transform:uppercase;'
            f'color:{GOLD};border-bottom:1px solid #e6e0d4;padding-bottom:6px;margin:26px 0 12px">{_e(text)}</h2>')


def _kpis(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<td align="center" style="padding:10px 4px"><div style="font:600 24px Georgia,serif">{_e(v)}</div>'
        f'<div style="font-size:11px;color:{MUTED};text-transform:uppercase;letter-spacing:.08em">{_e(k)}</div></td>'
        for k, v in items)
    return f'<table role="presentation" width="100%" style="background:{PAPER}"><tr>{cells}</tr></table>'


def _sentiment_bar(pos: int, neg: int, neu: int) -> str:
    total = max(pos + neg + neu, 1)
    segs = "".join(f'<td width="{w:.0f}%" style="background:{c};height:8px;font-size:0">&nbsp;</td>'
                   for w, c in ((pos / total * 100, POS), (neu / total * 100, NEU), (neg / total * 100, NEG)) if w > 0)
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>{segs}</tr></table>'


def _sentiment_counts(rows: list[dict]) -> tuple[int, int, int]:
    c = Counter(r.get("sentiment") for r in rows)
    return c.get("Positif", 0), c.get("Négatif", 0), c.get("Neutre", 0)


def _signal_line(signal: dict, index: EntityIndex) -> str:
    label = signal["entity_id"] if signal["entity_type"] == "term" else index.label(signal["entity_id"])
    chips = "".join(
        f'<span style="display:inline-block;background:{PAPER};border:1px solid #e6e0d4;padding:1px 7px;margin:2px 4px 0 0;'
        f'font-size:12px">{_e(METRIC_LABELS["term" if signal["entity_type"] == "term" else h["metric"]])} '
        f'<b style="color:{NEG}">×{h["ratio"]:g}</b></span>' for h in signal["hits"])
    kind = {"house": "maison", "group": "groupe", "person": "personne", "term": "terme"}[signal["entity_type"]]
    return (f'<div style="margin:0 0 10px"><b>{_e(label)}</b> <span style="color:{MUTED};font-size:12px">{kind}</span><br>{chips}</div>')


def _signals_block(signals: list[dict], index: EntityIndex, limit: int = 6) -> str:
    if not signals:
        return f'<p style="color:{MUTED}">Aucun signal faible détecté (seuil : ×2 la moyenne des 7 jours précédents).</p>'
    return "".join(_signal_line(s, index) for s in signals[:limit])


def _link(a: dict) -> str:
    return f'<a href="{_e(a["url"])}" style="color:{INK};text-decoration:underline">{_e(a["title"])}</a> <span style="color:{MUTED}">— {_e(a["source"])}</span>'


def _health_block(reports: list[dict]) -> str:
    failed = [r for r in reports if r.get("error")]
    if not failed:
        return ""
    names = ", ".join(_e(r["name"]) for r in failed)
    return f'<p style="font-size:12px;color:{NEG};margin-top:20px">⚠️ Sources en échec : {names}.</p>'


# ---------------------------------------------------------------------------
# Daily
# ---------------------------------------------------------------------------

def build_daily(*, day: str, new_rows: list[dict], clusters: list[dict], signals: list[dict], index: EntityIndex,
                dashboard_url: str = "", reports: list[dict] | None = None) -> tuple[str, str, str]:
    """new_rows: articles collected in this run (dicts). clusters: [{label, article_count, source_count, articles:[dict]}]."""
    magazine = [r for r in new_rows if r.get("kind") == "article"]
    social = [r for r in new_rows if r.get("kind") == "social"]
    pos, neg, neu = _sentiment_counts(magazine)
    cats = Counter(r.get("category") for r in magazine)
    ents = Counter(e for r in magazine for e in r.get("entities", []) if e in index.by_id)

    body = _kpis([("articles", str(len(magazine))), ("posts sociaux", str(len(social))),
                  ("sujets chauds", str(len(clusters))), ("signaux faibles", str(len(signals)))])
    body += _h("Sentiment du jour")
    body += _sentiment_bar(pos, neg, neu)
    body += (f'<p style="font-size:12px;color:{MUTED};margin:6px 0 0">'
             f'<span style="color:{POS}">■</span> Positif {pos} &nbsp; <span style="color:{NEU}">■</span> Neutre {neu} &nbsp; '
             f'<span style="color:{NEG}">■</span> Négatif {neg}</p>')

    body += _h("Signaux faibles")
    body += _signals_block(signals, index)

    body += _h("Sujets chauds")
    if clusters:
        for c in clusters[:5]:
            top = "<br>".join(_link(a) for a in c["articles"][:2])
            body += (f'<div style="margin-bottom:14px"><b>{_e(c["label"])}</b> '
                     f'<span style="color:{MUTED};font-size:12px">{c["article_count"]} articles · {c["source_count"]} sources</span>'
                     f'<div style="font-size:13px;margin-top:3px">{top}</div></div>')
    else:
        body += f'<p style="color:{MUTED}">Pas de sujet partagé par plusieurs sources aujourd\'hui.</p>'

    body += _h("Maisons les plus citées")
    for eid, n in ents.most_common(6):
        rows = [r for r in magazine if eid in r.get("entities", [])]
        p, ng, nu = _sentiment_counts(rows)
        body += (f'<table role="presentation" width="100%" style="margin-bottom:8px"><tr><td width="38%"><b>{_e(index.label(eid))}</b></td>'
                 f'<td width="12%" style="color:{MUTED}">{n}</td><td>{_sentiment_bar(p, ng, nu)}</td></tr></table>')
    if not ents:
        body += f'<p style="color:{MUTED}">Aucune maison suivie citée.</p>'

    body += _h("Par catégorie")
    body += "".join(f'<div style="margin:2px 0">{CATEGORY_EMOJI.get(c, "•")} {_e(c)} <b>{n}</b></div>'
                    for c, n in cats.most_common() if c)
    body += _health_block(reports or [])

    subject = f"Luxury Radar — {fr_date(day)} : {len(magazine)} articles" + (f", {len(signals)} signal(s) faible(s)" if signals else "")
    lines = [subject, "", f"Sentiment : {pos} positifs / {neu} neutres / {neg} négatifs", ""]
    lines += ["Signaux faibles :"] + [f"- {_signal_text(s, index)}" for s in signals[:6]]
    lines += ["", "Sujets chauds :"] + [f"- {c['label']} ({c['article_count']} articles)" for c in clusters[:5]]
    if dashboard_url:
        lines += ["", f"Dashboard : {dashboard_url}"]
    return subject, _shell("Récap du jour", fr_date(day), body, dashboard_url), "\n".join(lines)


def _signal_text(signal: dict, index: EntityIndex) -> str:
    label = signal["entity_id"] if signal["entity_type"] == "term" else index.label(signal["entity_id"])
    return f"{label}: " + ", ".join(
        f"{METRIC_LABELS['term' if signal['entity_type'] == 'term' else h['metric']]} x{h['ratio']:g}" for h in signal["hits"])


# ---------------------------------------------------------------------------
# Weekly
# ---------------------------------------------------------------------------

def _delta(cur: int, prev: int, comparable: bool = True) -> str:
    if not comparable:
        return "—"
    if prev == 0:
        return "nouveau" if cur else "—"
    pct = (cur - prev) / prev * 100
    color = POS if pct >= 0 else NEG
    return f'<span style="color:{color}">{pct:+.0f}%</span>'


def weekly_signals(signal_rows: list[dict], end_day: str, days: int = 7) -> list[dict]:
    """Best detection per entity over the week's last `days` days (each day compared to its own baseline)."""
    best: dict[str, dict] = {}
    end = datetime.fromisoformat(end_day)
    for i in range(days):
        d = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        for s in detect_weak_signals(signal_rows, day=d):
            if s["entity_id"] not in best or s["score"] > best[s["entity_id"]]["score"]:
                best[s["entity_id"]] = s
    return sorted(best.values(), key=lambda s: -s["score"])


def build_weekly(*, end_day: str, rows: list[dict], prev_rows: list[dict], signal_rows: list[dict],
                 stories: list[dict], index: EntityIndex, dashboard_url: str = "") -> tuple[str, str, str]:
    """rows / prev_rows: stored articles of the week and of the week before (dicts)."""
    cur = [r for r in rows if r.get("kind") == "article"]
    prev = [r for r in prev_rows if r.get("kind") == "article"]
    start = (datetime.fromisoformat(end_day) - timedelta(days=6)).strftime("%Y-%m-%d")
    pos, neg, neu = _sentiment_counts(cur)
    ppos, pneg, pneu = _sentiment_counts(prev)
    net = lambda p, n, u: round((p - n) / max(p + n + u, 1) * 100)  # noqa: E731
    # the first weeks the previous week is only partly collected: a delta would be an artefact
    comparable = len(prev) >= 0.5 * len(cur)

    body = _kpis([("articles", str(len(cur))), ("vs semaine préc.", f"{(len(cur) - len(prev)) / len(prev) * 100:+.0f}%" if comparable else "—"),
                  ("sentiment net", f"{net(pos, neg, neu):+d}"), ("sources actives", str(len({r['source'] for r in cur})))])
    body += _h("Sentiment de la semaine")
    body += _sentiment_bar(pos, neg, neu)
    body += (f'<p style="font-size:12px;color:{MUTED};margin:6px 0 0">Sentiment net (positifs − négatifs) : '
             f'<b>{net(pos, neg, neu):+d}</b> contre {net(ppos, pneg, pneu):+d} la semaine précédente.</p>')

    signals = weekly_signals(signal_rows, end_day)
    body += _h("Signaux faibles de la semaine")
    body += _signals_block(signals, index, limit=8)

    body += _h("Maisons : volume et sentiment")
    cur_ents = Counter(e for r in cur for e in r.get("entities", []) if e in index.by_id)
    prev_ents = Counter(e for r in prev for e in r.get("entities", []) if e in index.by_id)
    rows_html = ""
    for eid, n in cur_ents.most_common(10):
        p, ng, nu = _sentiment_counts([r for r in cur if eid in r.get("entities", [])])
        rows_html += (f'<tr><td style="padding:5px 0"><b>{_e(index.label(eid))}</b></td><td align="right" style="padding:5px 8px">{n}</td>'
                      f'<td align="right" style="padding:5px 8px">{_delta(n, prev_ents.get(eid, 0), comparable)}</td>'
                      f'<td width="35%">{_sentiment_bar(p, ng, nu)}</td></tr>')
    body += f'<table role="presentation" width="100%">{rows_html}</table>' if rows_html else f'<p style="color:{MUTED}">Aucune donnée.</p>'

    body += _h("Thèmes de la semaine")
    cur_articles = rows_to_articles(cur)
    exclude = index.alias_words() | source_words(cur_articles)
    ranked = sorted(term_counts(cur_articles, exclude, min_articles=4).items(), key=lambda kv: (-kv[1]["count"], -len(kv[0])))
    terms = []
    for t, s_ in ranked:  # a unigram already inside a listed bigram ('dario' / 'dario vitale') adds nothing
        if " " not in t and any(t in u.split() for u, _ in terms):
            continue
        terms.append((t, s_))
    terms = terms[:12]
    body += "".join(f'<span style="display:inline-block;background:{PAPER};padding:3px 9px;margin:0 6px 6px 0">{_e(t)} '
                    f'<b>{s["count"]}</b></span>' for t, s in terms) or f'<p style="color:{MUTED}">—</p>'

    body += _h("Histoires qui durent")
    live = sorted((s for s in stories if s["last_seen"] >= start and s["first_seen"] != s["last_seen"]),
                  key=lambda s: -s["article_count"])[:5]
    body += "".join(f'<div style="margin-bottom:6px"><b>{_e(s["label"])}</b> <span style="color:{MUTED};font-size:12px">'
                    f'{s["article_count"]} articles · du {fr_date(s["first_seen"])} au {fr_date(s["last_seen"])}</span></div>'
                    for s in live) or f'<p style="color:{MUTED}">Aucune histoire sur plusieurs jours.</p>'

    body += _h("Sources les plus actives")
    src = Counter(r["source"] for r in cur)
    body += "".join(f'<div style="margin:2px 0">{_e(s)} <b>{n}</b></div>' for s, n in src.most_common(6))

    subject = f"Luxury Radar — semaine du {fr_date(start)} : {len(cur)} articles"
    text = "\n".join([subject, "", f"Sentiment net : {net(pos, neg, neu):+d} (préc. {net(ppos, pneg, pneu):+d})", "",
                      "Signaux faibles :", *[f"- {_signal_text(s, index)}" for s in signals[:8]],
                      "", "Maisons :", *[f"- {index.label(e)} : {n}" for e, n in cur_ents.most_common(10)],
                      *(["", f"Dashboard : {dashboard_url}"] if dashboard_url else [])])
    return subject, _shell("Synthèse de la semaine", f"{fr_date(start)} → {fr_date(end_day)}", body, dashboard_url), text


# ---------------------------------------------------------------------------
# Sending (Resend)
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str, text_body: str) -> bool:
    """Send through Resend. Env: RESEND_API_KEY, MAIL_TO (comma-separated), MAIL_FROM. Returns True on success."""
    api_key, to = os.environ.get("RESEND_API_KEY"), os.environ.get("MAIL_TO", "")
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    if not api_key or not recipients:
        logging.warning("RESEND_API_KEY or MAIL_TO not set - e-mail not sent")
        return False
    sender = os.environ.get("MAIL_FROM") or "Luxury Radar <onboarding@resend.dev>"
    try:
        resp = requests.post(RESEND_URL, timeout=30, headers={"Authorization": f"Bearer {api_key}"},
                             json={"from": sender, "to": recipients, "subject": subject, "html": html_body, "text": text_body})
    except requests.RequestException as e:
        logging.error(f"Resend request failed: {e}")
        return False
    if resp.status_code >= 300:
        logging.error(f"Resend rejected the e-mail ({resp.status_code}): {resp.text[:300]}")
        return False
    logging.info(f"E-mail sent to {len(recipients)} recipient(s)")
    return True


def deliver(subject: str, html_body: str, text_body: str, name: str, dry_run: bool = False) -> bool:
    """Write a preview to output/<name>.html, then send unless dry_run. Returns send success (True on dry run)."""
    os.makedirs("output", exist_ok=True)
    with open(os.path.join("output", f"{name}.html"), "w", encoding="utf-8") as f:
        f.write(html_body)
    logging.info(f"Preview written to output/{name}.html")
    return True if dry_run else send_email(subject, html_body, text_body)
