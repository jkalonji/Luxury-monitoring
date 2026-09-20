"""Luxury Radar dashboard.

Static export (GitHub Pages):   python dashboard.py --export --days 30 --output site/index.html
Local Streamlit wrapper:        streamlit run dashboard.py

Both render the same self-contained page (dashboard_template.html): the data is embedded as JSON and
filtered/aggregated in the browser, so the published page needs no backend and no API key.
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from entities import EntityIndex
from models import CATEGORIES, CATEGORY_EMOJI
from signals import (METRIC_LABELS, MIN_HISTORY_DAYS, coverage_start, detect_weak_signals, yesterday)
from store import Store, get_store
from topics import STOPWORDS

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_template.html")
SERIES_DAYS = 30


def _article(r: dict) -> dict:
    """Compact article record for the page (short keys keep the embedded JSON small)."""
    return {"t": r["title"], "u": r["url"], "s": r.get("source") or "", "c": r.get("country") or "",
            "p": r["published"], "k": r.get("kind") or "article", "cat": r.get("category") or "",
            "se": r.get("sentiment") or "", "e": r.get("entities") or [], "en": r.get("engagement") or 0,
            "h": r.get("hot_reason") or "", "sh": bool(r.get("supa_hot")), "sm": r.get("summary") or "",
            "st": r.get("story_id")}


def build_signals(signal_rows: list[dict], index: EntityIndex, limit: int = 15) -> list[dict]:
    """D-1 weak signals with the 30-day series behind each hit (for sparklines)."""
    series: dict[tuple, dict[str, float]] = defaultdict(dict)
    for r in signal_rows:
        series[(r["entity_id"], r["metric"])][r["day"]] = r["value"]
    out = []
    for s in detect_weak_signals(signal_rows, day=yesterday())[:limit]:
        is_term = s["entity_type"] == "term"
        hits = []
        for h in s["hits"]:
            pts = sorted(series[(s["entity_id"], h["metric"])].items())[-SERIES_DAYS:]
            hits.append({**h, "label": METRIC_LABELS["term" if is_term else h["metric"]], "series": pts})
        ent = index.by_id.get(s["entity_id"])
        out.append({"id": s["entity_id"], "type": s["entity_type"], "score": s["score"], "hits": hits,
                    "label": s["entity_id"] if is_term else index.label(s["entity_id"]),
                    "group": ent.group if ent else ""})
    return out


def build_payload(store: Store, index: EntityIndex, days: int = 30) -> dict:
    rows = store.load_articles(days)
    signal_rows = store.load_signals(SERIES_DAYS + 10)
    stories = [s for s in store.all_stories(days) if s["first_seen"] != s["last_seen"]]
    start = coverage_start(rows)
    volume_ready = (datetime.fromisoformat(start) + timedelta(days=MIN_HISTORY_DAYS)).strftime("%Y-%m-%d") if start else None
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(timespec="minutes"),
        "days": days,
        "articles": [_article(r) for r in rows],
        "entities": [{"id": e.id, "label": e.label, "type": e.type, "group": e.group} for e in index.entities],
        "alias_words": sorted(index.alias_words()),
        "stopwords": sorted(STOPWORDS),
        "categories": [{"name": c, "emoji": CATEGORY_EMOJI[c]} for c in CATEGORIES],
        "signals": build_signals(signal_rows, index),
        "signals_day": yesterday(),
        "volume_signals_from": volume_ready,
        "stories": [{"id": s["id"], "label": s["label"], "first_seen": s["first_seen"], "last_seen": s["last_seen"],
                     "count": s["article_count"], "status": s["status"]} for s in stories],
    }


def render_html(payload: dict) -> str:
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    # </script> inside JSON would end the tag early; U+2028/9 break some JS parsers.
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            .replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return template.replace("/*__DATA__*/null", data, 1)


def export(output: str, days: int = 30, store: Store | None = None, index: EntityIndex | None = None) -> str:
    store = store or get_store()
    index = index or EntityIndex.load()
    html = render_html(build_payload(store, index, days))
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        f.write(html)
    logging.info(f"Dashboard exported to {output} ({len(html) / 1024:.0f} KB)")
    return output


def _streamlit_app() -> None:  # pragma: no cover - needs a running Streamlit session
    import streamlit as st
    st.set_page_config(page_title="Luxury Radar", layout="wide", initial_sidebar_state="collapsed")
    st.markdown("<style>header,footer{visibility:hidden}.block-container{padding:0}</style>", unsafe_allow_html=True)
    days = st.sidebar.slider("Fenêtre d'analyse (jours)", 7, 60, 30)

    @st.cache_data(ttl=600)
    def page(d: int) -> str:
        return render_html(build_payload(get_store(), EntityIndex.load(), d))

    if hasattr(st, "iframe"):       # components.v1.html is deprecated in recent Streamlit
        st.iframe(page(days), height=2600)
    else:
        import streamlit.components.v1 as components
        components.html(page(days), height=2600, scrolling=True)


def _in_streamlit() -> bool:
    if "streamlit" not in sys.modules:
        return False
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


if _in_streamlit():  # pragma: no cover
    _streamlit_app()
elif __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", action="store_true", help="write the static HTML page")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--output", default="site/index.html")
    args = ap.parse_args()
    if not args.export:
        ap.error("use --export (or run through `streamlit run dashboard.py`)")
    export(args.output, args.days)
